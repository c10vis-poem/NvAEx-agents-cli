# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Process-wide ADK session/artifact services shared by every serving surface.

Registered under ``shared://`` so the ADK web routes, the A2A path, and the
reasoning_engine adapter share one instance: a session created on any surface
is visible to the others.
"""

from __future__ import annotations

import functools
import os

{% if cookiecutter.session_type == "agent_platform_sessions" -%}
import agentplatform
{% endif -%}
from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.adk.cli.service_registry import get_service_registry
from google.adk.cli.utils.service_factory import create_session_service_from_options

SESSION_SERVICE_URI = "shared://session"
ARTIFACT_SERVICE_URI = "shared://artifact"

_AGENT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


@functools.cache
def get_session_service():
    """Process-wide session service shared across every serving surface."""
{%- if cookiecutter.session_type == "cloud_sql" %}
    from urllib.parse import quote

    db_user = os.environ.get("DB_USER", "postgres")
    db_name = os.environ.get("DB_NAME", "postgres")
    db_pass = os.environ.get("DB_PASS")
    instance_connection_name = os.environ.get("INSTANCE_CONNECTION_NAME")

    if instance_connection_name and db_pass:
        # URL-encode credentials/instance; '[' would otherwise trigger IPv6 parsing.
        encoded_user = quote(db_user, safe="")
        encoded_pass = quote(db_pass, safe="")
        encoded_instance = instance_connection_name.replace(":", "%3A")
        session_service_uri = (
            f"postgresql+asyncpg://{encoded_user}:{encoded_pass}@"
            f"/{db_name}?host=/cloudsql/{encoded_instance}"
        )
        return create_session_service_from_options(
            base_dir=_AGENT_DIR, session_service_uri=session_service_uri
        )

    from google.adk.sessions.in_memory_session_service import InMemorySessionService

    return InMemorySessionService()
{%- elif cookiecutter.session_type == "agent_platform_sessions" %}
    use_in_memory_session = os.environ.get("USE_IN_MEMORY_SESSION", "").lower() in (
        "true",
        "1",
        "yes",
    )
    if not use_in_memory_session:
        default_agent_name = "{{cookiecutter.project_name}}"
        agent_name = os.environ.get("AGENT_ENGINE_SESSION_NAME", default_agent_name)
        client = agentplatform.Client(location=agent_runtime_location())
        existing_agents = list(
            client.runtimes.list(config={"filter": f"display_name={agent_name}"})
        )
        runtime = (
            existing_agents[0]
            if existing_agents
            else client.runtimes.create(config={"display_name": agent_name})
        )
        return create_session_service_from_options(
            base_dir=_AGENT_DIR,
            session_service_uri=f"agentengine://{runtime.api_resource.name}",
        )

    from google.adk.sessions.in_memory_session_service import InMemorySessionService

    return InMemorySessionService()


def agent_runtime_location() -> str:
    """Region for the session Agent Runtime; it doesn't support "global"."""
    if location := os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION"):
        return location
    location = os.environ.get("GOOGLE_CLOUD_LOCATION")
    if location and location != "global":
        return location
    return "us-east1"
{%- else %}
    if uri := os.environ.get("SESSION_SERVICE_URI"):
        return create_session_service_from_options(
            base_dir=_AGENT_DIR, session_service_uri=uri
        )
    if agent_engine_id := os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID"):
        from google.adk.sessions.vertex_ai_session_service import VertexAiSessionService

        return VertexAiSessionService(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            # Runtime-injected agent-engine region, not GOOGLE_CLOUD_LOCATION
            # (which agent.py pins to "global").
            location=os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION")
            or os.environ.get("GOOGLE_CLOUD_LOCATION"),
            agent_engine_id=agent_engine_id,
        )
    from google.adk.sessions.in_memory_session_service import InMemorySessionService

    return InMemorySessionService()
{%- endif %}


@functools.cache
def get_artifact_service():
    """Process-wide artifact service: GCS when a bucket is set, else in-memory."""
    if bucket := os.environ.get("LOGS_BUCKET_NAME"):
        return GcsArtifactService(bucket_name=bucket)
    return InMemoryArtifactService()


_registry = get_service_registry()
_registry.register_session_service("shared", lambda uri, **kw: get_session_service())
_registry.register_artifact_service("shared", lambda uri, **kw: get_artifact_service())
