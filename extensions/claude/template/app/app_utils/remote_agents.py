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

"""Call other agents over A2A, as a tool the Claude agent can use."""

from __future__ import annotations

import logging
import os
import ssl
import uuid
import weakref
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from typing import Any

import anyio
import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
    TaskState,
)
from a2a.utils.constants import TransportProtocol
from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from app.app_utils.conversation import current_conversation
from app.app_utils.remote_auth import (
    PinnedHeaders,
    access_token,
    cloud_credentials,
    id_token,
    is_agent_runtime,
    is_cloud_run,
    mtls_target,
    origin,
    pin_to_origin,
)

# Remote conversations kept per remote agent; least recently used are dropped.
MAX_CONVERSATIONS = 1000

# Each remote_agent server's URL, keyed by its MCP server instance.
_urls: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()

_FAILED_STATES = {
    TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_REJECTED: "rejected",
    TaskState.TASK_STATE_CANCELED: "was canceled",
    TaskState.TASK_STATE_AUTH_REQUIRED: "requires authentication",
}


def remote_agent(
    name: str,
    url: str,
    *,
    description: str | None = None,
    auth: bool | None = None,
    bind_id_token: bool = True,
    headers: Mapping[str, str] | None = None,
    timeout: float = 300.0,
) -> McpSdkServerConfig:
    """An MCP server whose `ask` tool sends a message to the A2A agent at `url`.

    Follow-up calls in a conversation continue the remote agent's conversation.

        billing = remote_agent("billing", "https://billing-xyz.a.run.app/a2a/app")
        root_agent = ClaudeAgentOptions(
            mcp_servers={"billing": billing},
            allowed_tools=["mcp__billing__ask"],
            ...
        )

    Args:
        name: The server name; the tool is `mcp__<name>__ask`.
        url: Where the agent card is served, e.g. `https://<service>/a2a/app`.
            For an Agent Runtime agent, its `/api` passthrough URL plus that
            path: `https://<location>-aiplatform.googleapis.com/reasoningEngines/
            v1/projects/<project>/locations/<location>/reasoningEngines/<id>/api/
            a2a/app`.
        description: Tells the model when to call the remote agent.
        auth: None (default) sends a token only to Google-hosted agents: a
            Google ID token to Cloud Run (`*.run.app`; the caller needs
            roles/run.invoker) or an OAuth access token to Agent Runtime (the
            caller needs roles/aiplatform.user). True sends an ID token to any
            host; with gcloud user credentials that token can call any Cloud Run
            service you can, so use a service account for untrusted hosts.
            False sends no token.
        bind_id_token: Set False when an Agent Runtime agent calls a Cloud Run
            agent: Cloud Run rejects ID tokens bound to Agent Identity (401).
        headers: Extra headers for every request; `Authorization` replaces the
            token.
        timeout: Seconds to wait for the remote agent's reply.
    """
    if is_agent_runtime(url) and "/api" not in httpx.URL(url).path:
        raise ValueError(
            f"{url} is an Agent Runtime resource URL, which serves no A2A. Use "
            "the agent's passthrough URL: https://<location>-aiplatform."
            "googleapis.com/reasoningEngines/v1/projects/<project>/locations/"
            "<location>/reasoningEngines/<id>/api, plus the agent's A2A path "
            "(/a2a/app for an agent from this template)."
        )
    remote = _RemoteAgent(
        name,
        url,
        auth=auth,
        bind_id_token=bind_id_token,
        headers=headers or {},
        timeout=timeout,
    )

    @tool(
        "ask",
        description
        or f"Send a message to the {name} agent and return its reply. "
        "Follow-up messages continue the same conversation with it.",
        {"message": str},
    )
    async def ask(args: dict[str, Any]) -> dict[str, Any]:
        # Failures go back to the model as the tool result instead of ending
        # the turn.
        try:
            with anyio.fail_after(timeout):
                reply = await remote.send(args["message"])
        except TimeoutError:
            return _error(f"The {name} agent did not reply within {timeout:g}s.")
        except Exception as e:
            message = f"Calling the {name} agent failed: {e}"
            if (
                bind_id_token
                and not remote.agent_runtime
                and os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID")
                and "401" in str(e)
            ):
                # Likely Cloud Run rejecting a certificate-bound ID token.
                message += (
                    " From Agent Runtime, a Cloud Run agent needs "
                    f'remote_agent("{name}", ..., bind_id_token=False).'
                )
                logging.warning("%s", message)
            return _error(message)
        return {"content": [{"type": "text", "text": reply or "(empty reply)"}]}

    server = create_sdk_mcp_server(name=name, tools=[ask])
    _urls[server["instance"]] = url
    return server


def remote_agent_url(config: Mapping[str, Any]) -> str | None:
    """The remote agent's URL if `config` is a remote_agent server, else None.

    Lets BigQuery Agent Analytics record its tool as an A2A call.
    """
    instance = config.get("instance")
    try:
        return _urls.get(instance)
    except TypeError:  # not weak-referenceable, so not one of ours
        return None


class RemoteAgentError(RuntimeError):
    """The remote agent ended its task without a reply."""


class _RemoteAgent:
    """One remote agent's settings, and what it keeps between calls.

    Keeps the resolved agent card, Google credentials, and the remote context id
    of each local conversation.
    """

    def __init__(
        self,
        name: str,
        url: str,
        *,
        auth: bool | None,
        bind_id_token: bool,
        headers: Mapping[str, str],
        timeout: float,
    ) -> None:
        self.name = name
        self.url = url
        self.agent_runtime = is_agent_runtime(url)
        self._google_hosted = self.agent_runtime or is_cloud_run(url)
        self._send_token = self._google_hosted if auth is None else auth
        self._audience = str(origin(url)).rstrip("/")
        self._bind_id_token = bind_id_token
        self._headers = headers
        self._timeout = timeout
        self._card: AgentCard | None = None
        self._credentials: Any = None
        self._warned = False
        self._contexts: OrderedDict[str, str] = OrderedDict()

    async def send(self, message: str) -> str:
        """Send `message` to the remote agent and return its reply."""
        target, verify = await self._target()
        headers = await self._request_headers()
        conversation = current_conversation.get()
        async with httpx.AsyncClient(
            auth=PinnedHeaders(headers, origin(target)),
            timeout=self._timeout,
            verify=verify,
        ) as http_client:
            client = await self._client(http_client, target)
            request = self._request(message, conversation)
            reply, context_id = await _read_reply(
                client.send_message(request), self.name
            )
        if conversation and context_id:
            self._remember(conversation, context_id)
        return reply

    async def _target(self) -> tuple[str, ssl.SSLContext | bool]:
        """The URL to call and how to verify its TLS certificate."""
        if self.agent_runtime:
            return await anyio.to_thread.run_sync(mtls_target, self.url)
        return self.url, True

    async def _request_headers(self) -> dict[str, str]:
        headers = dict(self._headers)
        if self._send_token and "authorization" not in {k.lower() for k in headers}:
            headers["Authorization"] = f"Bearer {await self._token()}"
        return headers

    async def _token(self) -> str:
        """An access token for Agent Runtime, else an ID token for the host."""
        if self.agent_runtime:
            self._credentials = self._credentials or await anyio.to_thread.run_sync(
                cloud_credentials
            )
            return await anyio.to_thread.run_sync(access_token, self._credentials)
        token, unbound = await anyio.to_thread.run_sync(
            id_token, self._audience, self._bind_id_token
        )
        if unbound and not self._google_hosted and not self._warned:
            logging.warning(
                "remote_agent %r sends your gcloud user ID token to %s, "
                "which isn't bound to that host: it can call any Cloud "
                "Run service you can. Use a service account or "
                "impersonation for a host you don't trust.",
                self.name,
                httpx.URL(self.url).host,
            )
            self._warned = True
        return token

    async def _client(self, http_client: httpx.AsyncClient, target: str) -> Client:
        """An A2A client for the agent card, resolved on the first call."""
        if self._card is None:
            card = await A2ACardResolver(http_client, target).get_agent_card()
            pin_to_origin(card, target)
            self._card = card
        return ClientFactory(
            ClientConfig(
                httpx_client=http_client,
                supported_protocol_bindings=[
                    TransportProtocol.JSONRPC,
                    TransportProtocol.HTTP_JSON,
                ],
            )
        ).create(self._card)

    def _request(self, message: str, conversation: str | None) -> SendMessageRequest:
        """`message` in the remote conversation that continues `conversation`."""
        return SendMessageRequest(
            message=Message(
                message_id=uuid.uuid4().hex,
                role=Role.ROLE_USER,
                parts=[Part(text=message)],
                context_id=self._contexts.get(conversation, "") if conversation else "",
            )
        )

    def _remember(self, conversation: str, context_id: str) -> None:
        self._contexts[conversation] = context_id
        self._contexts.move_to_end(conversation)
        while len(self._contexts) > MAX_CONVERSATIONS:
            self._contexts.popitem(last=False)


async def _read_reply(
    chunks: AsyncIterator[StreamResponse], name: str
) -> tuple[str, str]:
    """The reply and remote context id from the streamed response.

    The reply is the task's text artifacts, else the last message.

    Raises:
        RemoteAgentError: The task ended in one of `_FAILED_STATES`.
    """
    artifacts: dict[str, str] = {}
    reply = ""
    context_id = ""
    async for chunk in chunks:
        status = None
        if chunk.HasField("task"):
            task = chunk.task
            context_id = task.context_id or context_id
            status = task.status
            if task.artifacts:
                artifacts = {a.artifact_id: _text(a.parts) for a in task.artifacts}
        elif chunk.HasField("artifact_update"):
            update = chunk.artifact_update
            context_id = update.context_id or context_id
            previous = artifacts.get(update.artifact.artifact_id, "")
            artifacts[update.artifact.artifact_id] = (
                previous if update.append else ""
            ) + _text(update.artifact.parts)
        elif chunk.HasField("status_update"):
            context_id = chunk.status_update.context_id or context_id
            status = chunk.status_update.status
        elif chunk.HasField("message"):
            context_id = chunk.message.context_id or context_id
            reply = _text(chunk.message.parts) or reply
        if status is not None and status.HasField("message"):
            reply = _text(status.message.parts) or reply
        if status is not None and status.state in _FAILED_STATES:
            raise RemoteAgentError(
                f"The {name} agent's task {_FAILED_STATES[status.state]}: "
                f"{_text(status.message.parts) or 'no reason given'}"
            )
    return "".join(artifacts.values()) or reply, context_id


def _text(parts: Any) -> str:
    return "".join(part.text for part in parts if part.text)


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}
