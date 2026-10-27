# Copyright 2026 Google LLC
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

"""Explain why an ADK-only agents-cli command does not work for this project."""

from __future__ import annotations

import sys

_REASONS = {
    "eval dataset": (
        "it runs the agent through ADK's user simulator, which needs google-adk "
        "in this project's venv."
    ),
    "eval optimize": "it shells out to the `adk` binary, which this project does not use.",
    "playground": (
        'the Claude Agent SDK has no local dev UI. Use `agents-cli run "<prompt>"`, '
        "or serve the agent locally with `uv run uvicorn app.fast_api_app:app "
        "--port 8080` and query it over A2A."
    ),
}


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "this command"
    reason = _REASONS.get(
        command, "it depends on ADK, which this project does not use."
    )
    print(
        f"agents-cli {command} is not supported by the claude extension: {reason}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
