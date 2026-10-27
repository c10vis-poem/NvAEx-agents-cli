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

"""OpenTelemetry setup: traces, logs and metrics to Google Cloud over OTLP.

Without GCP credentials, only the instrumentation runs. Each signal lives in
its own module; setup_telemetry() wires them together.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv

from app.app_utils.telemetry import (
    cli_relay,
    config,
    logs,
    metrics,
    request_flush,
    traces,
)
from app.app_utils.telemetry.cli_relay import cli_telemetry_env
from app.app_utils.telemetry.logs import emit_event
from app.app_utils.telemetry.request_flush import FlushTelemetryMiddleware

__all__ = [
    "FASTAPI_TELEMETRY",
    "FlushTelemetryMiddleware",
    "cli_telemetry_env",
    "emit_event",
    "setup_telemetry",
]

# Off so every request starts its own trace instead of continuing traceparent.
FASTAPI_TELEMETRY = {"tracing": False, "metrics": False, "logs": False}

_initialized = False


def setup_telemetry() -> None:
    """Configure trace export and instrumentation (idempotent)."""
    global _initialized
    if _initialized:
        return
    _initialized = True
    # Runs at import, before anything else loads .env.
    load_dotenv()

    exporting = False
    if not config.cloud_export_enabled():
        logging.info(
            "Cloud telemetry export disabled by "
            "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY."
        )
    else:
        try:
            _setup_cloud_exporters()
            exporting = True
        except Exception as e:  # no GCP creds (local dev)
            logging.warning("Cloud telemetry export not configured: %s", e)
    if not exporting:
        # OpenInference fails the turn when it ends a non-recording span with
        # gen_ai.* attributes on.
        os.environ.setdefault("OPENINFERENCE_ENABLE_GENAI_SEMCONV", "false")
    traces.install_instrumentation()


def _setup_cloud_exporters() -> None:
    """Export traces (the server's and the CLI's), logs and metrics to Cloud."""
    from opentelemetry import trace

    credentials, project_id = _default_credentials()
    resource = config.build_resource(project_id)

    tracer_provider = traces.setup_cloud_tracing(credentials, resource, project_id)
    tracer_provider.add_span_processor(metrics.MetricsProcessor())
    trace.set_tracer_provider(tracer_provider)

    logger_provider = logs.setup_cloud_logging(credentials, resource, project_id)
    reader = metrics.setup_cloud_metrics(credentials, resource)
    if reader is not None:
        request_flush.enable_request_flush(tracer_provider, logger_provider, reader)

    cli_relay.enable_cli_export(credentials, project_id)


def _default_credentials() -> tuple[Any, str]:
    """Application Default Credentials, and the project to export to."""
    import google.auth

    credentials, adc_project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or adc_project
    if not project_id:
        raise RuntimeError(
            "no Google Cloud project; set GOOGLE_CLOUD_PROJECT to export traces"
        )
    return credentials, project_id
