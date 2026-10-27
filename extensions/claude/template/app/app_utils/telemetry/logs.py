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

"""Python log records and GenAI events, exported to Cloud Logging.

Nothing is sent until setup_cloud_logging() runs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry._logs import LogRecord, SeverityNumber

from app.app_utils.telemetry import config

# Cloud Logging log names; GCP_DEFAULT_LOG_NAME overrides both.
_AGENT_RUNTIME_LOG_NAME = "aiplatform.googleapis.com/reasoning_engine_stdout"
_LOG_NAME = "adk-otel"

# GenAI events are emitted under the telemetry package's name.
_EVENT_LOGGER_NAME = __name__.rpartition(".")[0]

_logger_provider: Any = None


def setup_cloud_logging(credentials: Any, resource: Any, project_id: str) -> Any:
    """Send Python log records to Cloud Logging too; return the LoggerProvider."""
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    global _logger_provider
    resource = _log_resource(resource, project_id)
    session, upstream = config.telemetry_session(credentials)
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(session=session, endpoint=f"{upstream}/v1/logs")
        )
    )
    _install_handler(
        _CloudLoggingHandler(
            logger_provider,
            {
                "gcp.log_name": _log_name(),
                # Cloud Logging drops resource attributes, so send it as a label.
                "service.version": str(resource.attributes.get("service.version", "")),
            },
        )
    )
    _logger_provider = logger_provider
    return logger_provider


def emit_event(record: LogRecord) -> None:
    """Send a GenAI event to Cloud Logging, in a log named after `event.name`."""
    if _logger_provider is None:
        return
    attributes = dict(record.attributes or {})
    attributes["gcp.log_name"] = (
        _AGENT_RUNTIME_LOG_NAME
        if config.on_agent_runtime()
        else attributes["event.name"]
    )
    record.attributes = attributes
    _logger_provider.get_logger(_EVENT_LOGGER_NAME).emit(record)


def _log_resource(resource: Any, project_id: str) -> Any:
    """On Agent Runtime, the resource that files logs under the engine."""
    from opentelemetry.sdk.resources import Resource

    engine_id = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID")
    if not engine_id:
        return resource
    # Logs only: on the metrics' resource, gcp.resource_type would move them
    # off their monitored resource.
    return resource.merge(
        Resource(
            {
                "gcp.resource_type": "aiplatform.googleapis.com/ReasoningEngine",
                "location": config.agent_runtime_location(),
                "reasoning_engine_id": engine_id,
                # Without "projects/" the Telemetry API rejects the records.
                "resource_container": f"projects/{project_id}",
            }
        )
    )


def _log_name() -> str:
    return os.environ.get("GCP_DEFAULT_LOG_NAME") or (
        _AGENT_RUNTIME_LOG_NAME if config.on_agent_runtime() else _LOG_NAME
    )


def _install_handler(handler: logging.Handler) -> None:
    root = logging.getLogger()
    if not root.handlers:
        # Keep stderr output, which Python drops once the root logger has a handler.
        root.addHandler(logging.StreamHandler())
    root.addHandler(handler)
    # uvicorn's logger doesn't propagate; its access log is left out.
    uvicorn = logging.getLogger("uvicorn")
    if not uvicorn.propagate:
        uvicorn.addHandler(handler)


_SEVERITIES = (
    (logging.CRITICAL, SeverityNumber.FATAL, "FATAL"),
    (logging.ERROR, SeverityNumber.ERROR, "ERROR"),
    (logging.WARNING, SeverityNumber.WARN, "WARN"),
    (logging.INFO, SeverityNumber.INFO, "INFO"),
    (logging.NOTSET, SeverityNumber.DEBUG, "DEBUG"),
)


class _CloudLoggingHandler(logging.Handler):
    """Hands Python log records to `logger_provider`, under the active span.

    `extra=` fields are not copied into attributes.
    """

    def __init__(self, logger_provider: Any, attributes: Mapping[str, str]) -> None:
        super().__init__()
        self._logger_provider = logger_provider
        self._attributes = dict(attributes)

    def emit(self, record: logging.LogRecord) -> None:
        # The OTel SDK's own records would feed back into the export.
        if record.name.startswith("opentelemetry"):
            return
        try:
            _, severity, severity_text = next(
                s for s in _SEVERITIES if record.levelno >= s[0]
            )
            attributes: dict[str, Any] = {
                **self._attributes,
                "code.file.path": record.pathname,
                "code.function.name": record.funcName,
                "code.line.number": record.lineno,
            }
            if record.exc_info and record.exc_info[0]:
                attributes["exception.type"] = record.exc_info[0].__name__
            self._logger_provider.get_logger(record.name).emit(
                LogRecord(
                    timestamp=int(record.created * 1e9),
                    context=otel_context.get_current(),
                    severity_number=severity,
                    severity_text=severity_text,
                    body=self.format(record),
                    attributes=attributes,
                )
            )
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        self._logger_provider.force_flush(config.FLUSH_TIMEOUT_MILLIS)
