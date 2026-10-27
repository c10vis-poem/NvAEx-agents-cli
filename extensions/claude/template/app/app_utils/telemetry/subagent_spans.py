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


"""Names one turn's subagent spans, which OpenInference leaves unnamed."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from openinference.semconv.trace import SpanAttributes


class SubagentSpans:
    """Names one turn's subagent spans, and finds the span of each subagent.

    OpenInference doesn't say which Agent call a subagent span is for, so the
    first message with a new parent_tool_use_id claims the newest unclaimed span.
    """

    def __init__(self, agent_name: Callable[[str | None], str]) -> None:
        self._agent_name = agent_name
        self._turn_started = False
        self._unclaimed: dict[int, tuple[Any, list[Any]]] = {}
        self._subagent_spans: dict[str, Any] = {}
        self._span_agents: dict[int, str] = {}

    def observe(self, message: Any) -> None:
        """Claim a span for the subagent that sent `message`, if it has none yet."""
        parent = getattr(message, "parent_tool_use_id", None)
        if parent and parent not in self._subagent_spans and self._unclaimed:
            self._claim(parent, *self._unclaimed.popitem()[1])

    def subagent_span(self, parent_tool_use_id: str) -> Any:
        """The span of the subagent whose messages have this parent, if any."""
        return self._subagent_spans.get(parent_tool_use_id)

    def span_started(self, span: Any) -> str | None:
        """The name for one of the turn's OpenInference spans, if not the agent's."""
        attributes = span.attributes or {}
        if attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND) == "AGENT":
            # The first is the turn's own span; later ones are subagents'.
            if self._turn_started:
                self._unclaimed[span.get_span_context().span_id] = (span, [])
            self._turn_started = True
            return None
        parent = span.parent.span_id if span.parent else None
        if parent in self._unclaimed:
            self._unclaimed[parent][1].append(span)
        return self._span_agents.get(parent)

    def _claim(self, parent_tool_use_id: str, span: Any, children: list[Any]) -> None:
        name = self._agent_name(parent_tool_use_id)
        span.update_name(f"ClaudeAgentSDK.{name}")
        span.set_attributes(
            {SpanAttributes.AGENT_NAME: name, "gen_ai.agent.name": name}
        )
        for child in children:
            child.set_attribute("gen_ai.agent.name", name)
        self._subagent_spans[parent_tool_use_id] = span
        self._span_agents[span.get_span_context().span_id] = name


# The running turn's subagent spans, for naming spans as they start.
current_subagent_spans: ContextVar[SubagentSpans | None] = ContextVar(
    "current_subagent_spans", default=None
)
