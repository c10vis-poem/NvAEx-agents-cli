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

"""The Claude Code CLI's spans: a local relay to Cloud Trace, and the CLI's env.

The relay also adds ADK's gen_ai.usage.* attributes to the CLI's model-call
spans, and records their token usage.
"""

from __future__ import annotations

import gzip
import logging
import os
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from app.app_utils.telemetry import config, metrics

# If you set any of these, the Claude Code CLI's telemetry is left to you.
_USER_CLI_TELEMETRY_VARS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_TRACES_EXPORTER",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
)

# Statuses the CLI's OTLP exporter retries on.
_RETRIED_STATUSES = (429, 502, 503, 504)

# gen_ai.usage.input_tokens includes the cache tokens.
_CLI_LLM_SPAN = "claude_code.llm_request"
_CLI_CACHE_TOKENS = ("cache_read_tokens", "cache_creation_tokens")


@dataclass(frozen=True)
class _CliExport:
    endpoint: str
    project_id: str


_cli_export: _CliExport | None = None


def enable_cli_export(credentials: Any, project_id: str) -> None:
    """Relay the CLI's spans to Cloud Trace, unless you configured its telemetry."""
    global _cli_export
    if any(os.environ.get(v) for v in _USER_CLI_TELEMETRY_VARS):
        return
    endpoint = start_trace_forwarder(lambda: config.telemetry_session(credentials))
    _cli_export = _CliExport(endpoint, project_id)


def cli_telemetry_env() -> dict[str, str]:
    """Environment that makes the Claude Code CLI export its spans, if enabled."""
    if _cli_export is None:
        return {}
    runtime = config.agent_runtime_attributes(_cli_export.project_id)
    engine_id = runtime.pop("service.name", None)
    service_name = (
        os.environ.get("OTEL_SERVICE_NAME") or engine_id or config.service_name()
    )
    env = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_TRACES_EXPORTER": "otlp",
        # Never "console": the CLI's stdout carries the SDK's messages.
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": _cli_export.endpoint,
        "OTEL_RESOURCE_ATTRIBUTES": _resource_attributes(
            runtime, _cli_export.project_id
        ),
        "OTEL_SERVICE_NAME": service_name,
    }
    if config.capture_content():
        for var in (
            "OTEL_LOG_USER_PROMPTS",
            "OTEL_LOG_TOOL_DETAILS",
            "OTEL_LOG_TOOL_CONTENT",
        ):
            env[var] = os.environ.get(var, "1")
    return env


def start_trace_forwarder(new_session: Callable[[], tuple[Any, str]]) -> str:
    """Relay the CLI's OTLP trace exports from 127.0.0.1 to the Telemetry API.

    The CLI can't mint Google credentials, so the sessions `new_session` makes
    add them; it returns a session and the upstream it sends to. Returns the
    endpoint for the CLI's OTEL_EXPORTER_OTLP_ENDPOINT.
    """
    # Each request runs on its own thread and takes a session no other request
    # holds: sessions aren't thread-safe. The first is made now, so a bad
    # credential setup fails at startup.
    idle: queue.SimpleQueue[tuple[Any, str]] = queue.SimpleQueue()
    idle.put(new_session())

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/v1/traces":
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            headers = {
                k: self.headers[k]
                for k in ("Content-Type", "Content-Encoding")
                if self.headers.get(k)
            }
            body, calls = _with_genai_usage(body, headers)
            try:
                session, upstream = idle.get_nowait()
            except queue.Empty:
                session, upstream = new_session()
            try:
                response = session.post(
                    f"{upstream}/v1/traces", data=body, headers=headers, timeout=10
                )
            except Exception as e:
                logging.warning("Forwarding Claude Code CLI spans failed: %s", e)
                self.send_error(502)
                return
            finally:
                idle.put((session, upstream))
            # The CLI retries these, so count tokens only on the final attempt.
            if response.status_code not in _RETRIED_STATUSES:
                for model, usage in calls:
                    metrics.record_token_usage(model, usage)
            if response.status_code >= 400:
                logging.warning(
                    "Cloud Trace rejected Claude Code CLI spans (%s): %s",
                    response.status_code,
                    response.text[:500],
                )
            self.send_response(response.status_code)
            self.send_header(
                "Content-Type",
                response.headers.get("Content-Type", "application/x-protobuf"),
            )
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever, name="otlp-forwarder", daemon=True
    ).start()
    return f"http://127.0.0.1:{server.server_port}"


def _resource_attributes(runtime: dict[str, str], project_id: str) -> str:
    """OTEL_RESOURCE_ATTRIBUTES for the CLI: Agent Runtime's, then the user's."""
    # The Telemetry API rejects spans whose resource lacks gcp.project_id.
    user_attributes = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    if "gcp.project_id=" not in user_attributes:
        user_attributes = ",".join(
            filter(None, [user_attributes, f"gcp.project_id={project_id}"])
        )
    # User attributes come last so they override.
    return ",".join(
        [*(f"{key}={value}" for key, value in runtime.items()), user_attributes]
    )


def _with_genai_usage(
    body: bytes, headers: dict[str, str]
) -> tuple[bytes, list[tuple[str, dict[str, Any]]]]:
    """The export with gen_ai.usage.* added, if it's protobuf; else unchanged.

    Decompresses a gzipped body, and drops its Content-Encoding from `headers`.
    """
    if headers.get("Content-Type") != "application/x-protobuf":
        return body, []
    try:
        if headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
            headers.pop("Content-Encoding")
        return _add_genai_usage(body)
    except Exception as e:  # never lose spans over the extra attributes
        logging.debug("Could not add GenAI usage to CLI spans: %s", e)
        return body, []


def _add_genai_usage(body: bytes) -> tuple[bytes, list[tuple[str, dict[str, Any]]]]:
    """Add gen_ai.usage.* to the CLI's model-call spans in an OTLP export.

    Also returns each call's model and usage.
    """
    if _CLI_LLM_SPAN.encode() not in body:
        return body, []
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

    request = ExportTraceServiceRequest.FromString(body)
    calls = []
    for resource_spans in request.resource_spans:
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                if span.name != _CLI_LLM_SPAN:
                    continue
                present = {kv.key: kv.value for kv in span.attributes}
                usage = _genai_usage(_token_counts(present))
                model = present.get("model")
                calls.append((model.string_value if model else "", usage))
                for key, value in usage.items():
                    if key in present:
                        continue
                    any_value = (
                        AnyValue(int_value=value)
                        if isinstance(value, int)
                        else AnyValue(string_value=value)
                    )
                    span.attributes.append(KeyValue(key=key, value=any_value))
    return request.SerializeToString(), calls


def _token_counts(attributes: dict[str, Any]) -> dict[str, int]:
    """The CLI's token counts among a span's attributes, sent as int or string."""
    counts = {}
    for key in ("input_tokens", "output_tokens", *_CLI_CACHE_TOKENS):
        value = attributes.get(key)
        if value is None:
            continue
        raw = value.int_value if value.HasField("int_value") else value.string_value
        if str(raw).isdigit():
            counts[key] = int(raw)
    return counts


def _genai_usage(counts: dict[str, int]) -> dict[str, Any]:
    usage: dict[str, Any] = {"gen_ai.operation.name": "generate_content"}
    if "input_tokens" in counts:
        usage["gen_ai.usage.input_tokens"] = counts["input_tokens"] + sum(
            counts.get(key, 0) for key in _CLI_CACHE_TOKENS
        )
    if "output_tokens" in counts:
        usage["gen_ai.usage.output_tokens"] = counts["output_tokens"]
    if "cache_read_tokens" in counts:
        usage["gen_ai.usage.cache_read.input_tokens"] = counts["cache_read_tokens"]
    if "cache_creation_tokens" in counts:
        usage["gen_ai.usage.cache_creation.input_tokens"] = counts[
            "cache_creation_tokens"
        ]
    return usage
