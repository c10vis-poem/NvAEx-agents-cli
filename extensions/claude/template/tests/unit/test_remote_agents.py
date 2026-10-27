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

"""`remote_agent` against a real a2a-sdk server served in process over httpx."""

from __future__ import annotations

import functools
import logging
import ssl
from typing import ClassVar
from unittest.mock import patch

import anyio
import google.oauth2.credentials
import httpx
import pytest
from a2a.helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Part,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, PROTOCOL_VERSION_1_0
from claude_agent_sdk import ClaudeAgentOptions
from fastapi import FastAPI
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from opentelemetry.sdk.trace import TracerProvider

from app.app_utils import claude
from app.app_utils.conversation import current_conversation
from app.app_utils.remote_agents import remote_agent

URL = "https://callee-xyz.a.run.app/a2a/app"
# Not Google-hosted, so no token by default.
THIRD_PARTY_URL = "https://callee.example/a2a/app"
# An Agent Runtime agent's /api passthrough, which serves the app's routes.
AGENT_RUNTIME_API = (
    "/reasoningEngines/v1/projects/p/locations/us-central1/reasoningEngines/1/api"
)
AGENT_RUNTIME_URL = (
    f"https://us-central1-aiplatform.googleapis.com{AGENT_RUNTIME_API}/a2a/app"
)


class FakeAgent(AgentExecutor):
    """Replies "It's sunny." in two streamed chunks; fails on "fail"."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.requests.append(
            (context.context_id, context.call_context.state["headers"])
        )
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()
        if context.get_user_input() == "fail":
            await updater.failed(
                updater.new_agent_message([Part(text="out of credits")])
            )
            return
        for i, text in enumerate(["It's ", "sunny."]):
            await updater.add_artifact(
                [Part(text=text)],
                artifact_id="a",
                name="response",
                append=i > 0,
                last_chunk=False,
            )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


class RecordingTransport(httpx.AsyncBaseTransport):
    """Serves `app` in process and records every request's URL and headers."""

    def __init__(self, app: FastAPI) -> None:
        self._asgi = httpx.ASGITransport(app=app)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._asgi.handle_async_request(request)


def _serve(monkeypatch, prefix: str = "") -> FakeAgent:
    """Serve a FakeAgent's A2A routes under `prefix` to every httpx client."""
    agent = FakeAgent()
    # The card sends clients to another host: requests must stay on URL's.
    card = AgentCard(
        name="callee",
        description="Weather.",
        supported_interfaces=[
            AgentInterface(
                url=f"https://elsewhere.example{prefix}/a2a/app",
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION_1_0,
            )
        ],
        version="1",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[
            AgentSkill(
                id="weather", name="weather", description="Weather.", tags=["weather"]
            )
        ],
    )
    app = FastAPI()
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(
            card, card_url=f"/a2a/app{AGENT_CARD_WELL_KNOWN_PATH}"
        ),
        jsonrpc_routes=create_jsonrpc_routes(
            DefaultRequestHandler(
                agent_executor=agent, task_store=InMemoryTaskStore(), agent_card=card
            ),
            rpc_url="/a2a/app",
        ),
    )
    if prefix:
        outer = FastAPI()
        outer.mount(prefix, app)
        app = outer
    agent.transport = RecordingTransport(app)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=agent.transport),
    )
    return agent


@pytest.fixture(autouse=True)
def _no_client_certificate(monkeypatch):
    """Keep a workstation's client certificate (and mTLS) out of the tests."""
    monkeypatch.setenv("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")


@pytest.fixture
def agent(monkeypatch):
    return _serve(monkeypatch)


def ask(server, message: str, conversation: str = "conv-1"):
    async def call():
        token = current_conversation.set(conversation)
        try:
            instance = server["instance"]
            async with create_client_server_memory_streams() as (client, served):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(
                        instance.run, *served, instance.create_initialization_options()
                    )
                    async with ClientSession(*client) as session:
                        await session.initialize()
                        result = await session.call_tool("ask", {"message": message})
                    tg.cancel_scope.cancel()
            return result
        finally:
            current_conversation.reset(token)

    return anyio.run(call)


def text_of(result) -> str:
    return "".join(block.text for block in result.content)


def is_error(result) -> bool:
    # The field is isError on mcp 1.x and is_error on 2.x; both dump to isError.
    return result.model_dump(by_alias=True)["isError"]


# A service account key: the token comes from fetch_id_token.
@patch("google.auth.default", return_value=(object(), "test-project"))
@patch("google.oauth2.id_token.fetch_id_token", return_value="id-token")
class TestRemoteAgent:
    def test_returns_the_remote_reply(self, fetch_id_token, default, agent):
        result = ask(remote_agent("weather", URL), "Weather in Paris?")
        assert not is_error(result)
        assert text_of(result) == "It's sunny."

    def test_failed_task_is_a_tool_error_with_the_reason(
        self, fetch_id_token, default, agent
    ):
        result = ask(remote_agent("weather", URL), "fail")
        assert is_error(result)
        assert "failed: out of credits" in text_of(result)

    def test_unreachable_agent_is_a_tool_error(
        self, fetch_id_token, default, monkeypatch
    ):
        def refuse(request):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(refuse)),
        )
        result = ask(remote_agent("weather", URL), "hi")
        assert is_error(result)
        assert "connection refused" in text_of(result)

    @pytest.mark.parametrize("on_agent_runtime", [True, False])
    def test_401_from_agent_runtime_names_bind_id_token(
        self, fetch_id_token, default, monkeypatch, on_agent_runtime
    ):
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            functools.partial(
                httpx.AsyncClient,
                transport=httpx.MockTransport(lambda request: httpx.Response(401)),
            ),
        )
        if on_agent_runtime:
            monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")
        else:
            monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)
        result = ask(remote_agent("weather", URL), "hi")
        assert is_error(result)
        assert ("bind_id_token=False" in text_of(result)) is on_agent_runtime

    def test_id_token_goes_only_to_the_declared_origin(
        self, fetch_id_token, default, agent
    ):
        ask(remote_agent("weather", URL), "hi")
        fetch_id_token.assert_called_once_with(
            fetch_id_token.call_args.args[0], "https://callee-xyz.a.run.app"
        )
        hosts = {r.url.host for r in agent.transport.requests}
        assert hosts == {"callee-xyz.a.run.app"}
        assert all(
            r.headers["authorization"] == "Bearer id-token"
            for r in agent.transport.requests
        )

    def test_auth_opt_out_and_custom_headers(self, fetch_id_token, default, agent):
        ask(remote_agent("weather", URL, auth=False, headers={"X-Key": "k"}), "hi")
        fetch_id_token.assert_not_called()
        request = agent.transport.requests[-1]
        assert "authorization" not in request.headers
        assert request.headers["x-key"] == "k"

    @pytest.mark.parametrize(
        "url",
        [
            THIRD_PARTY_URL,
            # run.app in the host's middle, or over plain http.
            "https://callee.run.app.evil.example/a2a/app",
            "http://callee-xyz.a.run.app/a2a/app",
        ],
    )
    def test_no_token_by_default_to_a_host_not_google_hosted(
        self, fetch_id_token, default, agent, url
    ):
        result = ask(remote_agent("weather", url), "hi")
        assert text_of(result) == "It's sunny."
        default.assert_not_called()
        fetch_id_token.assert_not_called()
        assert all("authorization" not in r.headers for r in agent.transport.requests)

    def test_auth_true_sends_a_token_to_any_host(
        self, fetch_id_token, default, agent, caplog
    ):
        with caplog.at_level(logging.WARNING):
            ask(remote_agent("weather", THIRD_PARTY_URL, auth=True), "hi")
        fetch_id_token.assert_called_once_with(
            fetch_id_token.call_args.args[0], "https://callee.example"
        )
        _, headers = agent.requests[-1]
        assert headers["authorization"] == "Bearer id-token"
        # A service account key's token is bound to the host: no warning.
        assert not caplog.records

    def test_follow_ups_reuse_the_remote_context(self, fetch_id_token, default, agent):
        server = remote_agent("weather", URL)
        ask(server, "first", conversation="conv-1")
        ask(server, "follow-up", conversation="conv-1")
        ask(server, "other", conversation="conv-2")
        first, follow_up, other = (context for context, _ in agent.requests)
        assert follow_up == first
        assert other != first

    def test_does_not_carry_the_trace_context(self, fetch_id_token, default, agent):
        # The remote agent records a trace of its own.
        tracer = TracerProvider().get_tracer(__name__)
        with tracer.start_as_current_span("turn"):
            ask(remote_agent("weather", URL), "hi")
        _, headers = agent.requests[-1]
        assert "traceparent" not in headers


class FakeMetadataIDToken:
    """compute_engine.IDTokenCredentials, recording how it was asked for."""

    created: ClassVar[list[tuple[str, dict]]] = []

    def __init__(self, request, target_audience, **kwargs) -> None:
        self.created.append((target_audience, kwargs))
        self.token: str | None = None

    def refresh(self, request) -> None:
        self.token = "metadata-id-token"


@pytest.mark.parametrize("bind", [True, False])
@patch("google.auth.compute_engine.IDTokenCredentials", FakeMetadataIDToken)
def test_metadata_server_id_token_binding_follows_bind_id_token(agent, bind):
    # With Agent Identity, google-auth binds ID tokens to the agent's certificate,
    # which Cloud Run rejects, so callers to Cloud Run turn binding off.
    from google.auth import compute_engine

    FakeMetadataIDToken.created.clear()
    with patch("google.auth.default", return_value=(compute_engine.Credentials(), "p")):
        result = ask(remote_agent("weather", URL, bind_id_token=bind), "hi")

    assert text_of(result) == "It's sunny."
    audience, kwargs = FakeMetadataIDToken.created[0]
    assert audience == "https://callee-xyz.a.run.app"
    assert kwargs == {"use_metadata_identity_endpoint": True, "bind_id_token": bind}
    _, headers = agent.requests[-1]
    assert headers["authorization"] == "Bearer metadata-id-token"


class FakeUserCredentials(google.oauth2.credentials.Credentials):
    """`gcloud auth application-default login` credentials and their ID token."""

    def refresh(self, request) -> None:
        self._id_token = "user-id-token"


class TestUserCredentials:
    @patch("google.auth.default", return_value=(FakeUserCredentials(None), "p"))
    def test_auth_true_to_another_host_warns_the_token_is_unbound(
        self, default, agent, caplog
    ):
        server = remote_agent("weather", THIRD_PARTY_URL, auth=True)
        with caplog.at_level(logging.WARNING):
            ask(server, "hi")
            ask(server, "again")
        assert all(
            r.headers["authorization"] == "Bearer user-id-token"
            for r in agent.transport.requests
        )
        (record,) = caplog.records
        assert "callee.example" in record.getMessage()
        assert "isn't bound" in record.getMessage()

    @patch("google.auth.default", return_value=(FakeUserCredentials(None), "p"))
    def test_cloud_run_by_default_without_a_warning(self, default, agent, caplog):
        with caplog.at_level(logging.WARNING):
            ask(remote_agent("weather", URL), "hi")
        _, headers = agent.requests[-1]
        assert headers["authorization"] == "Bearer user-id-token"
        assert not caplog.records


class FakeCredentials:
    """ADC that hand out a new access token on each refresh."""

    def __init__(self) -> None:
        self.valid = False
        self.token: str | None = None
        self.refreshes = 0

    def refresh(self, request) -> None:
        self.refreshes += 1
        self.token = f"access-token-{self.refreshes}"
        self.valid = True


@patch("google.oauth2.id_token.fetch_id_token", return_value="id-token")
class TestAgentRuntimeAgent:
    def test_sends_a_fresh_access_token_to_the_passthrough(
        self, fetch_id_token, monkeypatch
    ):
        agent = _serve(monkeypatch, AGENT_RUNTIME_API)
        credentials = FakeCredentials()
        tracer = TracerProvider().get_tracer(__name__)
        with patch("google.auth.default", return_value=(credentials, "p")) as default:
            server = remote_agent("weather", AGENT_RUNTIME_URL)
            with tracer.start_as_current_span("turn"):
                assert text_of(ask(server, "hi")) == "It's sunny."
            # An expired token is refreshed, not reused.
            credentials.valid = False
            ask(server, "again")

        fetch_id_token.assert_not_called()
        assert default.call_args.kwargs["scopes"] == [
            "https://www.googleapis.com/auth/cloud-platform"
        ]
        first, second = agent.requests
        assert "traceparent" not in first[1]
        assert first[1]["authorization"] == "Bearer access-token-1"
        assert second[1]["authorization"] == "Bearer access-token-2"
        # The card's off-origin endpoint is pinned to the passthrough host.
        assert {r.url.host for r in agent.transport.requests} == {
            "us-central1-aiplatform.googleapis.com"
        }

    def test_goes_over_mtls_with_an_agent_identity_certificate(
        self, fetch_id_token, monkeypatch
    ):
        agent = _serve(monkeypatch, AGENT_RUNTIME_API)
        context = ssl.create_default_context()
        clients: list[dict] = []
        serve = httpx.AsyncClient  # already bound to the in-process transport

        def client(**kwargs):
            clients.append(kwargs)
            return serve(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", client)
        with (
            patch("google.auth.default", return_value=(FakeCredentials(), "p")),
            patch(
                "google.auth.transport.mtls.get_default_ssl_context",
                return_value=context,
            ),
        ):
            result = ask(remote_agent("weather", AGENT_RUNTIME_URL), "hi")

        assert text_of(result) == "It's sunny."
        assert clients[0]["verify"] is context
        assert {r.url.host for r in agent.transport.requests} == {
            "us-central1-aiplatform.mtls.googleapis.com"
        }
        assert all(
            r.headers["authorization"] == "Bearer access-token-1"
            for r in agent.transport.requests
        )

    def test_resource_url_without_the_passthrough_is_rejected(self, fetch_id_token):
        resource = (
            "https://us-central1-aiplatform.googleapis.com/v1/projects/p/"
            "locations/us-central1/reasoningEngines/1"
        )
        with pytest.raises(ValueError, match="passthrough URL"):
            remote_agent("weather", resource)

    def test_an_access_token_never_goes_to_another_host(
        self, fetch_id_token, monkeypatch
    ):
        # Agent Runtime's host and path, in the path of some other host.
        prefix = "/us-central1-aiplatform.googleapis.com/reasoningEngines/1/api"
        agent = _serve(monkeypatch, prefix)
        lookalike = f"https://callee-xyz.a.run.app{prefix}/a2a/app"
        with patch("google.auth.default", return_value=(object(), "p")):
            ask(remote_agent("weather", lookalike), "hi")
        fetch_id_token.assert_called_once()
        _, headers = agent.requests[-1]
        assert headers["authorization"] == "Bearer id-token"


def test_tools_see_the_turns_conversation(monkeypatch, tmp_path):
    """Two callers sending the same context id don't share a remote conversation."""
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    monkeypatch.setattr(claude, "load_dotenv", lambda: None)  # keep .env out
    seen = []

    async def query(prompt, options):
        seen.append(current_conversation.get())
        return
        yield

    async def turn(user_id=None):
        async for _ in claude.stream_turn(
            "hi",
            ClaudeAgentOptions(model="claude-test", cwd=str(tmp_path)),
            "c-9",
            user_id,
        ):
            pass

    with patch("claude_agent_sdk.query", query):
        anyio.run(turn)
        anyio.run(turn, "ana")
        anyio.run(turn, "ben")
        anyio.run(turn, "ana")
    assert seen[0] == "c-9"
    assert seen[1] == seen[3] != seen[2]
    assert current_conversation.get() is None
