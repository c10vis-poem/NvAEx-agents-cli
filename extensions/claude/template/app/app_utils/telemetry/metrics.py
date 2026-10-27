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

"""ADK's gen_ai.* metrics, exported to Cloud Monitoring.

Nothing is recorded until setup_cloud_metrics() runs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from openinference.semconv.trace import SpanAttributes
from opentelemetry.metrics import Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricReader,
)
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import StatusCode

from app.app_utils.telemetry import config

_instruments: _Instruments | None = None
_metrics_failing = False


def setup_cloud_metrics(credentials: Any, resource: Any) -> MetricReader | None:
    """Export the gen_ai.* metrics to Cloud Monitoring.

    Agent Runtime throttles the CPU between requests, so there metrics are
    collected per request and the reader is returned; elsewhere None.
    """
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    global _instruments
    session, upstream = config.telemetry_session(credentials)
    exporter = _LoggingMetricExporter(
        OTLPMetricExporter(session=session, endpoint=f"{upstream}/v1/metrics")
    )
    on_agent_runtime = config.on_agent_runtime()
    reader = (
        _RequestDrivenReader(exporter)
        if on_agent_runtime
        else PeriodicExportingMetricReader(exporter)
    )
    meter_provider = SdkMeterProvider(metric_readers=[reader], resource=resource)
    otel_metrics.set_meter_provider(meter_provider)
    _instruments = _create_instruments(meter_provider)
    return reader if on_agent_runtime else None


class MetricsProcessor(SpanProcessor):
    """Records agent and tool call durations from their spans."""

    def on_end(self, span: ReadableSpan) -> None:
        instruments = _instruments
        attributes = span.attributes or {}
        kind = attributes.get(SpanAttributes.OPENINFERENCE_SPAN_KIND)
        if instruments is None or kind not in ("AGENT", "TOOL"):
            return
        try:
            point: dict[str, Any] = {
                "gen_ai.agent.name": attributes.get("gen_ai.agent.name")
                or attributes.get(SpanAttributes.AGENT_NAME)
                or config.agent_name(),
            }
            if span.status.status_code == StatusCode.ERROR:
                point["error.type"] = _error_type(span)
            seconds = ((span.end_time or 0) - (span.start_time or 0)) / 1e9
            if kind == "AGENT":
                point["gen_ai.operation.name"] = "invoke_agent"
                instruments.agent_duration.record(seconds, point)
            else:
                point["gen_ai.operation.name"] = "execute_tool"
                point["gen_ai.tool.name"] = str(
                    attributes.get("gen_ai.tool.name")
                    or attributes.get(SpanAttributes.TOOL_NAME, "")
                )
                instruments.tool_duration.record(seconds, point)
        except Exception as e:  # metrics must never fail a turn
            _warn_metrics_failure(e)


def record_call_counts(counts: Iterable[tuple[str, int, int]]) -> None:
    """Record gen_ai.invoke_agent.inference_calls and .tool_calls.

    `counts` holds (agent name, inference calls, tool calls) per agent of a turn.
    """
    instruments = _instruments
    if instruments is None:
        return
    try:
        for agent, inference_calls, tool_calls in counts:
            point = {"gen_ai.agent.name": agent}
            instruments.inference_calls.record(inference_calls, point)
            instruments.tool_calls.record(tool_calls, point)
    except Exception as e:  # metrics must never fail a turn
        _warn_metrics_failure(e)


def record_token_usage(model: str, usage: Mapping[str, Any]) -> None:
    """Record gen_ai.client.token.usage for one CLI model call.

    Subagent calls count toward the main agent: the CLI's spans don't name them.
    """
    instruments = _instruments
    if instruments is None:
        return
    try:
        point = {
            "gen_ai.operation.name": "generate_content",
            "gen_ai.agent.name": config.agent_name(),
        }
        if model:
            point["gen_ai.request.model"] = model
        for token_type, key in (
            ("input", "gen_ai.usage.input_tokens"),
            ("output", "gen_ai.usage.output_tokens"),
        ):
            if usage.get(key):
                instruments.token_usage.record(
                    usage[key], {**point, "gen_ai.token.type": token_type}
                )
    except Exception as e:  # metrics must never drop the relay's response
        _warn_metrics_failure(e)


class _LoggingMetricExporter(MetricExporter):
    """Wraps an exporter: a failed export is logged once, and never raises."""

    def __init__(self, exporter: MetricExporter) -> None:
        # The OTLP exporter resolves these from OTEL_EXPORTER_OTLP_METRICS_*,
        # and only exposes the result here.
        super().__init__(
            preferred_temporality=exporter._preferred_temporality,
            preferred_aggregation=exporter._preferred_aggregation,
        )
        self._exporter = exporter

    def export(
        self, metrics_data: Any, timeout_millis: float = 10_000, **kwargs: Any
    ) -> MetricExportResult:
        global _metrics_failing
        try:
            result = self._exporter.export(metrics_data, timeout_millis=timeout_millis)
        except Exception as e:
            _warn_metrics_failure(e)
            return MetricExportResult.FAILURE
        if result == MetricExportResult.SUCCESS:
            _metrics_failing = False
        else:
            _warn_metrics_failure("the Telemetry API rejected the export")
        return result

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return self._exporter.force_flush(timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        self._exporter.shutdown(timeout_millis=timeout_millis)


class _RequestDrivenReader(MetricReader):
    """Collects only when collect() is called: see request_flush.py."""

    def __init__(self, exporter: MetricExporter) -> None:
        super().__init__(
            preferred_temporality=exporter._preferred_temporality,
            preferred_aggregation=exporter._preferred_aggregation,
        )
        self._exporter = exporter

    def _receive_metrics(
        self, metrics_data: Any, timeout_millis: float = 10_000, **kwargs: Any
    ) -> None:
        self._exporter.export(metrics_data, timeout_millis=timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        self.collect(timeout_millis=timeout_millis)
        self._exporter.shutdown(timeout_millis=timeout_millis)


@dataclass(frozen=True)
class _Instruments:
    agent_duration: Histogram
    tool_duration: Histogram
    inference_calls: Histogram
    tool_calls: Histogram
    token_usage: Histogram


_CALL_COUNT_BUCKETS = [0, 1, 2, 3, 4, 5, 6, 8, 12, 16, 24, 32, 64, 128, 256, 512]


def _create_instruments(meter_provider: MeterProvider) -> _Instruments:
    # Same meter name as ADK, so existing dashboards pick these up.
    meter = meter_provider.get_meter("gcp.vertex.agent")
    return _Instruments(
        agent_duration=meter.create_histogram(
            "gen_ai.invoke_agent.duration",
            unit="s",
            description="Duration of agent invocations.",
            explicit_bucket_boundaries_advisory=[0.1 * 2**i for i in range(13)],
        ),
        tool_duration=meter.create_histogram(
            "gen_ai.execute_tool.duration",
            unit="s",
            description="Duration of tool executions.",
            explicit_bucket_boundaries_advisory=[0.01 * 2**i for i in range(14)],
        ),
        inference_calls=meter.create_histogram(
            "gen_ai.invoke_agent.inference_calls",
            unit="1",
            description="Number of inference (model) calls per agent invocation.",
            explicit_bucket_boundaries_advisory=_CALL_COUNT_BUCKETS,
        ),
        tool_calls=meter.create_histogram(
            "gen_ai.invoke_agent.tool_calls",
            unit="1",
            description="Number of tool calls per agent invocation.",
            explicit_bucket_boundaries_advisory=_CALL_COUNT_BUCKETS,
        ),
        token_usage=meter.create_histogram(
            "gen_ai.client.token.usage",
            unit="{token}",
            description="Number of input and output tokens used.",
            explicit_bucket_boundaries_advisory=[4**i for i in range(14)],
        ),
    )


def _error_type(span: ReadableSpan) -> str:
    """The recorded exception's unqualified class name, else _OTHER."""
    for event in span.events:
        if event.name == "exception" and event.attributes:
            name = str(event.attributes.get("exception.type", "_OTHER"))
            return name.rsplit(".", 1)[-1]
    return "_OTHER"


def _warn_metrics_failure(error: object) -> None:
    global _metrics_failing
    if not _metrics_failing:
        logging.warning(
            "Exporting agent metrics to Cloud Monitoring failed: %s. The next "
            "export sends the totals again; the service account needs "
            "roles/monitoring.metricWriter.",
            error,
        )
    _metrics_failing = True
