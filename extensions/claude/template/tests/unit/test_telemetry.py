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

"""What `app/app_utils/telemetry/` exports, and what it keeps out of spans."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import gzip
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest
import requests
from claude_agent_sdk import AssistantMessage, StreamEvent, TextBlock, ToolUseBlock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.auth.credentials import AnonymousCredentials
from openinference.instrumentation import OITracer, get_tool_attributes
from openinference.semconv.trace import OpenInferenceSpanKindValues
from opentelemetry import propagate, trace
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.app_utils import settings, telemetry
from app.app_utils.telemetry import (
    cli_relay,
    config,
    logs,
    metrics,
    request_flush,
    traces,
)
from app.app_utils.telemetry.call_counts import CallCounts
from app.app_utils.turn_events import TurnEnded, TurnParser

_ENV_VARS = (
    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY",
    "GOOGLE_CLOUD_PROJECT",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_SERVICE_NAME",
    "K_SERVICE",
    "GOOGLE_CLOUD_AGENT_ENGINE_ID",
    "GOOGLE_CLOUD_AGENT_ENGINE_LOCATION",
    "GOOGLE_CLOUD_AGENT_ENGINE_RUNTIME_REVISION_ID",
    "GOOGLE_CLOUD_LOCATION",
    "GCP_DEFAULT_LOG_NAME",
    "OTEL_TRACES_SAMPLER",
    "OTEL_LOG_USER_PROMPTS",
    "OPENINFERENCE_ENABLE_GENAI_SEMCONV",
    "OPENINFERENCE_HIDE_INPUTS",
    "OPENINFERENCE_HIDE_OUTPUTS",
    *cli_relay._USER_CLI_TELEMETRY_VARS,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for module, name, value in (
        (telemetry, "_initialized", False),
        (cli_relay, "_cli_export", None),
        (metrics, "_instruments", None),
        (request_flush, "_request_flush", None),
        (logs, "_logger_provider", None),
        (metrics, "_metrics_failing", False),
        (request_flush, "_last_collect", float("-inf")),
    ):
        monkeypatch.setattr(module, name, value)
    # Keep a developer's .env, and a workstation's client certificate, out of
    # the tests.
    monkeypatch.setattr(telemetry, "load_dotenv", lambda: None)
    monkeypatch.setenv("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")
    # setup_telemetry() adds handlers to these loggers.
    loggers = [logging.getLogger(), logging.getLogger("uvicorn")]
    handlers = [list(logger.handlers) for logger in loggers]
    yield
    for logger, saved in zip(loggers, handlers, strict=True):
        logger.handlers[:] = saved


class _CapturedMetrics(MetricExporter):
    """Keeps what a metric export would have sent; can be told to fail."""

    def __init__(self):
        super().__init__()
        self.batches: list = []
        self.fail: str | None = None

    def export(self, metrics_data, timeout_millis=10_000, **kwargs):
        if self.fail == "raise":
            raise ConnectionError("telemetry.googleapis.com unreachable")
        if self.fail == "reject":
            return MetricExportResult.FAILURE
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis=10_000):
        return True

    def shutdown(self, timeout_millis=30_000, **kwargs):
        pass

    def points(self, name: str) -> list:
        """(attributes, data point) for each point of metric `name`, last batch."""
        return [
            (dict(point.attributes), point)
            for resource in self.batches[-1].resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]


@dataclass
class _Cloud:
    """The Cloud Logging and Cloud Monitoring exporters setup_telemetry() made."""

    log_exporter: MagicMock
    logs: InMemoryLogRecordExporter
    metric_exporter: MagicMock
    metrics: _CapturedMetrics
    meter_providers: list

    def flush_logs(self):
        for handler in logging.getLogger().handlers:
            handler.flush()
        return self.logs.get_finished_logs()

    def flush_metrics(self):
        [provider] = self.meter_providers
        provider.force_flush()
        return self.metrics


@pytest.fixture(autouse=True)
def cloud(monkeypatch):
    cloud = _Cloud(
        log_exporter=MagicMock(),
        logs=InMemoryLogRecordExporter(),
        metric_exporter=MagicMock(),
        metrics=_CapturedMetrics(),
        meter_providers=[],
    )
    cloud.log_exporter.return_value = cloud.logs
    cloud.metric_exporter.return_value = cloud.metrics
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter",
        cloud.log_exporter,
    )
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter",
        cloud.metric_exporter,
    )
    monkeypatch.setattr(
        "opentelemetry.metrics.set_meter_provider", cloud.meter_providers.append
    )
    yield cloud
    # Rather than at exit, where a provider's final export would still run.
    cloud.metrics.fail = None
    for provider in cloud.meter_providers:
        provider.shutdown()


@pytest.fixture
def setup(monkeypatch):
    """setup_telemetry() with fake ADC and no global side effects.

    Returns the mocked OTLP exporter class and the tracer provider that would
    have been installed (None if none was).
    """

    @patch(
        "openinference.instrumentation.claude_agent_sdk.ClaudeAgentSDKInstrumentor.instrument"
    )
    @patch("opentelemetry.trace.set_tracer_provider")
    @patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter")
    @patch("google.auth.default")
    def run(adc_project, auth, exporter, set_provider, instrument):
        auth.return_value = (AnonymousCredentials(), adc_project)
        telemetry.setup_telemetry()
        provider = set_provider.call_args.args[0] if set_provider.called else None
        return exporter, provider

    return lambda adc_project="adc-project": run(adc_project)


def _finished_spans(provider: TracerProvider, *starts: dict) -> list:
    """End one span per attribute set; return them as exported.

    Spans with an OpenInference span kind go through OpenInference's tracer.
    """
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    plain = provider.get_tracer(__name__)
    openinference = OITracer(plain, config=traces._trace_config())
    for attributes in starts:
        oi = "openinference.span.kind" in attributes
        (openinference if oi else plain).start_span("span", attributes=attributes).end()
    return [dict(span.attributes or {}) for span in memory.get_finished_spans()]


def test_server_spans_go_over_otlp_with_their_resource(setup, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.setenv("K_SERVICE", "weather-agent")
    exporter, provider = setup()

    kwargs = exporter.call_args.kwargs
    assert kwargs["endpoint"] == "https://telemetry.googleapis.com/v1/traces"
    assert isinstance(kwargs["session"], requests.Session)
    resource = provider.resource.attributes
    assert resource["service.name"] == "weather-agent"
    # The Telemetry API rejects spans without it.
    assert resource["gcp.project_id"] == "my-project"
    assert resource["service.instance.id"]


_ENGINE = "//aiplatform.googleapis.com/projects/137/locations/us-central1/reasoningEngines/123"


@pytest.mark.parametrize(
    ("env", "service_name", "resource_id"),
    [
        ({"GOOGLE_CLOUD_AGENT_ENGINE_LOCATION": "us-central1"}, "123", _ENGINE),
        (
            {
                "GOOGLE_CLOUD_AGENT_ENGINE_LOCATION": "us-central1",
                "OTEL_SERVICE_NAME": "weather-agent",
            },
            "weather-agent",
            _ENGINE,
        ),
        # Without a location the engine can't be named.
        ({}, "123", None),
    ],
)
def test_on_agent_runtime_spans_name_the_engine(
    setup, monkeypatch, env, service_name, resource_id
):
    """The Agent Runtime console finds an engine's traces by cloud.resource_id."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "137")
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_RUNTIME_REVISION_ID", "7")
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    _, provider = setup()

    resource = provider.resource.attributes
    assert resource.get("cloud.resource_id") == resource_id
    assert resource["cloud.platform"] == "gcp.agent_engine"
    assert resource["service.name"] == service_name
    assert resource["service.version"] == "7"
    assert resource["gcp.project_id"] == "137"


def test_with_a_client_certificate_spans_go_over_mtls(setup, monkeypatch):
    """Agent Identity tokens are bound to its certificate: only mTLS takes them."""
    monkeypatch.setenv("GOOGLE_API_USE_CLIENT_CERTIFICATE", "true")

    def configure_mtls_channel(self, client_cert_callback=None):
        self._is_mtls = True

    with patch(
        "google.auth.transport.requests.AuthorizedSession.configure_mtls_channel",
        configure_mtls_channel,
    ):
        exporter, _ = setup()

    kwargs = exporter.call_args.kwargs
    assert kwargs["endpoint"] == "https://telemetry.mtls.googleapis.com/v1/traces"
    assert kwargs["session"].is_mtls


def test_without_a_project_nothing_is_exported(setup, cloud):
    exporter, provider = setup(adc_project=None)
    exporter.assert_not_called()
    assert provider is None
    assert telemetry.cli_telemetry_env() == {}
    cloud.log_exporter.assert_not_called()
    assert cloud.meter_providers == []


def test_agent_spans_carry_the_agent_name_setting(setup, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_NAME", "weather_agent")
    _, provider = setup()
    agent, tool, subagent, other = _finished_spans(
        provider,
        {"openinference.span.kind": "AGENT"},
        {"openinference.span.kind": "TOOL"},
        {"openinference.span.kind": "AGENT", "agent.name": "Task"},
        {"mcp.method.name": "tools/list"},
    )
    for span in (agent, tool):
        assert span["gen_ai.agent.name"] == "weather_agent"
    # A subagent's span keeps naming the subagent.
    assert subagent["gen_ai.agent.name"] == "Task"
    assert "gen_ai.agent.name" not in other


def test_mcp_and_bigquery_client_spans_are_not_recorded(setup):
    """The MCP SDK's duplicate OpenInference's tool spans; BigQuery's are noise.

    A span started under one, like an A2A call from a tool, keeps the enclosing
    span as its parent rather than pointing at a span that was never exported.
    """
    exporter, provider = setup()
    tracer = provider.get_tracer("openinference.instrumentation.claude_agent_sdk")
    with tracer.start_as_current_span("execute_tool") as tool:
        for scope in ("mcp-python-sdk", "google.cloud.bigquery.opentelemetry_tracing"):
            with provider.get_tracer(scope).start_as_current_span(scope):
                provider.get_tracer("a2a-python-sdk").start_span("a2a call").end()
    provider.force_flush()

    exported = [
        span
        for call in exporter.return_value.export.call_args_list
        for span in call.args[0]
    ]
    assert sorted(span.name for span in exported) == [
        "a2a call",
        "a2a call",
        "execute_tool",
    ]
    tool_id = tool.get_span_context().span_id
    assert all(s.parent.span_id == tool_id for s in exported if s.name == "a2a call")


def test_an_unrendered_agent_name_falls_back_to_the_service_name(setup, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_NAME", "{{cookiecutter.root_agent_name}}")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "weather-agent")
    _, provider = setup()
    [agent] = _finished_spans(provider, {"openinference.span.kind": "AGENT"})
    assert agent["gen_ai.agent.name"] == "weather-agent"


def test_cli_exports_to_the_forwarder_without_content(setup, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team=agents")
    setup()

    env = telemetry.cli_telemetry_env()
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"].startswith("http://127.0.0.1:")
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
    assert env["OTEL_METRICS_EXPORTER"] == env["OTEL_LOGS_EXPORTER"] == "none"
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "team=agents,gcp.project_id=my-project"
    assert "OTEL_LOG_USER_PROMPTS" not in env


def test_cli_spans_carry_the_cloud_run_service_name(setup, monkeypatch):
    monkeypatch.setenv("K_SERVICE", "weather-agent")
    setup()

    assert telemetry.cli_telemetry_env()["OTEL_SERVICE_NAME"] == "weather-agent"


def test_on_agent_runtime_cli_spans_name_the_engine(setup, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "137")
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION", "us-central1")
    monkeypatch.setenv("K_SERVICE", "reasoning-engine-123")
    setup()

    env = telemetry.cli_telemetry_env()
    assert env["OTEL_SERVICE_NAME"] == "123"
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == (
        "cloud.provider=gcp,cloud.platform=gcp.agent_engine,"
        f"cloud.region=us-central1,cloud.resource_id={_ENGINE},gcp.project_id=137"
    )


def test_capture_on_logs_cli_content(setup, monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    setup()
    env = telemetry.cli_telemetry_env()
    assert env["OTEL_LOG_USER_PROMPTS"] == "1"
    assert env["OTEL_LOG_TOOL_DETAILS"] == "1"
    # The ADC project when GOOGLE_CLOUD_PROJECT is unset.
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "gcp.project_id=adc-project"


def test_switch_off_exports_nothing(setup, cloud, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "false")
    exporter, _ = setup()
    exporter.assert_not_called()
    assert telemetry.cli_telemetry_env() == {}
    cloud.log_exporter.assert_not_called()
    cloud.metric_exporter.assert_not_called()
    assert cloud.meter_providers == []


def test_spans_under_an_unsampled_traceparent_are_recorded(setup):
    """Cloud Run's and Agent Runtime's front ends mark their traceparent not sampled."""
    _, provider = setup()
    parent = propagate.extract(
        {"traceparent": "00-00000000000000002e9787a365705b7c-1111111111111111-00"}
    )
    tracer = OITracer(provider.get_tracer(__name__), config=traces._trace_config())
    span = tracer.start_span(
        "turn",
        context=parent,
        openinference_span_kind=OpenInferenceSpanKindValues.AGENT,
    )
    assert span.is_recording()
    span.end()


def test_a_turn_the_users_sampler_drops_ends_cleanly_unexported(setup, monkeypatch):
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    exporter, provider = setup()
    tracer = OITracer(
        provider.get_tracer("openinference.instrumentation.claude_agent_sdk"),
        config=traces._trace_config(),
    )
    tracer.start_span(
        "turn", openinference_span_kind=OpenInferenceSpanKindValues.AGENT
    ).end()
    provider.force_flush()
    exporter.return_value.export.assert_not_called()


def test_with_export_off_an_agent_span_ends_cleanly(setup, monkeypatch):
    """With no tracer provider every span is non-recording; ending one must not raise."""
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "false")
    setup()
    tracer = OITracer(trace.NoOpTracer(), config=traces._trace_config())
    tracer.start_span(
        "turn", openinference_span_kind=OpenInferenceSpanKindValues.AGENT
    ).end()


def test_users_own_cli_telemetry_wins(setup, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    exporter, _ = setup()
    exporter.assert_called_once()
    assert telemetry.cli_telemetry_env() == {}


class _Upstream:
    """A fake Telemetry API that records what it receives."""

    def __init__(self, status: int = 200):
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        self.status = status
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                upstream.requests.append((self.path, dict(self.headers), body))
                self.send_response(upstream.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_port}"

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def upstream():
    servers: list[_Upstream] = []

    def make(status: int = 200) -> _Upstream:
        servers.append(_Upstream(status))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def _post(url: str, body: bytes) -> int:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-protobuf"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as e:
        return e.code


def test_forwarder_relays_traces_unchanged(upstream):
    fake = upstream()
    endpoint = cli_relay.start_trace_forwarder(lambda: (requests.Session(), fake.url))

    assert _post(f"{endpoint}/v1/traces", b"\x0a\x01spans") == 200
    [(path, headers, body)] = fake.requests
    assert path == "/v1/traces"
    assert body == b"\x0a\x01spans"
    assert headers["Content-Type"] == "application/x-protobuf"


def _cli_llm_span_export(model: str = "claude-haiku-4-5", **counts: int) -> bytes:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

    request = ExportTraceServiceRequest()
    scope = request.resource_spans.add().scope_spans.add()
    scope.spans.add(name="claude_code.tool")
    span = scope.spans.add(name="claude_code.llm_request")
    span.attributes.append(KeyValue(key="model", value=AnyValue(string_value=model)))
    span.attributes.extend(
        KeyValue(key=k, value=AnyValue(int_value=v)) for k, v in counts.items()
    )
    return request.SerializeToString()


def _span_attributes(body: bytes, name: str) -> dict:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    request = ExportTraceServiceRequest.FromString(body)
    [span] = [
        s
        for rs in request.resource_spans
        for ss in rs.scope_spans
        for s in ss.spans
        if s.name == name
    ]
    return {
        kv.key: kv.value.int_value or kv.value.string_value for kv in span.attributes
    }


@pytest.mark.parametrize("compress", [False, True])
def test_forwarder_adds_adk_style_usage_to_cli_model_calls(upstream, compress):
    fake = upstream()
    endpoint = cli_relay.start_trace_forwarder(lambda: (requests.Session(), fake.url))
    body = _cli_llm_span_export(
        input_tokens=100, output_tokens=7, cache_read_tokens=20, cache_creation_tokens=5
    )
    request = urllib.request.Request(
        f"{endpoint}/v1/traces",
        data=gzip.compress(body) if compress else body,
        headers={
            "Content-Type": "application/x-protobuf",
            **({"Content-Encoding": "gzip"} if compress else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200

    [(_, headers, forwarded)] = fake.requests
    assert "Content-Encoding" not in headers
    attributes = _span_attributes(forwarded, "claude_code.llm_request")
    # Cache tokens fold into the input count.
    assert attributes["gen_ai.usage.input_tokens"] == 125
    assert attributes["gen_ai.usage.output_tokens"] == 7
    assert attributes["gen_ai.usage.cache_read.input_tokens"] == 20
    assert attributes["gen_ai.usage.cache_creation.input_tokens"] == 5
    assert attributes["gen_ai.operation.name"] == "generate_content"
    assert attributes["input_tokens"] == 100  # the CLI's own names stay
    assert "gen_ai.usage.input_tokens" not in _span_attributes(
        forwarded, "claude_code.tool"
    )


def test_forwarder_relays_concurrent_exports_at_once():
    # Answers only once two exports are in flight together.
    both_in_flight = threading.Barrier(2, timeout=2)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            try:
                both_in_flight.wait()
                status = 200
            except threading.BrokenBarrierError:
                status = 500
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    sessions: list[requests.Session] = []

    def new_session():
        sessions.append(requests.Session())
        return sessions[-1], url

    try:
        endpoint = cli_relay.start_trace_forwarder(new_session)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            statuses = list(
                pool.map(lambda _: _post(f"{endpoint}/v1/traces", b"x"), range(2))
            )
    finally:
        server.shutdown()
        server.server_close()
    assert statuses == [200, 200]
    # Each request had a session of its own.
    assert len(sessions) == 2


def test_forwarder_relays_rejections_and_only_forwards_traces(upstream):
    fake = upstream(status=400)
    endpoint = cli_relay.start_trace_forwarder(lambda: (requests.Session(), fake.url))

    assert _post(f"{endpoint}/v1/traces", b"x") == 400
    assert _post(f"{endpoint}/v1/metrics", b"x") == 404
    assert [path for path, _, _ in fake.requests] == ["/v1/traces"]


def _tool_span_attributes() -> dict:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = OITracer(provider.get_tracer(__name__), config=traces._trace_config())
    span = tracer.start_span(
        "get_weather",
        openinference_span_kind=OpenInferenceSpanKindValues.TOOL,
        attributes=get_tool_attributes(
            name="get_weather", parameters={"city": "San Francisco"}
        ),
    )
    span.end()
    [finished] = exporter.get_finished_spans()
    return dict(finished.attributes or {})


def test_tool_arguments_are_redacted_by_default():
    attributes = _tool_span_attributes()
    assert attributes["tool.parameters"] == "__REDACTED__"
    assert attributes["gen_ai.tool.name"] == "get_weather"
    assert not any("San Francisco" in str(v) for v in attributes.values())


def test_tool_arguments_are_kept_when_capture_is_on(monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    attributes = _tool_span_attributes()
    assert "San Francisco" in attributes["tool.parameters"]
    assert "San Francisco" in attributes["gen_ai.tool.call.arguments"]


@pytest.mark.parametrize(
    ("env", "log_name", "resource_type"),
    [
        ({}, "adk-otel", None),
        (
            {
                "GOOGLE_CLOUD_AGENT_ENGINE_ID": "123",
                "GOOGLE_CLOUD_AGENT_ENGINE_LOCATION": "us-central1",
            },
            "aiplatform.googleapis.com/reasoning_engine_stdout",
            "aiplatform.googleapis.com/ReasoningEngine",
        ),
        ({"GCP_DEFAULT_LOG_NAME": "my-agent"}, "my-agent", None),
    ],
)
def test_log_records_go_to_cloud_logging_under_the_active_span(
    setup, cloud, monkeypatch, env, log_name, resource_type
):
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    _, provider = setup()

    with provider.get_tracer(__name__).start_as_current_span("turn") as span:
        try:
            raise TimeoutError("model call timed out")
        except TimeoutError:
            logging.getLogger("app.test").exception(
                "Turn failed: %s", "boom", extra={"prompt": "secret"}
            )
    [record] = cloud.flush_logs()

    log = record.log_record
    assert log.severity_number == SeverityNumber.ERROR
    assert log.severity_text == "ERROR"
    assert log.trace_id == span.get_span_context().trace_id
    assert log.span_id == span.get_span_context().span_id
    assert str(log.body).startswith("Turn failed: boom\nTraceback")
    assert log.attributes["exception.type"] == "TimeoutError"
    assert log.attributes["gcp.log_name"] == log_name
    # Fields a caller attaches to a record don't leave the process.
    assert "secret" not in str(dict(log.attributes))
    assert record.resource.attributes.get("gcp.resource_type") == resource_type
    # The span-only Agent Runtime attributes would remap the records.
    assert "cloud.resource_id" not in record.resource.attributes
    if resource_type:
        assert record.resource.attributes["reasoning_engine_id"] == "123"
        assert record.resource.attributes["location"] == "us-central1"
        assert record.resource.attributes["resource_container"] == (
            "projects/my-project"
        )
    endpoint = cloud.log_exporter.call_args.kwargs["endpoint"]
    assert endpoint == "https://telemetry.googleapis.com/v1/logs"


@pytest.mark.parametrize(
    ("env", "log_name"),
    [
        ({}, "gen_ai.client.inference.operation.details"),
        (
            {"GOOGLE_CLOUD_AGENT_ENGINE_ID": "123"},
            "aiplatform.googleapis.com/reasoning_engine_stdout",
        ),
    ],
)
def test_genai_events_go_where_adk_puts_them(setup, cloud, monkeypatch, env, log_name):
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    event = {"event.name": "gen_ai.client.inference.operation.details"}
    # Before setup there is nowhere to send them.
    telemetry.emit_event(LogRecord(attributes={**event, "a": "1"}))
    setup()
    telemetry.emit_event(LogRecord(attributes={**event, "a": "2"}))
    [record] = cloud.flush_logs()

    assert record.log_record.attributes["a"] == "2"
    assert record.log_record.attributes["gcp.log_name"] == log_name


def test_log_records_still_print_to_stderr(setup, monkeypatch, capsys):
    """Python prints to stderr only while the root logger has no handler."""
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    setup()
    logging.getLogger("app.test").warning("Tool call denied.")
    assert "Tool call denied." in capsys.readouterr().err


def test_uvicorn_server_logs_reach_cloud_logging(setup, cloud, monkeypatch):
    # As uvicorn configures its loggers before it imports the app.
    monkeypatch.setattr(logging.getLogger("uvicorn"), "propagate", False)
    setup()
    logging.getLogger("uvicorn.error").warning("Invalid HTTP request received.")
    [record] = cloud.flush_logs()
    assert record.log_record.severity_number == SeverityNumber.WARN
    assert record.log_record.body == "Invalid HTTP request received."


def _messages() -> list:
    """A turn with two model calls; the second delegates to a subagent (Task),
    which makes two of its own."""

    def message(message_id, *content, parent=None):
        return AssistantMessage(
            content=list(content),
            model="claude-haiku-4-5",
            message_id=message_id,
            parent_tool_use_id=parent,
        )

    def stream(kind):
        return StreamEvent(uuid="u", session_id="s", event={"type": kind})

    return [
        stream("message_start"),
        message("m1", ToolUseBlock(id="t1", name="get_weather", input={})),
        message("m1", TextBlock(text="Checking.")),  # the same model call
        stream("message_stop"),
        stream("message_start"),
        message("m2", ToolUseBlock(id="t2", name="Task", input={})),
        stream("message_stop"),
        message("m3", ToolUseBlock(id="t3", name="Bash", input={}), parent="t2"),
        message("m4", TextBlock(text="Done."), parent="t2"),
    ]


def _count_calls(messages: list) -> None:
    """Record a turn's call counts, as stream_turn does."""
    parser = TurnParser(config.agent_name())
    calls = CallCounts(parser.agent_name)
    for message in messages:
        for event in parser.feed(message):
            calls.handle(event)
    for event in parser.finish():
        calls.handle(event)


def test_a_turn_records_adk_metrics(setup, cloud, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_NAME", "weather_agent")
    _, provider = setup()
    tracer = OITracer(provider.get_tracer(__name__), config=traces._trace_config())
    with tracer.start_as_current_span(
        "turn", openinference_span_kind=OpenInferenceSpanKindValues.AGENT
    ):
        tracer.start_span(
            "get_weather",
            openinference_span_kind=OpenInferenceSpanKindValues.TOOL,
            attributes=get_tool_attributes(name="get_weather", parameters={}),
        ).end()
    _count_calls(_messages())

    exported = cloud.flush_metrics()
    [(attributes, turn)] = exported.points("gen_ai.invoke_agent.duration")
    assert attributes == {
        "gen_ai.agent.name": "weather_agent",
        "gen_ai.operation.name": "invoke_agent",
    }
    assert turn.count == 1
    [(attributes, _)] = exported.points("gen_ai.execute_tool.duration")
    assert attributes == {
        "gen_ai.agent.name": "weather_agent",
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "get_weather",
    }
    for name in ("inference_calls", "tool_calls"):
        counts = {
            attributes["gen_ai.agent.name"]: point.sum
            for attributes, point in exported.points(f"gen_ai.invoke_agent.{name}")
        }
        # The Task call is the agent's; the subagent's calls are its own.
        assert counts == {
            "weather_agent": 2,
            "subagent": 2 if name == "inference_calls" else 1,
        }
    endpoint = cloud.metric_exporter.call_args.kwargs["endpoint"]
    assert endpoint == "https://telemetry.googleapis.com/v1/metrics"


def test_a_failed_turn_records_its_error_type(setup, cloud):
    _, provider = setup()
    tracer = OITracer(provider.get_tracer(__name__), config=traces._trace_config())
    span = tracer.start_span(
        "turn", openinference_span_kind=OpenInferenceSpanKindValues.AGENT
    )
    span.record_exception(_ModelUnavailableError("claude-haiku-4-5"))
    span.set_status(trace.Status(trace.StatusCode.ERROR))
    span.end()

    [(attributes, _)] = cloud.flush_metrics().points("gen_ai.invoke_agent.duration")
    # The class name, not the span's module-qualified one.
    assert attributes["error.type"] == "_ModelUnavailableError"


class _ModelUnavailableError(Exception):
    pass


def test_cli_model_calls_record_token_usage_once(setup, cloud, upstream):
    setup()
    fake = upstream(status=503)
    endpoint = cli_relay.start_trace_forwarder(lambda: (requests.Session(), fake.url))
    body = _cli_llm_span_export(input_tokens=100, output_tokens=7, cache_read_tokens=20)

    # The CLI resends a batch the Telemetry API turned away for now.
    assert _post(f"{endpoint}/v1/traces", body) == 503
    fake.status = 200
    assert _post(f"{endpoint}/v1/traces", body) == 200

    points = cloud.flush_metrics().points("gen_ai.client.token.usage")
    tokens = {attrs["gen_ai.token.type"]: (p.count, p.sum) for attrs, p in points}
    assert tokens == {"input": (1, 120), "output": (1, 7)}
    attributes, _ = points[0]
    assert attributes["gen_ai.request.model"] == "claude-haiku-4-5"
    assert attributes["gen_ai.operation.name"] == "generate_content"


def test_a_failing_token_metric_still_relays_the_response(
    setup, cloud, upstream, monkeypatch, caplog
):
    setup()
    monkeypatch.setattr(
        metrics,
        "_instruments",
        dataclasses.replace(
            metrics._instruments,
            token_usage=MagicMock(record=MagicMock(side_effect=ValueError("bad"))),
        ),
    )
    endpoint = cli_relay.start_trace_forwarder(
        lambda: (requests.Session(), upstream().url)
    )

    assert _post(f"{endpoint}/v1/traces", _cli_llm_span_export(input_tokens=1)) == 200
    assert "agent metrics" in caplog.text


def test_without_cloud_export_turns_record_no_metrics(setup, cloud, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "false")
    setup()
    _count_calls(_messages())
    cloud.metric_exporter.assert_not_called()


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(telemetry.FlushTelemetryMiddleware)

    # Agent turns arrive as POSTs.
    @app.post("/turn")
    def turn() -> dict[str, str]:
        calls = CallCounts(TurnParser(config.agent_name()).agent_name)
        calls.handle(TurnEnded(failed=False, error=None))
        return {"status": "ok"}

    @app.get("/health")
    def health() -> dict[str, str]:
        calls = CallCounts(TurnParser(config.agent_name()).agent_name)
        calls.handle(TurnEnded(failed=False, error=None))
        return {"status": "ok"}

    return app


@pytest.mark.parametrize("on_agent_runtime", [True, False])
def test_agent_runtime_exports_metrics_before_each_response_ends(
    setup, cloud, monkeypatch, on_agent_runtime
):
    """Agent Runtime throttles the CPU between requests; elsewhere a periodic
    reader exports in the background."""
    if on_agent_runtime:
        monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")
    setup()

    client = TestClient(_app())
    assert client.post("/turn").status_code == 200
    assert bool(cloud.metrics.batches) == on_agent_runtime
    if on_agent_runtime:
        assert cloud.metrics.points("gen_ai.invoke_agent.inference_calls")
    # Cloud Monitoring rejects points closer than 5s apart on one series: the
    # next request's points wait for a later collect.
    assert client.post("/turn").status_code == 200
    assert len(cloud.metrics.batches) == int(on_agent_runtime)


def test_reads_like_health_checks_do_not_wait_for_a_flush(setup, cloud, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")
    setup()

    assert TestClient(_app()).get("/health").status_code == 200

    assert not cloud.metrics.batches


def test_a_failing_metrics_export_never_fails_a_request(
    setup, cloud, monkeypatch, caplog
):
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")
    setup()
    client = TestClient(_app())

    def turn(fail: str | None) -> None:
        cloud.metrics.fail = fail
        monkeypatch.setattr(request_flush, "_last_collect", float("-inf"))
        assert client.post("/turn").status_code == 200

    def warnings() -> int:
        return sum("agent metrics" in r.getMessage() for r in caplog.records)

    turn("raise")
    turn("reject")
    assert warnings() == 1  # once per run of failures
    turn(None)
    turn("reject")
    assert warnings() == 2
