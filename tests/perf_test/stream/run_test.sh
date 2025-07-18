#!/bin/bash
# 1. Test server streams data very fast

# Function to clean up server process
cleanup() {
    pkill -f "uv run tests/perf_test/stream/stream_speed/server.py"
}

# Trap script exit to run cleanup
trap cleanup EXIT

# Start the server in the background and capture its PID
uv run tests/perf_test/stream/stream_speed/server.py &
SERVER_PID=$!

echo "Server started with PID $SERVER_PID"

# Run your benchmark script
echo "Preparing to run benchmark.py..."

export PYTHONPATH=$PWD && uv run tests/perf_test/stream/stream_speed/benchmark.py

# Check if benchmark.py exited successfully
if [ $? -ne 0 ]; then
    echo "benchmark.py failed to run successfully."
    exit 1
else
    echo "benchmark.py ran successfully."
fi
