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


"""Records gen_ai.invoke_agent.inference_calls and .tool_calls for one turn."""

from __future__ import annotations

from collections.abc import Callable

from app.app_utils.telemetry import metrics
from app.app_utils.turn_events import Event, ModelCallEnded, ToolCallStarted, TurnEnded


class CallCounts:
    """Counts one turn's model and tool calls per agent, and records them at its end."""

    def __init__(self, agent_name: Callable[[str | None], str]) -> None:
        self._agent_name = agent_name
        # The main agent always gets a point, even with no calls.
        self._model_calls: dict[str | None, int] = {None: 0}
        self._tool_calls: dict[str | None, int] = {None: 0}

    def handle(self, event: Event) -> None:
        if isinstance(event, ModelCallEnded):
            self._model_calls[event.agent] = self._model_calls.get(event.agent, 0) + 1
        elif isinstance(event, ToolCallStarted):
            self._tool_calls[event.agent] = self._tool_calls.get(event.agent, 0) + 1
        elif isinstance(event, TurnEnded):
            metrics.record_call_counts(
                (self._agent_name(agent), model_calls, self._tool_calls.get(agent, 0))
                for agent, model_calls in self._model_calls.items()
            )
