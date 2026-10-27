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

"""Register with Gemini over A2A; refuse on Agent Runtime.

Registration on Agent Runtime is not supported for Claude agents yet.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import yaml


def _deployed_to_agent_runtime() -> bool:
    targets = []
    manifest = pathlib.Path("agents-cli-manifest.yaml")
    if manifest.exists():
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        create_params = data.get("create_params") if isinstance(data, dict) else None
        if isinstance(create_params, dict):
            targets.append(create_params.get("deployment_target"))
    # `agents-cli deploy --deployment-target` can differ from the manifest.
    metadata = pathlib.Path("deployment_metadata.json")
    if metadata.exists():
        data = json.loads(metadata.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            targets.append(data.get("deployment_target"))
    return "agent_runtime" in targets


def main() -> int:
    if _deployed_to_agent_runtime():
        print(
            "Publishing to Gemini is not supported yet for Claude agents on "
            "Agent Runtime. Deploy to cloud_run and publish from there, where "
            "registration is A2A.",
            file=sys.stderr,
        )
        return 2

    env = {**os.environ, "AGENTS_CLI_DISABLE_OVERRIDES": "1"}
    # The pre-rename name, which every agents-cli version accepts.
    return subprocess.run(
        ["agents-cli", "publish", "gemini-enterprise", *sys.argv[1:]], env=env
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
