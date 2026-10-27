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

"""Read a turn's reply and tool calls out of the SDK messages it streams."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app.app_utils.telemetry.config import capture_content
from app.app_utils.turn_events import subagent_name


@dataclasses.dataclass
class TurnReply:
    """What one turn produced: the reply, its tool calls, and the run stats."""

    text: str = ""
    session_id: str | None = None
    tool_calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    result: ResultMessage | None = None
    subagent_calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)


class AgentError(RuntimeError):
    """The turn failed, or couldn't start: a preflight check found a problem."""


def record(reply: TurnReply, message: Message) -> TurnReply:
    """Fold one SDK message into `reply` and return it.

    Raises:
        AgentError: `message` is an error result.
    """
    if isinstance(message, AssistantMessage):
        # Fallback for results without a reply.
        reply.text = last_text(message) or reply.text
        parent = message.parent_tool_use_id
        for block in message.content:
            if not isinstance(block, ToolUseBlock):
                continue
            call = {"id": block.id, "name": block.name, "args": block.input}
            if parent:
                call.update(agent=_subagent_name(reply, parent), parent=parent)
                reply.subagent_calls.append(call)
            else:
                reply.tool_calls.append(call)
    elif isinstance(message, UserMessage) and isinstance(message.content, list):
        results = {
            b.tool_use_id: b for b in message.content if isinstance(b, ToolResultBlock)
        }
        for call in reply.tool_calls + reply.subagent_calls:
            if call["id"] in results and "result" not in call:
                call["result"] = tool_result_text(results[call["id"]])
    elif isinstance(message, ResultMessage):
        reply.result = message
        reply.session_id = message.session_id
        # A denied tool call doesn't fail the turn. Its arguments are logged only
        # with content capture on.
        for denial in message.permission_denials or []:
            if not capture_content() and isinstance(denial, dict):
                denial = denial.get("tool_name")
            logging.warning("Claude agent was denied a tool call: %s", denial)
        if message.is_error:
            raise AgentError(f"Claude agent turn failed: {result_error(message)}")
        reply.text = message.result or reply.text
    return reply


def _subagent_name(reply: TurnReply, parent: str) -> str:
    """The subagent the `parent` call delegated to, named as in BigQuery rows."""
    for call in reply.tool_calls + reply.subagent_calls:
        if call["id"] == parent:
            return subagent_name(call["args"])
    return "subagent"


def result_error(result: ResultMessage) -> str:
    """What went wrong, from an error result."""
    return result.result or ", ".join(result.errors or []) or result.subtype


def tool_result_text(block: ToolResultBlock) -> str:
    """Flatten a tool result's content (a string or MCP content blocks)."""
    content = block.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
    return ""


def last_text(message: Message) -> str:
    """Text of a top-level assistant message, or ""."""
    if not isinstance(message, AssistantMessage) or message.parent_tool_use_id:
        return ""
    return "".join(b.text for b in message.content if isinstance(b, TextBlock))


def text_delta(message: Message) -> str:
    """The text a partial-message stream event adds, or ""."""
    if not isinstance(message, StreamEvent) or message.parent_tool_use_id:
        return ""
    event = message.event
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta") or {}
    return delta.get("text", "") if delta.get("type") == "text_delta" else ""


def starts_block(message: Message, kind: str) -> bool:
    """Whether a partial-message stream event opens a top-level `kind` block."""
    if not isinstance(message, StreamEvent) or message.parent_tool_use_id:
        return False
    event = message.event
    block = event.get("content_block") or {}
    return event.get("type") == "content_block_start" and block.get("type") == kind
