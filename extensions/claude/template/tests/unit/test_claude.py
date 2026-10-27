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

"""How `app/app_utils/claude.py` drives the SDK, with the SDK's `query` faked."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    InMemorySessionStore,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from openinference.instrumentation import TraceConfig
from openinference.instrumentation.claude_agent_sdk import ClaudeAgentSDKInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.app_utils import (
    claude,
    project_settings,
    replies,
    settings,
    turn_options,
    workspaces,
)
from app.app_utils.telemetry import traces


def _result(**overrides) -> ResultMessage:
    fields = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "s-1",
        "result": "It's foggy.",
    }
    return ResultMessage(**{**fields, **overrides})


def _delta(text: str, parent: str | None = None) -> StreamEvent:
    return StreamEvent(
        uuid="u",
        session_id="s-1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
        parent_tool_use_id=parent,
    )


_TOOL_TURN = [
    AssistantMessage(
        content=[
            ToolUseBlock(id="t1", name="mcp__w__get_weather", input={"city": "SF"})
        ],
        model="m",
    ),
    UserMessage(
        content=[
            ToolResultBlock(tool_use_id="t1", content=[{"type": "text", "text": "60F"}])
        ]
    ),
    AssistantMessage(content=[TextBlock(text="It's foggy.")], model="m"),
    _result(),
]


def _fake_query(messages, seen: list[dict] | None = None):
    async def fake(*, prompt, options):
        if seen is not None:
            seen.append({"prompt": prompt, "options": options})
        for message in messages:
            yield message

    return fake


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Gemini platform env, and workspaces and project root inside tmp_path."""
    # Keep the project's .env out of the tests: load_dotenv() finds it from
    # claude.py's location, not cwd.
    monkeypatch.setattr(claude, "load_dotenv", lambda: None)
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Most tests run a bare ClaudeAgentOptions(), which names no model.
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test")
    for name in (
        "INSTANCE_CONNECTION_NAME",
        "DB_PASS",
        "DB_USER",
        "DB_NAME",
        "BQ_ANALYTICS_DATASET_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    turn_options._cloud_sql_store.cache_clear()
    monkeypatch.setattr(workspaces, "WORKSPACES", tmp_path / "workspaces")
    monkeypatch.setattr(workspaces, "_workspaces", {})
    project = tmp_path / "project"
    agent_config = project / "agent_config"
    agent_config.mkdir(parents=True)
    monkeypatch.setattr(project_settings, "PROJECT_ROOT", project)
    monkeypatch.setattr(project_settings, "AGENT_CONFIG_DIR", agent_config)
    return agent_config


def _workspaces() -> pathlib.Path:
    """Where the agent's conversation workspaces go."""
    return workspaces.WORKSPACES / settings.AGENT_NAME


def _options_for(*args, **kwargs) -> ClaudeAgentOptions:
    return anyio.run(turn_options.turn_options, *args, **kwargs)


def _write_local_transcript(conversation_id: str) -> None:
    """What the CLI leaves on disk after a conversation's first turn."""
    session = turn_options.session_id_for(conversation_id)
    transcript = workspaces.transcripts_dir(_workspaces() / session)
    transcript.mkdir(parents=True, exist_ok=True)
    (transcript / f"{session}.jsonl").write_text('{"type": "user"}\n')


class TestRunTurn:
    def test_collects_reply_session_and_tool_calls(self):
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            reply = anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "c")

        assert reply.text == "It's foggy."
        assert reply.session_id == "s-1"
        assert reply.tool_calls == [
            {
                "id": "t1",
                "name": "mcp__w__get_weather",
                "args": {"city": "SF"},
                "result": "60F",
            }
        ]

    def test_a_subagents_tool_calls_are_kept_apart_under_its_name(self):
        delegate = {"subagent_type": "forecaster", "prompt": "Weather in SF?"}
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="a1", name="Agent", input=delegate)],
                model="m",
            ),
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="get_weather", input={})],
                model="m",
                parent_tool_use_id="a1",
            ),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="t1", content="60F")],
                parent_tool_use_id="a1",
            ),
            UserMessage(content=[ToolResultBlock(tool_use_id="a1", content="Foggy")]),
            _result(),
        ]
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(messages)):
            reply = anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "c")

        assert [call["name"] for call in reply.tool_calls] == ["Agent"]
        assert reply.subagent_calls == [
            {
                "id": "t1",
                "name": "get_weather",
                "args": {},
                "agent": "forecaster",
                "parent": "a1",
                "result": "60F",
            }
        ]

    def test_error_result_raises(self):
        failing = [_result(is_error=True, result="model not available")]
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(failing)):
            with pytest.raises(claude.AgentError, match="model not available"):
                anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "c")

    @pytest.mark.parametrize("capture", [None, "true"])
    def test_a_denied_call_is_logged_with_its_arguments_only_on_capture(
        self, monkeypatch, caplog, capture
    ):
        if capture:
            monkeypatch.setenv(
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", capture
            )
        else:
            monkeypatch.delenv(
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False
            )
        denial = {
            "tool_name": "Bash",
            "tool_use_id": "t1",
            "tool_input": {"cmd": "ls ~"},
        }
        denied = [_result(permission_denials=[denial])]
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(denied)):
            anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "c")

        [warning] = [
            r.getMessage() for r in caplog.records if "denied" in r.getMessage()
        ]
        assert "Bash" in warning
        assert ("ls ~" in warning) == bool(capture)

    def test_an_error_result_closes_the_sdk_stream(self):
        closed = []

        async def query(*, prompt, options):
            try:
                yield _result(is_error=True, result="max turns")
                yield _result()  # never reached: the turn stops at the error
            finally:
                closed.append(True)

        async def turn_then_check():
            with pytest.raises(claude.AgentError):
                await claude.run_turn("hi", ClaudeAgentOptions(), "c")
            # Checked while the loop runs: the stream must close when the turn ends.
            return list(closed)

        with patch.object(claude.claude_agent_sdk, "query", query):
            assert anyio.run(turn_then_check) == [True]

    @pytest.mark.parametrize(
        ("cli_path", "skipped"), [(None, "1"), ("/usr/local/bin/claude", None)]
    )
    def test_the_cli_version_check_is_skipped_only_for_the_bundled_cli(
        self, monkeypatch, cli_path, skipped
    ):
        # setenv first so monkeypatch removes what the turn sets.
        monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "")
        monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK")
        with patch.object(claude.claude_agent_sdk, "query", _fake_query([_result()])):
            anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(cli_path=cli_path), "c")
        assert os.environ.get("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK") == skipped

    def test_least_recently_used_workspaces_are_evicted(self, monkeypatch):
        monkeypatch.setattr(workspaces, "MAX_WORKSPACES", 2)
        # Another process serving the same agent shares its directory.
        (_workspaces() / "other-service").mkdir(parents=True)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query([_result()])):
            for conversation in ["old", "mid", "new", "old", "newest"]:
                anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), conversation)

        kept = {d.name for d in _workspaces().iterdir()}
        assert kept == {"other-service"} | {
            turn_options.session_id_for(c) for c in ("old", "newest")
        }

    def test_a_workspace_with_a_turn_running_is_not_evicted(self, monkeypatch):
        monkeypatch.setattr(workspaces, "MAX_WORKSPACES", 1)
        cwd_existed = []

        async def query(*, prompt, options):
            if prompt == "long":
                events["started"].set()
                await events["finish"].wait()
                cwd_existed.append(pathlib.Path(options.cwd).is_dir())
            yield _result()

        async def overlapping_turns():
            events.update(started=anyio.Event(), finish=anyio.Event())
            async with anyio.create_task_group() as tg:
                tg.start_soon(claude.run_turn, "long", ClaudeAgentOptions(), "a")
                await events["started"].wait()
                await claude.run_turn("short", ClaudeAgentOptions(), "b")
                events["finish"].set()

        events: dict[str, anyio.Event] = {}
        with patch.object(claude.claude_agent_sdk, "query", query):
            anyio.run(overlapping_turns)

        assert cwd_existed == [True]

    def test_turns_are_recorded_for_bigquery_only_when_it_is_on(self, monkeypatch):
        rows: list[dict] = []
        writer = MagicMock(put=rows.append)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            with patch.object(claude.bq_analytics, "writer", return_value=None):
                anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "c")
            assert rows == []
            with patch.object(claude.bq_analytics, "writer", return_value=writer):
                anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "conv-1")

        assert rows[0]["event_type"] == "USER_MESSAGE_RECEIVED"
        assert rows[-1]["event_type"] == "INVOCATION_COMPLETED"
        assert {r["session_id"] for r in rows} == {
            turn_options.session_id_for("conv-1")
        }
        assert {r["user_id"] for r in rows} == {"A2A_USER_conv-1"}

    def test_a_failing_observer_never_fails_the_turn_or_hides_its_error(
        self, monkeypatch, caplog
    ):
        class Broken:
            def __init__(self, agent_name):
                pass

            def handle(self, event):
                raise ValueError("observer bug")

        async def failing_query(*, prompt, options):
            yield _TOOL_TURN[0]
            raise RuntimeError("CLI exited")

        monkeypatch.setattr(claude, "CallCounts", Broken)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            reply = anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "c")
        with patch.object(claude.claude_agent_sdk, "query", failing_query):
            with pytest.raises(RuntimeError, match="CLI exited"):
                anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "c")

        assert reply.text == "It's foggy."
        assert "Broken failed on TurnEnded" in caplog.text

    def test_model_calls_are_logged_when_the_upload_hook_is_on(self, monkeypatch):
        monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "upload")
        monkeypatch.setenv(
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH", "memory://logs/completions"
        )
        events: list = []
        monkeypatch.setattr(claude.completions.telemetry, "emit_event", events.append)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "conv-1")

        # The tool call and the answer.
        assert len(events) == 2
        assert {e.attributes["gen_ai.conversation.id"] for e in events} == {
            turn_options.session_id_for("conv-1")
        }

    def test_turns_of_one_conversation_run_one_after_another(self):
        events: list[str] = []
        resumed: dict[str, str | None] = {}

        async def query(*, prompt, options):
            events.append(f"start {prompt}")
            resumed[prompt] = options.resume
            if prompt != "c":
                _write_local_transcript("ctx-1")
            await anyio.sleep(0.01)
            events.append(f"end {prompt}")
            yield _result(result=prompt)

        async def overlapping_turns():
            async with anyio.create_task_group() as tg:
                for prompt, conversation in [
                    ("a", "ctx-1"),
                    ("b", "ctx-1"),
                    ("c", "ctx-2"),
                ]:
                    tg.start_soon(
                        claude.run_turn, prompt, ClaudeAgentOptions(), conversation
                    )

        with patch.object(claude.claude_agent_sdk, "query", query):
            anyio.run(overlapping_turns)

        # The second turn resumes the first's transcript instead of starting a
        # parallel one, and another conversation doesn't wait for either.
        assert events.index("end a") < events.index("start b")
        assert events.index("start c") < events.index("end a")
        assert resumed == {
            "a": None,
            "b": turn_options.session_id_for("ctx-1"),
            "c": None,
        }
        assert not claude._turn_locks

    # Without its prompt first, the subagent's first message is its tool call.
    @pytest.mark.parametrize("prompt_first", [True, False])
    def test_a_subagents_spans_name_it_and_hold_its_model_calls(
        self, monkeypatch, prompt_first
    ):
        monkeypatch.setenv("OPENINFERENCE_ENABLE_GENAI_SEMCONV", "true")
        monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "upload")
        monkeypatch.setenv(
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH", "memory://logs/completions"
        )
        monkeypatch.setattr(claude.completions.telemetry, "emit_event", lambda _: None)
        monkeypatch.setattr(settings, "AGENT_NAME", "weather_agent")
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(traces.AgentNameProcessor("weather_agent"))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        delegate = ToolUseBlock(
            id="agent-1",
            name="Agent",
            input={"subagent_type": "weather_researcher", "prompt": "Paris?"},
        )
        lookup = ToolUseBlock(id="t1", name="get_weather", input={"city": "Paris"})
        # The CLI runs the subagent in the background: the Agent call returns
        # before the subagent's messages arrive.
        turn = [
            AssistantMessage(content=[delegate], model="m", message_id="m1"),
            UserMessage(content=[ToolResultBlock(tool_use_id="agent-1", content="")]),
            *(
                [UserMessage(content="Paris?", parent_tool_use_id="agent-1")]
                if prompt_first
                else []
            ),
            AssistantMessage(
                content=[lookup],
                model="m",
                message_id="s1",
                parent_tool_use_id="agent-1",
            ),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="t1", content="Sunny.")],
                parent_tool_use_id="agent-1",
            ),
            AssistantMessage(
                content=[TextBlock(text="Sunny.")],
                model="m",
                message_id="s2",
                parent_tool_use_id="agent-1",
            ),
            AssistantMessage(
                content=[TextBlock(text="It's sunny.")], model="m", message_id="m2"
            ),
            _result(),
        ]
        instrumentor = ClaudeAgentSDKInstrumentor()
        # The instrumentor wraps query() where the SDK defines it, then
        # re-exports the wrapper; uninstrument() re-exports the original.
        query_module = sys.modules["claude_agent_sdk.query"]
        with (
            patch.object(query_module, "query", _fake_query(turn)),
            patch.object(claude.claude_agent_sdk, "query"),
        ):
            instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
            try:
                with patch.object(
                    claude.completions.trace, "get_tracer", provider.get_tracer
                ):
                    anyio.run(claude.run_turn, "weather?", ClaudeAgentOptions(), "c")
            finally:
                instrumentor.uninstrument()

        spans = exporter.get_finished_spans()
        [root] = [s for s in spans if s.name == "ClaudeAgentSDK.query"]
        [subagent] = [s for s in spans if s.parent and s.name.startswith("ClaudeAgent")]
        [tool] = [s for s in spans if s.name == "get_weather"]
        [call] = [s for s in spans if s.name == "Agent"]
        chats = [s for s in spans if s.name == "chat m"]

        def agent_of(span):
            return span.attributes["gen_ai.agent.name"]

        assert subagent.name == "ClaudeAgentSDK.weather_researcher"
        assert agent_of(subagent) == "weather_researcher"
        assert tool.parent.span_id == subagent.context.span_id
        assert agent_of(tool) == "weather_researcher"
        assert agent_of(call) == "weather_agent"
        nested: dict[int, list[str]] = {}
        for chat in chats:
            nested.setdefault(chat.parent.span_id, []).append(agent_of(chat))
        assert nested == {
            root.context.span_id: ["weather_agent"] * 2,
            subagent.context.span_id: ["weather_researcher"] * 2,
        }

    def test_missing_mcp_command_fails_with_a_fix(self):
        options = ClaudeAgentOptions(
            mcp_servers={"fs": {"command": "no-such-binary-xyz", "args": []}}
        )
        with pytest.raises(claude.AgentError, match="nodejs"):
            anyio.run(claude.run_turn, "hi", options, "c")

    def test_no_model_fails_with_a_fix_before_calling_the_model(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_MODEL")
        # Turns always go to Gemini platform, so an API key names no model.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        query = MagicMock()
        with patch.object(claude.claude_agent_sdk, "query", query):
            with pytest.raises(claude.AgentError, match="model="):
                anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "c")
        query.assert_not_called()

    @pytest.mark.parametrize(
        "options",
        [
            ClaudeAgentOptions(model="claude-haiku-4-5"),
            ClaudeAgentOptions(env={"ANTHROPIC_MODEL": "claude-haiku-4-5"}),
            # Settings files may name the model.
            ClaudeAgentOptions(setting_sources=["project"]),
        ],
    )
    def test_a_model_from_elsewhere_runs(self, monkeypatch, options):
        monkeypatch.delenv("ANTHROPIC_MODEL")
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            reply = anyio.run(claude.run_turn, "hi", options, "c")
        assert reply.text == "It's foggy."

    def test_project_settings_may_name_the_model(self, monkeypatch, isolated):
        monkeypatch.delenv("ANTHROPIC_MODEL")
        (isolated / ".claude").mkdir()
        (isolated / ".claude" / "settings.json").write_text('{"model": "m"}')
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            reply = anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "c")
        assert reply.text == "It's foggy."

    @pytest.mark.parametrize("setting_sources", [None, ["project"]])
    def test_missing_project_mcp_command_fails_with_a_fix(
        self, isolated, setting_sources
    ):
        (isolated / ".mcp.json").write_text(
            '{"mcpServers": {"fs": {"command": "no-such-binary-xyz"}}}'
        )
        options = ClaudeAgentOptions(setting_sources=setting_sources)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            with pytest.raises(
                claude.AgentError, match="'fs' runs 'no-such-binary-xyz'"
            ):
                anyio.run(claude.run_turn, "hi", options, "c")

    @pytest.mark.parametrize(
        ("setting_sources", "command"),
        [
            # The CLI doesn't read .mcp.json without the project source.
            (["user"], "no-such-binary-xyz"),
            # The CLI expands variables in commands itself.
            (None, "${NO_SUCH_DIR}/no-such-binary-xyz"),
        ],
    )
    def test_project_mcp_commands_left_to_the_cli_are_not_checked(
        self, isolated, setting_sources, command
    ):
        (isolated / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"fs": {"command": command}}})
        )
        options = ClaudeAgentOptions(setting_sources=setting_sources)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            assert anyio.run(claude.run_turn, "hi", options, "c").text == "It's foggy."

    @pytest.mark.parametrize("content", ["{not json", "[]"])
    def test_malformed_project_mcp_json_fails_with_a_fix(self, isolated, content):
        (isolated / ".mcp.json").write_text(content)
        with patch.object(claude.claude_agent_sdk, "query", _fake_query(_TOOL_TURN)):
            with pytest.raises(claude.AgentError, match=r"\.mcp\.json is not"):
                anyio.run(claude.run_turn, "hi", ClaudeAgentOptions(), "c")


class TestTurnOptions:
    def test_conversation_starts_then_resumes_its_session(self):
        first = _options_for(ClaudeAgentOptions(), "ctx-1")
        _write_local_transcript("ctx-1")
        later = _options_for(ClaudeAgentOptions(), "ctx-1")

        session = turn_options.session_id_for("ctx-1")
        assert (first.session_id, first.resume) == (session, None)
        assert (later.session_id, later.resume) == (None, session)
        # Same conversation, same workspace: that is where the CLI keys it.
        assert first.cwd == later.cwd == str(_workspaces() / session)
        assert first.include_partial_messages is True

    def test_a_uuid_conversation_id_is_the_session_id(self):
        # A2A clients send UUID context ids: the caller, the CLI and the spans
        # then all see the same id.
        context_id = "3e3a0114-1fa0-463c-a119-b599a1beb275"
        assert _options_for(ClaudeAgentOptions(), context_id).session_id == context_id
        # Any other spelling, or a non-UUID id, gets a stable derived UUID.
        for other in (context_id.upper(), "ctx-1"):
            session = _options_for(ClaudeAgentOptions(), other).session_id
            assert session != other
            assert str(uuid.UUID(session)) == session
            assert _options_for(ClaudeAgentOptions(), other).session_id == session

    def test_prompts_are_delivered_verbatim(self):
        # Remote callers write the prompt: no "@/path" file reads, no slash commands.
        assert _options_for(ClaudeAgentOptions(), "c").verbatim_prompts is True

    def test_agent_running_in_the_project_root_needs_no_copy(self, isolated):
        (isolated / "CLAUDE.md").write_text("Be terse.")
        options = _options_for(ClaudeAgentOptions(cwd=str(isolated)), "c")
        assert options.cwd == str(isolated)
        assert options.setting_sources == ["project"]

    def test_an_agents_own_directory_keeps_its_claude_settings(
        self, isolated, tmp_path
    ):
        (isolated / ".claude").mkdir()
        (isolated / ".claude" / "settings.json").write_text("{}")
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude" / "settings.json").write_text('{"model": "repo"}')
        (repo / "CLAUDE.md").write_text("The repo's notes.")
        (repo / ".mcp.json").write_text('{"mcpServers": {}}')

        options = _options_for(ClaudeAgentOptions(cwd=str(repo)), "c")

        # Nothing is copied over or deleted in a directory the agent points at.
        assert (repo / ".claude" / "settings.json").read_text() == '{"model": "repo"}'
        assert (repo / "CLAUDE.md").read_text() == "The repo's notes."
        assert (repo / ".mcp.json").exists()
        assert options.setting_sources == []

    def test_conversations_get_their_own_workspace(self):
        a = _options_for(ClaudeAgentOptions(), "ctx-a")
        b = _options_for(ClaudeAgentOptions(), "ctx-b")
        assert a.cwd != b.cwd

    def test_server_defaults_apply_only_where_unset(self, tmp_path):
        defaults = _options_for(ClaudeAgentOptions(), "c")
        assert defaults.permission_mode == "dontAsk"
        assert defaults.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert defaults.env["ENABLE_TOOL_SEARCH"] == "false"
        assert defaults.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        # Without project settings to copy, the CLI loads none.
        assert defaults.setting_sources == []

        own = _options_for(
            ClaudeAgentOptions(
                permission_mode="default",
                cwd=str(tmp_path / "own"),
                env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"},
                setting_sources=["user"],
            ),
            "c",
        )
        assert own.permission_mode == "default"
        assert own.cwd == str(tmp_path / "own")
        assert own.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "0"
        assert own.setting_sources == ["user"]

    def test_own_permission_callback_is_not_shadowed(self):
        async def allow(tool_name, tool_input, context):
            return None

        options = _options_for(ClaudeAgentOptions(can_use_tool=allow), "c")
        assert options.permission_mode is None

    @pytest.mark.parametrize(
        ("options", "bare"),
        [
            (ClaudeAgentOptions(tools=[]), True),
            (ClaudeAgentOptions(), False),
            (ClaudeAgentOptions(tools=["WebSearch"]), False),
            (ClaudeAgentOptions(tools=[], setting_sources=["user"]), False),
            (ClaudeAgentOptions(tools=[], skills="all"), False),
            (
                ClaudeAgentOptions(tools=[], plugins=[{"type": "local", "path": "p"}]),
                False,
            ),
            (
                ClaudeAgentOptions(tools=[], hooks={"PreToolUse": [HookMatcher()]}),
                False,
            ),
            (ClaudeAgentOptions(tools=[], env={"CLAUDE_CODE_SIMPLE": "0"}), False),
        ],
    )
    def test_bare_mode_only_for_agents_that_lose_nothing_to_it(self, options, bare):
        # Bare mode drops the reminders that make the model refuse plain
        # instructions, but also built-in tools, settings, skills, plugins and hooks.
        env = _options_for(options, "c").env
        assert (env.get("CLAUDE_CODE_SIMPLE") == "1") == bare

    def test_project_settings_rule_out_bare_mode(self, isolated):
        (isolated / "CLAUDE.md").write_text("Be terse.")
        options = _options_for(ClaudeAgentOptions(tools=[]), "c")
        assert "CLAUDE_CODE_SIMPLE" not in options.env

    def test_project_claude_settings_ship_with_the_agent(self, isolated):
        (isolated / ".claude" / "agents").mkdir(parents=True)
        (isolated / ".claude" / "agents" / "reviewer.md").write_text("---\n---\n")
        (isolated / "CLAUDE.md").write_text("Always answer in French.")

        options = _options_for(ClaudeAgentOptions(), "ctx-1")

        workspace = _workspaces() / turn_options.session_id_for("ctx-1")
        assert options.setting_sources == ["project"]
        assert (workspace / "CLAUDE.md").read_text() == "Always answer in French."
        assert (workspace / ".claude" / "agents" / "reviewer.md").is_file()

    def test_project_mcp_servers_ship_with_the_agent(self, isolated):
        (isolated / ".mcp.json").write_text('{"mcpServers": {}}')

        options = _options_for(ClaudeAgentOptions(), "ctx-1")

        workspace = _workspaces() / turn_options.session_id_for("ctx-1")
        assert options.setting_sources == ["project"]
        assert (workspace / ".mcp.json").read_text() == '{"mcpServers": {}}'

    def test_agents_that_load_project_settings_get_the_copy(self, isolated):
        (isolated / "CLAUDE.md").write_text("Be terse.")
        options = _options_for(ClaudeAgentOptions(setting_sources=["project"]), "c")
        assert (
            _workspaces() / turn_options.session_id_for("c") / "CLAUDE.md"
        ).is_file()
        assert options.setting_sources == ["project"]

    @pytest.mark.parametrize(
        ("setting_sources", "allowed"),
        [
            (None, ["mcp__w__get_weather", "Bash(touch:*)"]),
            (["project", "local"], ["mcp__w__get_weather", "Bash(touch:*)", "Read"]),
            ([], ["mcp__w__get_weather"]),
        ],
    )
    def test_project_permission_rules_apply_in_the_workspace(
        self, isolated, setting_sources, allowed
    ):
        # The CLI ignores allow rules from an untrusted workspace's settings,
        # and a fresh workspace is never trusted.
        (isolated / ".claude").mkdir()
        (isolated / ".claude" / "settings.json").write_text(
            '{"permissions": {"allow": ["Bash(touch:*)", "mcp__w__get_weather"]}}'
        )
        (isolated / ".claude" / "settings.local.json").write_text(
            '{"permissions": {"allow": ["Read"]}}'
        )
        options = _options_for(
            ClaudeAgentOptions(
                allowed_tools=["mcp__w__get_weather"], setting_sources=setting_sources
            ),
            "c",
        )
        assert options.allowed_tools == allowed

    def test_the_developers_own_claude_settings_are_not_loaded(self, isolated):
        # The project root's files belong to the developer's coding assistant.
        project = isolated.parent
        (project / ".claude").mkdir()
        (project / ".claude" / "settings.json").write_text(
            '{"permissions": {"allow": ["Bash(*)"]}}'
        )
        (project / "CLAUDE.md").write_text("You are a coding assistant.")

        options = _options_for(ClaudeAgentOptions(), "ctx-1")

        workspace = _workspaces() / turn_options.session_id_for("ctx-1")
        assert options.setting_sources == []
        assert options.allowed_tools == []
        assert not (workspace / "CLAUDE.md").exists()
        assert not (workspace / ".claude").exists()

    def test_settings_the_agent_writes_into_its_workspace_are_dropped(self, isolated):
        (isolated / ".claude" / "agents").mkdir(parents=True)
        (isolated / ".claude" / "agents" / "reviewer.md").write_text("---\n---\n")
        _options_for(ClaudeAgentOptions(), "ctx-1")
        # An agent that can write files adds settings and an MCP server to its
        # workspace, hoping the next turn loads them.
        workspace = _workspaces() / turn_options.session_id_for("ctx-1")
        (workspace / ".claude" / "settings.json").write_text(
            '{"permissions": {"allow": ["Bash(*)"]}, "hooks": {}}'
        )
        (workspace / ".mcp.json").write_text('{"mcpServers": {"x": {"command": "sh"}}}')

        options = _options_for(ClaudeAgentOptions(), "ctx-1")

        assert options.allowed_tools == []
        assert not (workspace / ".claude" / "settings.json").exists()
        assert not (workspace / ".mcp.json").exists()
        assert (workspace / ".claude" / "agents" / "reviewer.md").is_file()

    @pytest.mark.parametrize(
        "settings",
        ["{", "[]", '{"permissions": []}', '{"permissions": {"allow": "Bash"}}'],
    )
    def test_malformed_project_settings_add_no_rules(self, isolated, settings):
        (isolated / ".claude").mkdir()
        (isolated / ".claude" / "settings.json").write_text(settings)
        assert _options_for(ClaudeAgentOptions(), "c").allowed_tools == []

    def test_the_cli_does_not_get_the_cloud_sql_password(self, monkeypatch):
        from app.app_utils.session_store import PostgresSessionStore

        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "proj:us-east1:db")
        monkeypatch.setenv("DB_PASS", "secret")
        with patch.object(
            PostgresSessionStore, "exists", AsyncMock(return_value=False)
        ):
            options = _options_for(ClaudeAgentOptions(), "ctx-1")

        # The server's store has it; the CLI and its tools see it blanked.
        assert isinstance(options.session_store, PostgresSessionStore)
        assert options.env["DB_PASS"] == ""

    def test_the_cli_always_calls_gemini_platform(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-server")
        options = _options_for(
            ClaudeAgentOptions(
                env={
                    "ANTHROPIC_API_KEY": "sk-agent",
                    "ANTHROPIC_AUTH_TOKEN": "token",
                    "CLAUDE_CODE_USE_VERTEX": "0",
                }
            ),
            "ctx-1",
        )
        assert options.env["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert options.env["ANTHROPIC_API_KEY"] == ""
        assert options.env["ANTHROPIC_AUTH_TOKEN"] == ""

    def test_cloud_sql_env_gives_one_postgres_store_per_process(self, monkeypatch):
        from app.app_utils.session_store import PostgresSessionStore

        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "proj:us-east1:db")
        monkeypatch.setenv("DB_PASS", "secret")
        exists = AsyncMock(side_effect=[False, True])
        with patch.object(PostgresSessionStore, "exists", exists):
            first = _options_for(ClaudeAgentOptions(), "ctx-1")
            later = _options_for(ClaudeAgentOptions(), "ctx-1")

        assert isinstance(first.session_store, PostgresSessionStore)
        assert later.session_store is first.session_store
        session = turn_options.session_id_for("ctx-1")
        assert (first.session_id, later.resume) == (session, session)
        key = exists.call_args.args[0]
        assert key["session_id"] == session
        assert key["project_key"] == claude.claude_agent_sdk.project_key_for_directory(
            _workspaces() / session
        )

    def test_agents_own_store_wins_over_cloud_sql(self, monkeypatch):
        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "proj:us-east1:db")
        monkeypatch.setenv("DB_PASS", "secret")
        own = InMemorySessionStore()
        options = _options_for(ClaudeAgentOptions(session_store=own), "ctx-1")
        assert options.session_store is own
        assert options.session_id == turn_options.session_id_for("ctx-1")

    def test_resumes_from_an_agents_own_store(self):
        own = InMemorySessionStore()
        session = turn_options.session_id_for("ctx-1")
        project_key = claude.claude_agent_sdk.project_key_for_directory(
            _workspaces() / session
        )
        anyio.run(
            own.append,
            {"project_key": project_key, "session_id": session},
            [{"type": "user"}],
        )
        options = _options_for(ClaudeAgentOptions(session_store=own), "ctx-1")
        assert options.resume == session

    def test_another_agent_does_not_resume_the_conversation(self, monkeypatch):
        # Services running different agents can share a machine and a database.
        store = InMemorySessionStore()
        monkeypatch.setattr(settings, "AGENT_NAME", "agent_a")
        first = _options_for(ClaudeAgentOptions(session_store=store), "ctx-1")
        key = {
            "project_key": claude.claude_agent_sdk.project_key_for_directory(first.cwd),
            "session_id": first.session_id,
        }
        anyio.run(store.append, key, [{"type": "user"}])

        monkeypatch.setattr(settings, "AGENT_NAME", "agent_b")
        other = _options_for(ClaudeAgentOptions(session_store=store), "ctx-1")
        assert other.cwd != first.cwd
        assert other.resume is None
        monkeypatch.setattr(settings, "AGENT_NAME", "agent_a")
        again = _options_for(ClaudeAgentOptions(session_store=store), "ctx-1")
        assert again.resume == first.session_id

    def test_without_cloud_sql_there_is_no_store(self):
        assert _options_for(ClaudeAgentOptions(), "ctx-1").session_store is None


class TestCallerScopedConversations:
    """With an authenticated caller, a conversation is that caller's alone."""

    CONTEXT_ID = "3e3a0114-1fa0-463c-a119-b599a1beb275"

    def _turn(self, user_id: str | None, options=None) -> ClaudeAgentOptions:
        """One turn through stream_turn; the fake CLI saves its transcript."""
        seen: list[dict] = []

        async def query(*, prompt, options):
            seen.append(options)
            session = options.resume or options.session_id
            # Keyed by the CLI's working directory, on disk and in the store.
            project_key = claude.claude_agent_sdk.project_key_for_directory(options.cwd)
            transcripts = (
                pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / project_key
            )
            transcripts.mkdir(parents=True, exist_ok=True)
            (transcripts / f"{session}.jsonl").write_text('{"type": "user"}\n')
            if options.session_store is not None:
                key = {"project_key": project_key, "session_id": session}
                await options.session_store.append(key, [{"type": "user"}])
            yield _result(session_id=session)

        async def run() -> None:
            async for _ in claude.stream_turn(
                "hi", options or ClaudeAgentOptions(), self.CONTEXT_ID, user_id
            ):
                pass

        with patch.object(claude.claude_agent_sdk, "query", query):
            anyio.run(run)
        return seen[0]

    def test_another_caller_does_not_resume_the_conversation(self):
        alice = self._turn("alice@example.com")
        bob = self._turn("bob@example.com")
        assert alice.cwd != bob.cwd
        assert (bob.session_id, bob.resume) == (self.CONTEXT_ID, None)

    def test_the_same_caller_resumes_their_conversation(self):
        first = self._turn("alice@example.com")
        later = self._turn("alice@example.com")
        assert later.cwd == first.cwd
        assert (first.session_id, later.resume) == (self.CONTEXT_ID, self.CONTEXT_ID)

    def test_callers_are_kept_apart_in_the_session_store(self):
        store = InMemorySessionStore()
        self._turn("alice@example.com", ClaudeAgentOptions(session_store=store))
        # Only the store has the transcript now, as on another instance.
        shutil.rmtree(pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects")

        bob = self._turn("bob@example.com", ClaudeAgentOptions(session_store=store))
        back = self._turn("alice@example.com", ClaudeAgentOptions(session_store=store))
        assert bob.resume is None
        assert back.resume == self.CONTEXT_ID

    def test_without_a_caller_the_context_id_alone_keys_it(self):
        anyone = self._turn(None)
        again = self._turn(None)
        assert anyone.cwd == str(_workspaces() / self.CONTEXT_ID)
        assert (again.cwd, again.resume) == (anyone.cwd, self.CONTEXT_ID)
        # A caller doesn't pick up the unauthenticated conversation either.
        assert self._turn("alice@example.com").resume is None


class TestTextDelta:
    def test_top_level_text_delta(self):
        assert replies.text_delta(_delta("Hello")) == "Hello"

    def test_subagent_and_non_text_events_are_skipped(self):
        assert replies.text_delta(_delta("x", parent="t1")) == ""
        other = StreamEvent(uuid="u", session_id="s", event={"type": "message_start"})
        assert replies.text_delta(other) == ""
        assert replies.text_delta(_result()) == ""


class TestModelAccess:
    def test_defaults_to_vertex_with_the_given_project(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
        monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-env")

        claude.configure_model_access()

        import os

        assert os.environ["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert os.environ["CLOUD_ML_REGION"] == "global"
        assert os.environ["ANTHROPIC_VERTEX_PROJECT_ID"] == "from-env"

    def test_an_api_key_does_not_skip_vertex(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        claude.configure_model_access()

        import os

        assert os.environ["CLAUDE_CODE_USE_VERTEX"] == "1"

    def test_existing_values_win(self, monkeypatch):
        monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
        claude.configure_model_access()

        import os

        assert os.environ["CLOUD_ML_REGION"] == "us-east5"
        assert os.environ["ANTHROPIC_VERTEX_PROJECT_ID"] == "test-project"


class TestEvalAgentsMetadata:
    def test_declares_the_agents_mcp_tools(self):
        import importlib.util
        from pathlib import Path

        script = Path(__file__).resolve().parents[2] / "scripts" / "eval_generate.py"
        spec = importlib.util.spec_from_file_location("eval_generate", script)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        from claude_agent_sdk import create_sdk_mcp_server, tool

        @tool("get_weather", "Get the weather.", {"city": str})
        async def get_weather(args):
            return {"content": []}

        # Its own agent, so the test holds whatever app/agent.py declares.
        options = ClaudeAgentOptions(
            system_prompt="Be brief.",
            mcp_servers={"w": create_sdk_mcp_server(name="w", tools=[get_weather])},
            tools=["WebFetch"],
        )

        agents = mod.agents_metadata(options, "weather")

        declarations = agents["weather"]["tools"][0]["function_declarations"]
        assert [d["name"] for d in declarations] == ["mcp__w__get_weather", "WebFetch"]
        assert declarations[0]["parameters"]["properties"] == {
            "city": {"type": "string"}
        }
        assert agents["weather"]["instruction"] == "Be brief."

    def test_declares_the_agents_subagents(self):
        import importlib.util
        from pathlib import Path

        script = Path(__file__).resolve().parents[2] / "scripts" / "eval_generate.py"
        spec = importlib.util.spec_from_file_location("eval_generate", script)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        from claude_agent_sdk import AgentDefinition

        options = ClaudeAgentOptions(
            tools=["Agent", "WebFetch"],
            agents={
                "researcher": AgentDefinition(
                    description="Looks things up.", prompt="Search.", tools=["WebFetch"]
                ),
                "writer": AgentDefinition(description="Writes.", prompt="Write."),
            },
        )

        agents = mod.agents_metadata(options, "root")

        assert agents["root"]["sub_agents"] == ["researcher", "writer"]
        assert agents["researcher"] == {
            "agent_id": "researcher",
            "agent_type": "ClaudeAgent",
            "description": "Looks things up.",
            "instruction": "Search.",
            "tools": [{"function_declarations": [{"name": "WebFetch"}]}],
        }
        # Without its own tool list, a subagent inherits the agent's tools.
        assert agents["writer"]["tools"] == agents["root"]["tools"]


def _fake_pool() -> MagicMock:
    """An asyncpg pool whose table setup succeeds and whose queries find nothing."""
    conn = MagicMock(execute=AsyncMock())
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock(fetchval=AsyncMock(return_value=False))
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


_KEY = {"project_key": "p", "session_id": "s"}


class TestPostgresSessionStoreConnection:
    @patch("asyncpg.create_pool", new_callable=AsyncMock)
    def test_password_connects_over_the_cloudsql_socket(self, create_pool, monkeypatch):
        from app.app_utils.session_store import PostgresSessionStore

        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "proj:us-east1:db")
        monkeypatch.setenv("DB_PASS", "secret")
        monkeypatch.setenv("DB_NAME", "sessions")
        create_pool.return_value = _fake_pool()

        anyio.run(PostgresSessionStore.from_env().exists, _KEY)

        assert create_pool.call_args.kwargs == {
            "min_size": 1,
            "max_size": 5,
            "host": "/cloudsql/proj:us-east1:db",
            "user": "postgres",
            "password": "secret",
            "database": "sessions",
        }

    def test_needs_a_password(self, monkeypatch):
        from app.app_utils.session_store import PostgresSessionStore

        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "proj:us-east1:db")
        assert PostgresSessionStore.from_env() is None


@pytest.mark.skipif(
    not os.environ.get("SESSION_STORE_POSTGRES_URL"),
    reason="set SESSION_STORE_POSTGRES_URL to a Postgres database to run",
)
class TestPostgresSessionStore:
    @pytest.fixture
    def make_store(self):
        """Stores on fresh tables, dropped afterwards."""
        import asyncpg

        from app.app_utils.session_store import PostgresSessionStore

        dsn = os.environ["SESSION_STORE_POSTGRES_URL"]
        tables: list[str] = []

        def make() -> PostgresSessionStore:
            tables.append(f"test_session_store_{os.getpid()}_{len(tables)}")
            return PostgresSessionStore(tables[-1], dsn=dsn)

        yield make

        async def drop() -> None:
            conn = await asyncpg.connect(dsn)
            for table in tables:
                await conn.execute(f"DROP TABLE IF EXISTS {table}")
            await conn.close()

        anyio.run(drop)

    def test_meets_the_sdk_session_store_contract(self, make_store):
        from claude_agent_sdk.testing.session_store_conformance import (
            run_session_store_conformance,
        )

        # Each contract gets its own table, so they don't see each other's rows.
        anyio.run(run_session_store_conformance, make_store)

    def test_exists_without_loading(self, make_store):
        store = make_store()
        main = {"project_key": "p", "session_id": "s"}
        sub = {**main, "subpath": "subagents/agent-1"}

        async def run() -> list[bool]:
            before = await store.exists(main)
            await store.append(sub, [{"type": "user"}])
            only_sub = await store.exists(main)
            await store.append(main, [{"type": "user"}])
            return [before, only_sub, await store.exists(main)]

        assert anyio.run(run) == [False, False, True]
