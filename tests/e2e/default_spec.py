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
import litserve as ls
from litserve.specs.openai import OpenAISpec
from litserve.test_examples.openai_spec_example import TestAPI

if __name__ == "__main__":
    api = TestAPI(spec=OpenAISpec())
    server = ls.LitServer(api)
    server.run(port=8000)
