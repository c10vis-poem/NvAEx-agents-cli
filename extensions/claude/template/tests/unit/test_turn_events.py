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

"""The turn parser, fed SDK messages as the CLI sends them."""

from __future__ import annotations

from collections import Counter

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app.app_utils.turn_events import (
    ModelCallEnded,
    ModelCallStarted,
    SubagentEnded,
    SubagentPrompt,
    SubagentStarted,
    ToolCallEnded,
    ToolCallStarted,
    TurnEnded,
    TurnParser,
    TurnResult,
)


def _stream(kind: str, **fields) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s", event={"type": kind, **fields})


def _say(message_id: str | None, *blocks, parent: str | None = None, **fields):
    return AssistantMessage(
        content=list(blocks),
        model="claude-haiku-4-5",
        message_id=message_id,
        parent_tool_use_id=parent,
        **fields,
    )


def _results(*blocks: ToolResultBlock, parent: str | None = None) -> UserMessage:
    return UserMessage(content=list(blocks), parent_tool_use_id=parent)


def _result(is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s",
    )


def _parse(messages: list, error: BaseException | None = None):
    parser = TurnParser("weather_agent")
    events = [event for message in messages for event in parser.feed(message)]
    *events, ended = events + parser.finish(error)
    assert isinstance(ended, TurnEnded)
    return parser, events


def _kinds(events: list) -> list[str]:
    return [type(event).__name__ for event in events]


def _calls_per_agent(events: list, kind: type) -> Counter:
    return Counter(event.agent for event in events if isinstance(event, kind))


_DELEGATE = ToolUseBlock(
    id="agent-1",
    name="Agent",
    input={"subagent_type": "researcher", "prompt": "Paris?"},
)


def test_a_streamed_call_in_several_messages_is_one_call_with_the_final_usage():
    _, events = _parse(
        [
            _stream("message_start", message={"usage": {"input_tokens": 100}}),
            _stream("content_block_delta", delta={"type": "text_delta"}),
            _say("m1", TextBlock(text="Checking."), usage={"output_tokens": 1}),
            _say("m1", ToolUseBlock(id="t1", name="lookup", input={})),
            _stream(
                "message_delta",
                usage={"output_tokens": 20},
                delta={"stop_reason": "tool_use"},
            ),
            _stream("message_stop"),
        ]
    )

    started, call, tool = events
    assert started == ModelCallStarted(None)
    assert isinstance(call, ModelCallEnded)
    assert len(call.blocks) == 2
    assert call.usage == {"input_tokens": 100, "output_tokens": 20}
    assert call.stop_reason == "tool_use"
    assert call.first_token_at is not None
    assert tool == ToolCallStarted(None, "t1", "lookup", {})


def test_tool_results_before_message_stop_wait_for_the_call_to_end():
    _, events = _parse(
        [
            _stream("message_start", message={}),
            _say("m1", ToolUseBlock(id="t1", name="lookup", input={})),
            _results(ToolResultBlock(tool_use_id="t1", content="60F")),
            _stream("message_stop"),
        ]
    )

    assert _kinds(events) == [
        "ModelCallStarted",
        "ModelCallEnded",
        "ToolCallStarted",
        "ToolCallEnded",
    ]
    assert events[-1].content == "60F"


def test_an_unstreamed_call_ends_at_the_next_user_or_result_message():
    _, events = _parse(
        [
            _say(None, ToolUseBlock(id="t1", name="lookup", input={})),
            _say(None, TextBlock(text="and")),
            _results(ToolResultBlock(tool_use_id="t1", content="60F")),
            _say("m2", TextBlock(text="It's 60F.")),
            _result(),
        ]
    )

    calls = [e for e in events if isinstance(e, ModelCallEnded)]
    assert [len(c.blocks) for c in calls] == [2, 1]
    assert _calls_per_agent(events, ModelCallEnded) == {None: 2}
    assert _calls_per_agent(events, ToolCallStarted) == {None: 1}
    assert isinstance(events[-1], TurnResult)


def test_a_failed_model_call_carries_its_error():
    _, events = _parse(
        [
            _say("m1", TextBlock(text="API Error: 429"), error="rate_limit"),
            _result(is_error=True),
        ]
    )

    [call] = [e for e in events if isinstance(e, ModelCallEnded)]
    assert call.error == "rate_limit"


@pytest.mark.parametrize("task_started_first", [False, True])
def test_a_subagent_runs_between_its_tool_call_and_result(task_started_first):
    task_started = SystemMessage(
        subtype="task_started", data={"tool_use_id": "agent-1"}
    )
    parser, events = _parse(
        [
            *([task_started] if task_started_first else []),
            _stream("message_start", message={}),
            _say("m1", _DELEGATE),
            _stream("message_stop"),
            UserMessage(content="Paris?", parent_tool_use_id="agent-1"),
            _say(
                "s1", ToolUseBlock(id="t1", name="lookup", input={}), parent="agent-1"
            ),
            _results(
                ToolResultBlock(tool_use_id="t1", content="Sunny"), parent="agent-1"
            ),
            _say("s2", TextBlock(text="Sunny."), parent="agent-1"),
            # Not a task: the result ends the subagent.
            *(
                []
                if task_started_first
                else [
                    _results(ToolResultBlock(tool_use_id="agent-1", content="Sunny."))
                ]
            ),
            *(
                [
                    SystemMessage(
                        subtype="task_notification",
                        data={"tool_use_id": "agent-1", "status": "completed"},
                    )
                ]
                if task_started_first
                else []
            ),
        ]
    )

    assert SubagentStarted("agent-1", "Paris?") in events
    assert SubagentPrompt("agent-1", "Paris?") in events
    assert SubagentEnded("agent-1", False) in events
    subagent_calls = [e for e in events if isinstance(e, ModelCallEnded) and e.agent]
    assert len(subagent_calls) == 2
    assert parser.agent_name("agent-1") == "researcher"
    assert parser.agent_name(None) == "weather_agent"
    assert _calls_per_agent(events, ModelCallEnded) == {None: 1, "agent-1": 2}
    assert _calls_per_agent(events, ToolCallStarted) == {None: 1, "agent-1": 1}
    # The subagent's last call ends before the subagent does.
    assert events.index(subagent_calls[-1]) < events.index(
        SubagentEnded("agent-1", False)
    )


def test_a_subagent_still_running_in_the_background_ends_with_the_turn():
    _, events = _parse(
        [
            _stream("message_start", message={}),
            _say("m1", _DELEGATE),
            _stream("message_stop"),
            # Returned before the subagent sent anything: it runs in the background.
            _results(ToolResultBlock(tool_use_id="agent-1", content="Started.")),
            _say("s1", TextBlock(text="Sunny."), parent="agent-1"),
            _result(is_error=True),
        ]
    )

    assert [e for e in events if isinstance(e, ModelCallEnded) and e.agent]
    assert events[-1] == SubagentEnded("agent-1", True)


def test_a_call_cut_off_mid_stream_is_closed_by_finish():
    _, events = _parse(
        [
            _stream("message_start", message={}),
            _say("m1", ToolUseBlock(id="t1", name="lookup", input={})),
            _results(ToolResultBlock(tool_use_id="t1", content="60F")),
        ],
        error=RuntimeError("stream ended"),
    )

    assert _kinds(events)[-2:] == ["ToolCallStarted", "ToolCallEnded"]


def test_a_malformed_subagent_call_falls_back_to_defaults():
    odd = ToolUseBlock(id="agent-1", name="Agent", input=["not", "a", "dict"])
    parser, events = _parse([_say("m1", odd), _result()])

    assert SubagentStarted("agent-1", None) in events
    assert parser.agent_name("agent-1") == "subagent"
    assert isinstance(events[-1], SubagentEnded)


def test_tool_results_carry_the_message_they_came_in():
    _, events = _parse(
        [
            _say("m1", *(ToolUseBlock(id=i, name="lookup", input={}) for i in "ab")),
            _results(
                ToolResultBlock(tool_use_id="a", content="1"),
                ToolResultBlock(tool_use_id="b", content="2"),
            ),
        ]
    )

    ended = [e for e in events if isinstance(e, ToolCallEnded)]
    assert len({e.message_index for e in ended}) == 1


@pytest.mark.parametrize(
    ("messages", "error", "failed", "error_kept"),
    [
        pytest.param([_result()], None, False, False, id="completed"),
        pytest.param([_result(is_error=True)], None, True, False, id="error result"),
        pytest.param([], RuntimeError("boom"), True, True, id="exception"),
        pytest.param(
            [_result()], GeneratorExit(), False, False, id="stopped at the result"
        ),
        pytest.param([], GeneratorExit(), True, True, id="stopped before a result"),
    ],
)
def test_every_turn_ends_with_turn_ended(messages, error, failed, error_kept):
    parser = TurnParser("weather_agent")
    for message in messages:
        parser.feed(message)
    *_, ended = parser.finish(error)
    assert ended == TurnEnded(failed, error if error_kept else None)


def test_a_turn_the_parser_stopped_reading_still_ends():
    parser = TurnParser("weather_agent")
    malformed = SystemMessage(
        subtype="compact_boundary", data={"compact_metadata": "x"}
    )
    assert parser.feed(malformed) == []
    assert parser.finish() == [TurnEnded(False, None)]
