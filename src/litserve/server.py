# Copyright The Lightning AI team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import asyncio
import contextlib
import copy
import inspect
import json
import logging
import multiprocessing as mp
import os
import sys
import threading
import time
import uuid
import warnings
from collections import deque
from contextlib import asynccontextmanager
from multiprocessing.context import Process
from threading import Thread
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

import uvicorn
import uvicorn.server
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from starlette.formparsers import MultiPartParser
from starlette.middleware.gzip import GZipMiddleware

from litserve import LitAPI
from litserve.callbacks.base import Callback, CallbackRunner, EventTypes
from litserve.connector import _Connector
from litserve.loggers import Logger, _LoggerConnector
from litserve.loops import LitLoop, inference_worker
from litserve.middlewares import MaxSizeMiddleware, RequestCountMiddleware
from litserve.python_client import client_template
from litserve.specs.base import LitSpec
from litserve.transport.base import MessageTransport
from litserve.transport.factory import TransportConfig, create_transport_from_config
from litserve.utils import LitAPIStatus, WorkerSetupStatus, call_after_stream

mp.allow_connection_pickling()

logger = logging.getLogger(__name__)

# if defined, it will require clients to auth with X-API-Key in the header
LIT_SERVER_API_KEY = os.environ.get("LIT_SERVER_API_KEY")

# FastAPI writes form files to disk over 1MB by default, which prevents serialization by multiprocessing
MultiPartParser.max_file_size = sys.maxsize
# renamed in PR: https://github.com/encode/starlette/pull/2780
MultiPartParser.spool_max_size = sys.maxsize


def no_auth():
    pass


def api_key_auth(x_api_key: str = Depends(APIKeyHeader(name="X-API-Key"))):
    if x_api_key != LIT_SERVER_API_KEY:
        raise HTTPException(
            status_code=401, detail="Invalid API Key. Check that you are passing a correct 'X-API-Key' in your header."
        )


async def response_queue_to_buffer(
    transport: MessageTransport,
    response_buffer: Dict[str, Union[Tuple[deque, asyncio.Event], asyncio.Event]],
    stream: bool,
    consumer_id: int = 0,
):
    if stream:
        while True:
            try:
                result = await transport.areceive(consumer_id)
                if result is None:
                    continue

                uid, response = result
                stream_response_buffer, event = response_buffer[uid]
                stream_response_buffer.append(response)
                event.set()
            except asyncio.CancelledError:
                logger.debug("Response queue to buffer task was cancelled")
                break
            except Exception as e:
                logger.error(f"Error in response_queue_to_buffer: {e}")
                break

    else:
        while True:
            try:
                result = await transport.areceive(consumer_id)
                if result is None:
                    continue

                uid, response = result
                event = response_buffer.pop(uid)
                response_buffer[uid] = response
                event.set()
            except asyncio.CancelledError:
                logger.debug("Response queue to buffer task was cancelled")
                break
            except Exception as e:
                logger.error(f"Error in response_queue_to_buffer: {e}")
                break


def _migration_warning(feature_name):
    warnings.warn(
        f"The {feature_name} parameter is being deprecated in `LitServer` "
        "and will be removed in version v0.3.0.\n\n"
        "Please update your code to pass these arguments to `LitAPI` instead.\n\n"
        "Old usage:\n"
        f"    server = LitServer(api, {feature_name}=...)\n\n"
        "New usage:\n"
        f"    api = LitAPI({feature_name}=...)\n"
        "    server = LitServer(api, ...)",
        DeprecationWarning,
        stacklevel=3,
    )


class LitServer:
    def __init__(
        self,
        lit_api: LitAPI,
        accelerator: str = "auto",
        devices: Union[str, int] = "auto",
        workers_per_device: int = 1,
        timeout: Union[float, bool] = 30,
        healthcheck_path: str = "/health",
        info_path: str = "/info",
        model_metadata: Optional[dict] = None,
        spec: Optional[LitSpec] = None,
        max_payload_size=None,
        track_requests: bool = False,
        callbacks: Optional[Union[List[Callback], Callback]] = None,
        middlewares: Optional[list[Union[Callable, tuple[Callable, dict]]]] = None,
        loggers: Optional[Union[Logger, List[Logger]]] = None,
        fast_queue: bool = False,
        max_batch_size: Optional[int] = None,
        batch_timeout: float = 0.0,
        stream: bool = False,
        api_path: Optional[str] = None,
        loop: Optional[Union[str, LitLoop]] = None,
    ):
        """Initialize a LitServer instance.

        Args:
            lit_api: The API instance that handles requests and responses.
            accelerator: Type of hardware to use, like 'cpu', 'cuda', or 'mps'. 'auto' selects the best available.
            devices: Number of devices to use, or 'auto' to select automatically.
            workers_per_device: Number of worker processes per device.
            max_batch_size: Deprecated. Use `lit_api.max_batch_size` instead.
            batch_timeout: Deprecated. Use `lit_api.batch_timeout` instead.
            timeout: Maximum time to wait for a request to complete. Set to False for no timeout.
            api_path: Deprecated. Use `LitAPI(api_path=...)` instead.
            healthcheck_path: URL path for the health check endpoint.
            info_path: URL path for the server and model information endpoint.
            model_metadata: Metadata about the model, shown at the info endpoint.
            stream: Whether to enable streaming responses.
            spec: Specification for the API, such as OpenAISpec or custom specs.
            max_payload_size: Maximum size of request payloads.
            track_requests: Whether to track the number of active requests.
            loop: Inference loop to use, or 'auto' to select based on settings.
            callbacks: List of callback classes to execute at various stages.
            middlewares: List of middleware classes to apply to the server.
            loggers: List of loggers to use for recording server activity.
            fast_queue: Whether to use ZeroMQ for faster response handling.

        """
        if max_batch_size is not None:
            warnings.warn(
                "'max_batch_size' and 'batch_timeout' are being deprecated in `LitServer` "
                "and will be removed in version v0.3.0.\n\n"
                "Please update your code to pass these arguments to `LitAPI` instead.\n\n"
                "Old usage:\n"
                "    server = LitServer(api, max_batch_size=N, batch_timeout=T, ...)\n\n"
                "New usage:\n"
                "    api = LitAPI(max_batch_size=N, batch_timeout=T, ...)\n"
                "    server = LitServer(api, ...)",
                DeprecationWarning,
                stacklevel=2,
            )
            lit_api.max_batch_size = max_batch_size
            lit_api.batch_timeout = batch_timeout

        if middlewares is None:
            middlewares = []
        if not isinstance(middlewares, list):
            _msg = (
                "middlewares must be a list of tuples"
                " where each tuple contains a middleware and its arguments. For example:\n"
                "server = ls.LitServer(ls.test_examples.SimpleLitAPI(), "
                'middlewares=[(RequestIdMiddleware, {"length": 5})])'
            )
            raise ValueError(_msg)

        # Handle 0.3.0 migration
        if api_path is not None:
            _migration_warning("api_path")
            lit_api.api_path = api_path
        if stream is True:
            _migration_warning("stream")
            lit_api.stream = stream
        if isinstance(loop, LitLoop):
            _migration_warning("loop")
            lit_api.loop = loop
        if isinstance(spec, LitSpec):
            _migration_warning("spec")
            lit_api.spec = spec
            lit_api.stream = spec.stream

        # pre setup
        lit_api.pre_setup(spec=spec)
        lit_api.loop.pre_setup(lit_api, spec=spec)

        if api_path and not api_path.startswith("/"):
            raise ValueError(
                "api_path must start with '/'. "
                "Please provide a valid api path like '/predict', '/classify', or '/v1/predict'"
            )

        if not healthcheck_path.startswith("/"):
            raise ValueError(
                "healthcheck_path must start with '/'. "
                "Please provide a valid api path like '/health', '/healthcheck', or '/v1/health'"
            )

        if not info_path.startswith("/"):
            raise ValueError(
                "info_path must start with '/'. Please provide a valid api path like '/info', '/details', or '/v1/info'"
            )

        try:
            json.dumps(model_metadata)
        except (TypeError, ValueError):
            raise ValueError("model_metadata must be JSON serializable.")

        # Check if the batch and unbatch methods are overridden in the lit_api instance
        batch_overridden = lit_api.batch.__code__ is not LitAPI.batch.__code__
        unbatch_overridden = lit_api.unbatch.__code__ is not LitAPI.unbatch.__code__

        if batch_overridden and unbatch_overridden and lit_api.max_batch_size == 1:
            warnings.warn(
                "The LitServer has both batch and unbatch methods implemented, "
                "but the max_batch_size parameter was not set."
            )

        if sys.platform == "win32" and fast_queue:
            warnings.warn("ZMQ is not supported on Windows with LitServe. Disabling ZMQ.")
            fast_queue = False

        self.healthcheck_path = healthcheck_path
        self.info_path = info_path
        self.track_requests = track_requests
        self.timeout = timeout
        # TODO: Connector
        lit_api.request_timeout = timeout
        if lit_api.batch_timeout > timeout and timeout not in (False, -1):
            raise ValueError("batch_timeout must be less than request_timeout")
        self.app = FastAPI(lifespan=self.lifespan)
        self.app.response_queue_id = None
        self.response_queue_id = None
        self.response_buffer = {}
        # gzip does not play nicely with streaming, see https://github.com/tiangolo/fastapi/discussions/8448
        # TODO: Connector
        if not lit_api.stream:
            middlewares.append((GZipMiddleware, {"minimum_size": 1000}))
        if max_payload_size is not None:
            middlewares.append((MaxSizeMiddleware, {"max_size": max_payload_size}))
        self.active_counters: List[mp.Value] = []
        self.middlewares = middlewares
        self._logger_connector = _LoggerConnector(self, loggers)
        self.logger_queue = None
        self.lit_api = lit_api
        self.workers_per_device = workers_per_device
        self.max_payload_size = max_payload_size
        self.model_metadata = model_metadata
        self._connector = _Connector(accelerator=accelerator, devices=devices)
        self._callback_runner = CallbackRunner(callbacks)
        self.use_zmq = fast_queue
        self.transport_config = None

        # specs = spec if spec is not None else []
        # self._specs = specs if isinstance(specs, Sequence) else [specs]

        decode_request_signature = inspect.signature(lit_api.decode_request)
        encode_response_signature = inspect.signature(lit_api.encode_response)

        self.request_type = decode_request_signature.parameters["request"].annotation
        if self.request_type == decode_request_signature.empty:
            self.request_type = Request

        self.response_type = encode_response_signature.return_annotation
        if self.response_type == encode_response_signature.empty:
            self.response_type = Response

        accelerator = self._connector.accelerator
        devices = self._connector.devices
        if accelerator == "cpu":
            self.devices = [accelerator]
        elif accelerator in ["cuda", "mps"]:
            device_list = devices
            if isinstance(devices, int):
                device_list = range(devices)
            self.devices = [self.device_identifiers(accelerator, device) for device in device_list]

        self.inference_workers = self.devices * self.workers_per_device
        self.transport_config = TransportConfig(transport_config="zmq" if self.use_zmq else "mp")
        self.register_endpoints()

    def launch_inference_worker(self, num_uvicorn_servers: int):
        self.transport_config.num_consumers = num_uvicorn_servers
        manager = self.transport_config.manager = mp.Manager()
        self._transport = create_transport_from_config(self.transport_config)
        self.workers_setup_status = manager.dict()
        self.request_queue = manager.Queue()
        if self._logger_connector._loggers:
            self.logger_queue = manager.Queue()

        self._logger_connector.run(self)

        specs = [self.lit_api.spec] if self.lit_api.spec else []
        for spec in specs:
            # Objects of Server class are referenced (not copied)
            logging.debug(f"shallow copy for Server is created for for spec {spec}")
            server_copy = copy.copy(self)
            del server_copy.app, server_copy.transport_config
            spec.setup(server_copy)

        process_list = []
        for worker_id, device in enumerate(self.inference_workers):
            if len(device) == 1:
                device = device[0]

            self.workers_setup_status[worker_id] = WorkerSetupStatus.STARTING

            ctx = mp.get_context("spawn")
            process = ctx.Process(
                target=inference_worker,
                args=(
                    self.lit_api,
                    self.lit_api.spec,
                    device,
                    worker_id,
                    self.request_queue,
                    self._transport,
                    self.lit_api.stream,
                    self.workers_setup_status,
                    self._callback_runner,
                    self.lit_api.loop,
                ),
            )
            process.start()
            process_list.append(process)
        return manager, process_list

    @asynccontextmanager
    async def lifespan(self, app: FastAPI):
        loop = asyncio.get_running_loop()

        if not hasattr(self, "_transport") or not self._transport:
            raise RuntimeError(
                "Response queues have not been initialized. "
                "Please make sure to call the 'launch_inference_worker' method of "
                "the LitServer class to initialize the response queues."
            )

        transport = self._transport
        future = response_queue_to_buffer(
            transport,
            self.response_buffer,
            self.lit_api.stream,
            app.response_queue_id,
        )
        task = loop.create_task(future, name=f"response_queue_to_buffer-{app.response_queue_id}")

        try:
            yield
        finally:
            self._callback_runner.trigger_event(EventTypes.ON_SERVER_END.value, litserver=self)

            # Cancel the task
            task.cancel()

            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(task, timeout=1.0)

    def device_identifiers(self, accelerator, device):
        if isinstance(device, Sequence):
            return [f"{accelerator}:{el}" for el in device]
        return [f"{accelerator}:{device}"]

    async def data_streamer(self, q: deque, data_available: asyncio.Event, send_status: bool = False):
        while True:
            await data_available.wait()
            while len(q) > 0:
                data, status = q.popleft()
                if status == LitAPIStatus.FINISH_STREAMING:
                    return

                if status == LitAPIStatus.ERROR:
                    logger.error(
                        "Error occurred while streaming outputs from the inference worker. "
                        "Please check the above traceback."
                    )
                    if send_status:
                        yield data, status
                    return
                if send_status:
                    yield data, status
                else:
                    yield data
            data_available.clear()

    @property
    def active_requests(self):
        if self.track_requests and self.active_counters:
            return sum(counter.value for counter in self.active_counters)
        return None

    def register_endpoints(self):
        """Register endpoint routes for the FastAPI app and setup middlewares."""
        self._callback_runner.trigger_event(EventTypes.ON_SERVER_START.value, litserver=self)
        workers_ready = False

        @self.app.get("/", dependencies=[Depends(self.setup_auth())])
        async def index(request: Request) -> Response:
            return Response(content="litserve running")

        @self.app.get(self.healthcheck_path, dependencies=[Depends(self.setup_auth())])
        async def health(request: Request) -> Response:
            nonlocal workers_ready
            if not workers_ready:
                workers_ready = all(v == WorkerSetupStatus.READY for v in self.workers_setup_status.values())

            lit_api_health_status = self.lit_api.health()
            if workers_ready and lit_api_health_status:
                return Response(content="ok", status_code=200)

            return Response(content="not ready", status_code=503)

        @self.app.get(self.info_path, dependencies=[Depends(self.setup_auth())])
        async def info(request: Request) -> Response:
            return JSONResponse(
                content={
                    "model": self.model_metadata,
                    "server": {
                        "devices": self.devices,
                        "workers_per_device": self.workers_per_device,
                        "timeout": self.timeout,
                        "stream": self.lit_api.stream,
                        "max_payload_size": self.max_payload_size,
                        "track_requests": self.track_requests,
                    },
                }
            )

        async def predict(request: self.request_type) -> self.response_type:
            self._callback_runner.trigger_event(
                EventTypes.ON_REQUEST.value,
                active_requests=self.active_requests,
                litserver=self,
            )
            response_queue_id = self.app.response_queue_id
            uid = uuid.uuid4()
            event = asyncio.Event()
            self.response_buffer[uid] = event
            logger.debug(f"Received request uid={uid}")

            payload = request
            if self.request_type == Request:
                if request.headers["Content-Type"] == "application/x-www-form-urlencoded" or request.headers[
                    "Content-Type"
                ].startswith("multipart/form-data"):
                    payload = await request.form()
                else:
                    payload = await request.json()

            self.request_queue.put((response_queue_id, uid, time.monotonic(), payload))

            await event.wait()
            response, status = self.response_buffer.pop(uid)
            if status == LitAPIStatus.ERROR and isinstance(response, HTTPException):
                logger.error("Error in request: %s", response)
                raise response
            if status == LitAPIStatus.ERROR:
                logger.error("Error in request: %s", response)
                raise HTTPException(status_code=500)
            self._callback_runner.trigger_event(EventTypes.ON_RESPONSE.value, litserver=self)
            return response

        async def stream_predict(request: self.request_type) -> self.response_type:
            self._callback_runner.trigger_event(
                EventTypes.ON_REQUEST.value,
                active_requests=self.active_requests,
                litserver=self,
            )
            response_queue_id = self.app.response_queue_id
            uid = uuid.uuid4()
            event = asyncio.Event()
            q = deque()
            self.response_buffer[uid] = (q, event)
            logger.debug(f"Received request uid={uid}")

            payload = request
            if self.request_type == Request:
                payload = await request.json()
            self.request_queue.put((response_queue_id, uid, time.monotonic(), payload))

            response = call_after_stream(
                self.data_streamer(q, data_available=event),
                self._callback_runner.trigger_event,
                EventTypes.ON_RESPONSE.value,
                litserver=self,
            )
            return StreamingResponse(response)

        if not self.lit_api.spec:
            stream = self.lit_api.stream
            # In the future we might want to differentiate endpoints for streaming vs non-streaming
            # For now we allow either one or the other
            endpoint = self.lit_api.api_path
            methods = ["POST"]
            self.app.add_api_route(
                endpoint,
                stream_predict if stream else predict,
                methods=methods,
                dependencies=[Depends(self.setup_auth())],
            )

        specs = [self.lit_api.spec] if self.lit_api.spec else []
        for spec in specs:
            spec: LitSpec
            # TODO check that path is not clashing
            for path, endpoint, methods in spec.endpoints:
                self.app.add_api_route(
                    path, endpoint=endpoint, methods=methods, dependencies=[Depends(self.setup_auth())]
                )

        for middleware in self.middlewares:
            if isinstance(middleware, tuple):
                middleware, kwargs = middleware
                self.app.add_middleware(middleware, **kwargs)
            elif callable(middleware):
                self.app.add_middleware(middleware)

    @staticmethod
    def generate_client_file(port: Union[str, int] = 8000):
        dest_path = os.path.join(os.getcwd(), "client.py")

        if os.path.exists(dest_path):
            logger.debug("client.py already exists in the current directory. Skipping generation.")
            return

        try:
            client_code = client_template.format(PORT=port)
            with open(dest_path, "w") as f:
                f.write(client_code)

        except Exception as e:
            logger.exception(f"Error copying file: {e}")

    def verify_worker_status(self):
        while not any(v == WorkerSetupStatus.READY for v in self.workers_setup_status.values()):
            if any(v == WorkerSetupStatus.ERROR for v in self.workers_setup_status.values()):
                raise RuntimeError("One or more workers failed to start. Shutting down LitServe")
            time.sleep(0.05)
        logger.debug("One or more workers are ready to serve requests")

    def run(
        self,
        host: str = "0.0.0.0",
        port: Union[str, int] = 8000,
        num_api_servers: Optional[int] = None,
        log_level: str = "info",
        generate_client_file: bool = True,
        api_server_worker_type: Optional[str] = None,
        **kwargs,
    ):
        if generate_client_file:
            LitServer.generate_client_file(port=port)

        port_msg = f"port must be a value from 1024 to 65535 but got {port}"
        try:
            port = int(port)
        except ValueError:
            raise ValueError(port_msg)

        if not (1024 <= port <= 65535):
            raise ValueError(port_msg)

        host_msg = f"host must be '0.0.0.0', '127.0.0.1', or '::' but got {host}"
        if host not in ["0.0.0.0", "127.0.0.1", "::"]:
            raise ValueError(host_msg)

        config = uvicorn.Config(app=self.app, host=host, port=port, log_level=log_level, **kwargs)
        sockets = [config.bind_socket()]

        if num_api_servers is None:
            num_api_servers = len(self.inference_workers)

        if num_api_servers < 1:
            raise ValueError("num_api_servers must be greater than 0")

        if sys.platform == "win32":
            warnings.warn(
                "Windows does not support forking. Using threads api_server_worker_type will be set to 'thread'"
            )
            api_server_worker_type = "thread"
        elif api_server_worker_type is None:
            api_server_worker_type = "process"

        manager, inference_workers = self.launch_inference_worker(num_api_servers)

        self.verify_worker_status()
        try:
            uvicorn_workers = self._start_server(
                port, num_api_servers, log_level, sockets, api_server_worker_type, **kwargs
            )
            print(f"Swagger UI is available at http://0.0.0.0:{port}/docs")
            # On Linux, kill signal will be captured by uvicorn.
            # => They will join and raise a KeyboardInterrupt, allowing to Shutdown server.
            for i, uw in enumerate(uvicorn_workers):
                uw: Union[Process, Thread]
                if isinstance(uw, Process):
                    print(f"Uvicorn worker {i} : [{uw.pid}]")
                uw.join()
        finally:
            print("Shutting down LitServe")
            self._transport.close()
            for iw in inference_workers:
                iw: Process
                iw.terminate()
                iw.join()
            manager.shutdown()

    def _prepare_app_run(self, app: FastAPI):
        # Add middleware to count active requests
        active_counter = mp.Value("i", 0, lock=True)
        self.active_counters.append(active_counter)
        app.add_middleware(RequestCountMiddleware, active_counter=active_counter)

    def _start_server(self, port, num_uvicorn_servers, log_level, sockets, uvicorn_worker_type, **kwargs):
        workers = []
        for response_queue_id in range(num_uvicorn_servers):
            self.app.response_queue_id = response_queue_id
            if self.lit_api.spec:
                self.lit_api.spec.response_queue_id = response_queue_id
            app: FastAPI = copy.copy(self.app)

            self._prepare_app_run(app)
            config = uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level=log_level, **kwargs)
            if sys.platform == "win32" and num_uvicorn_servers > 1:
                logger.debug("Enable Windows explicit socket sharing...")
                # We make sure sockets is listening...
                # It prevents further [WinError 10022]
                for sock in sockets:
                    sock.listen(config.backlog)
                # We add worker to say unicorn to use a shared socket (win32)
                # https://github.com/encode/uvicorn/pull/802
                config.workers = num_uvicorn_servers
            server = uvicorn.Server(config=config)
            if uvicorn_worker_type == "process":
                ctx = mp.get_context("fork")
                w = ctx.Process(target=server.run, args=(sockets,))
            elif uvicorn_worker_type == "thread":
                w = threading.Thread(target=server.run, args=(sockets,))
            else:
                raise ValueError("Invalid value for api_server_worker_type. Must be 'process' or 'thread'")
            w.start()
            workers.append(w)
        return workers

    def setup_auth(self):
        if hasattr(self.lit_api, "authorize") and callable(self.lit_api.authorize):
            return self.lit_api.authorize
        if LIT_SERVER_API_KEY:
            return api_key_auth
        return no_auth
