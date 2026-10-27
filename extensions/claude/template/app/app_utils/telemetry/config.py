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

"""Settings every telemetry signal shares: names, resource, Telemetry API access."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from app.app_utils import settings

_TELEMETRY_ENDPOINT = "https://telemetry.googleapis.com"
_MTLS_TELEMETRY_ENDPOINT = "https://telemetry.mtls.googleapis.com"

FLUSH_TIMEOUT_MILLIS = 5_000


def cloud_export_enabled() -> bool:
    value = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY")
    return value is None or value.lower() in ("true", "1")


def capture_content() -> bool:
    """Whether prompts, replies and tool arguments and results go into spans."""
    capture = os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "")
    return capture.lower() not in ("", "false", "no_content")


def on_agent_runtime() -> bool:
    return bool(os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID"))


def agent_runtime_location() -> str:
    return os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION") or os.environ.get(
        "GOOGLE_CLOUD_LOCATION", ""
    )


def service_name() -> str:
    return os.environ.get("OTEL_SERVICE_NAME") or os.environ.get(
        "K_SERVICE", "claude-agent"
    )


def agent_name() -> str:
    name = settings.AGENT_NAME
    return service_name() if not name or "{{" in name else name


def build_resource(project_id: str) -> Any:
    """Build the Resource; OTEL_* env vars, then GCP detection, override defaults."""
    from opentelemetry.sdk.resources import OTELResourceDetector, Resource

    resource = Resource.create(
        {
            "service.name": service_name(),
            "service.version": os.environ.get("COMMIT_SHA", "dev"),
            "service.instance.id": f"{uuid.uuid4().hex}-{os.getpid()}",
            # The Telemetry API rejects spans whose resource lacks gcp.project_id.
            "gcp.project_id": project_id,
            "cloud.account.id": project_id,
        }
    )
    resource = resource.merge(OTELResourceDetector().detect())
    try:
        from opentelemetry.resourcedetector.gcp_resource_detector import (
            GoogleCloudResourceDetector,
        )

        resource = resource.merge(
            GoogleCloudResourceDetector(raise_on_error=False).detect()
        )
    except Exception as e:  # best-effort; unavailable off-GCP
        logging.debug("GCP resource detection skipped: %s", e)
    return resource


def agent_runtime_attributes(project_id: str) -> dict[str, str]:
    """Span resource attributes for this Agent Runtime engine; empty elsewhere.

    The Agent Runtime console finds an engine's traces by cloud.resource_id.
    """
    engine_id = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID")
    if not engine_id:
        return {}
    location = agent_runtime_location()
    attributes = {
        "cloud.provider": "gcp",
        "cloud.platform": "gcp.agent_engine",
        "service.name": engine_id,
        "service.version": os.environ.get(
            "GOOGLE_CLOUD_AGENT_ENGINE_RUNTIME_REVISION_ID", ""
        ),
        "cloud.region": location,
    }
    if location:
        attributes["cloud.resource_id"] = (
            f"//aiplatform.googleapis.com/projects/{project_id}"
            f"/locations/{location}/reasoningEngines/{engine_id}"
        )
    return {key: value for key, value in attributes.items() if value}


def telemetry_session(credentials: Any) -> tuple[Any, str]:
    """An authorized session for the Telemetry API, and the endpoint to use.

    With a client certificate (Agent Identity on Agent Runtime), tokens are
    bound to it and only accepted over mTLS. Make one session per exporter:
    requests sessions aren't thread-safe.
    """
    from google.auth.transport import mtls
    from google.auth.transport.requests import AuthorizedSession

    session = AuthorizedSession(credentials)
    if mtls.should_use_client_cert():
        session.configure_mtls_channel()
        if session.is_mtls:
            return session, _MTLS_TELEMETRY_ENDPOINT
    return session, _TELEMETRY_ENDPOINT
