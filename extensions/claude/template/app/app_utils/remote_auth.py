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

"""How `remote_agent` authenticates to a remote agent.

Which host gets which Google token, over which TLS connection, and how the
token is kept to the host it was meant for.
"""

from __future__ import annotations

import ssl
from collections.abc import Generator, Mapping
from typing import Any

import httpx
from a2a.types import AgentCard

_AGENT_RUNTIME_HOST_SUFFIX = "-aiplatform.googleapis.com"
_CLOUD_RUN_HOST_SUFFIX = ".run.app"
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def is_agent_runtime(url: str) -> bool:
    """Whether `url` is an Agent Runtime endpoint, matched on the host.

    The access token sent there is good for any Google Cloud API.
    """
    parsed = httpx.URL(url)
    return (
        parsed.scheme == "https"
        and parsed.host.endswith(_AGENT_RUNTIME_HOST_SUFFIX)
        and "/reasoningEngines/" in parsed.path
    )


def is_cloud_run(url: str) -> bool:
    """Whether `url` is at a Cloud Run service's own `*.run.app` host."""
    parsed = httpx.URL(url)
    return parsed.scheme == "https" and parsed.host.endswith(_CLOUD_RUN_HOST_SUFFIX)


def mtls_target(url: str) -> tuple[str, ssl.SSLContext | bool]:
    """Where to send an Agent Runtime request, and how to verify TLS there.

    Tokens bound to an Agent Identity certificate need mTLS at the
    mtls.googleapis.com host. Call this per request to pick up a rotated cert.
    """
    from google.auth.transport import mtls

    context = mtls.get_default_ssl_context()
    if context is None:
        return url, True
    return _mtls_url(url), context


def _mtls_url(url: str) -> str:
    """`url` at its API's mTLS host, e.g. us-central1-aiplatform.mtls.googleapis.com."""
    parsed = httpx.URL(url)
    host = parsed.host.removesuffix(".googleapis.com") + ".mtls.googleapis.com"
    return str(parsed.copy_with(host=host))


def cloud_credentials() -> Any:
    """Application Default Credentials for Google Cloud APIs."""
    import google.auth

    credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
    return credentials


def access_token(credentials: Any) -> str:
    """A current OAuth access token from `credentials`, refreshed when expired."""
    from google.auth.transport.requests import Request

    if not credentials.valid:  # also false within a few minutes of expiry
        credentials.refresh(Request())
    return credentials.token


def id_token(audience: str, bind: bool = True) -> tuple[str, bool]:
    """A Google ID token for `audience`, and whether it's the user's unbound one."""
    import google.auth
    import google.oauth2.credentials
    import google.oauth2.id_token
    from google.auth import compute_engine, impersonated_credentials
    from google.auth.transport.requests import Request

    request = Request()
    # Resolved like ADC, so a developer's login wins over a VM's metadata server.
    credentials, _ = google.auth.default()
    if isinstance(credentials, google.oauth2.credentials.Credentials):
        # User credentials' own ID token is for gcloud's client, not `audience`:
        # whoever receives it can call any Cloud Run service the user can.
        credentials.refresh(request)
        return credentials.id_token, True
    if isinstance(credentials, impersonated_credentials.Credentials):
        token = impersonated_credentials.IDTokenCredentials(
            credentials, target_audience=audience, include_email=True
        )
        token.refresh(request)
        return token.token, False
    if isinstance(credentials, compute_engine.Credentials):
        # With Agent Identity the token is bound to the agent's certificate,
        # which Cloud Run rejects with 401; callers to Cloud Run pass bind=False.
        token = compute_engine.IDTokenCredentials(
            request,
            audience,
            use_metadata_identity_endpoint=True,
            bind_id_token=bind,
        )
        token.refresh(request)
        return token.token, False
    # A service account key.
    return google.oauth2.id_token.fetch_id_token(request, audience), False


def origin(url: str | httpx.URL) -> httpx.URL:
    """The scheme, host and port of `url`."""
    # httpx.URL normalizes case and drops a default port.
    url = httpx.URL(url)
    return httpx.URL(scheme=url.scheme, host=url.host, port=url.port)


class PinnedHeaders(httpx.Auth):
    """Add `headers` only to requests for `origin`, so credentials stay there."""

    def __init__(self, headers: Mapping[str, str], origin: httpx.URL) -> None:
        self._headers = dict(headers)
        self._origin = origin

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response]:
        if origin(request.url) == self._origin:
            # No traceparent: the remote agent records its own trace.
            request.headers.update(self._headers)
        yield request


def pin_to_origin(card: AgentCard, url: str) -> None:
    """Point the card's endpoints at `url`'s origin, so it can't redirect the token."""
    target = httpx.URL(url)
    for interface in card.supported_interfaces:
        declared = httpx.URL(interface.url)
        interface.url = str(
            target.copy_with(path=declared.path or target.path, query=declared.query)
        )
