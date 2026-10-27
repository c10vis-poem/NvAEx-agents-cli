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

"""Load test against the A2A route."""

import json
import logging
import os
import time
import uuid

from locust import HttpUser, between, task

ENDPOINT = "/a2a/app"
HOST = None
# Agent Runtime has no service URL, so its staging step passes no host: the A2A
# route sits under the engine's passthrough path.
if os.path.exists("deployment_metadata.json"):
    with open("deployment_metadata.json", encoding="utf-8") as f:
        _runtime_id = json.load(f).get("remote_agent_runtime_id")
    if _runtime_id:
        HOST = f"https://{_runtime_id.split('/')[3]}-aiplatform.googleapis.com"
        ENDPOINT = f"/reasoningEngines/v1/{_runtime_id}/api/a2a/app"
# A2A 0.3 spells states "failed"; 1.0 spells them "TASK_STATE_FAILED".
_FAILED_STATES = {"failed", "rejected", "canceled"}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class ChatStreamUser(HttpUser):
    wait_time = between(1, 3)
    host = HOST

    @task
    def chat_stream(self) -> None:
        headers = {"Content-Type": "application/json"}
        # An identity token on Cloud Run, an access token on Agent Runtime.
        token = os.environ.get("_ID_TOKEN") or os.environ.get("_AUTH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/stream",
            "params": {
                "message": {
                    "messageId": f"msg-{uuid.uuid4()}",
                    "role": "user",
                    "parts": [{"text": "Hello! Weather in New York?"}],
                }
            },
        }

        start_time = time.time()
        with self.client.post(
            ENDPOINT,
            name=f"{ENDPOINT} message/stream",
            headers=headers,
            json=request,
            catch_response=True,
            stream=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"Got status code {response.status_code}")
                return
            # A failed run still streams HTTP 200, so read the events to the end.
            first_chunk = False
            for line in response.iter_lines():
                if not line:
                    continue
                if not first_chunk:
                    first_chunk = True
                    logger.info("First chunk in %.2fs", time.time() - start_time)
                if not line.startswith(b"data:"):
                    continue
                try:
                    event = json.loads(line[len(b"data:") :])
                except json.JSONDecodeError:
                    response.failure(f"Unparseable stream event: {line[:200]!r}")
                    return
                if "error" in event:
                    response.failure(f"JSON-RPC error: {event['error']}")
                    return
                result = event.get("result") or {}
                if "message" in result or result.get("kind") == "message":
                    response.success()
                    return
                state = _task_state(result)
                if state in _FAILED_STATES:
                    response.failure(f"Task {state}")
                    return
                if state == "completed":
                    response.success()
                    return
            response.failure("Stream ended without a completed task")


def _task_state(result: dict) -> str | None:
    """Returns a stream event's task state in A2A 0.3 spelling, e.g. "failed"."""
    # 1.0 wraps the event in a "statusUpdate" or "task" key; 0.3 doesn't.
    event = result.get("statusUpdate") or result.get("task") or result
    state = (event.get("status") or {}).get("state")
    return state.removeprefix("TASK_STATE_").lower() if state else None
