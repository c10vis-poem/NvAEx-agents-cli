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

"""On Agent Runtime, export each request's telemetry before its response ends.

Agent Runtime throttles the CPU once a response ends, starving background
exports.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
from opentelemetry.sdk.metrics.export import MetricReader

from app.app_utils.telemetry.config import FLUSH_TIMEOUT_MILLIS

# Cloud Monitoring rejects points on one time series written closer than this.
_MIN_COLLECT_SECONDS = 5.0


@dataclass(frozen=True)
class _RequestFlush:
    tracer_provider: Any
    logger_provider: Any
    reader: MetricReader


_request_flush: _RequestFlush | None = None
_collect_lock = threading.Lock()
_last_collect = float("-inf")


def enable_request_flush(
    tracer_provider: Any, logger_provider: Any, reader: MetricReader
) -> None:
    """Make FlushTelemetryMiddleware flush these before each response ends."""
    global _request_flush
    _request_flush = _RequestFlush(tracer_provider, logger_provider, reader)


class FlushTelemetryMiddleware:
    """Exports each POST request's telemetry before its last body chunk.

    No-op until enable_request_flush() runs, which only happens on Agent Runtime.
    Agent turns arrive as POSTs; health checks and agent card reads don't wait.
    """

    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        flush = _request_flush
        if flush is None or scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        async def send_after_flush(message: Any) -> None:
            if message["type"] == "http.response.body" and not message.get("more_body"):
                await anyio.to_thread.run_sync(_flush_request_telemetry, flush)
            await send(message)

        await self.app(scope, receive, send_after_flush)


def _flush_request_telemetry(flush: _RequestFlush) -> None:
    """Export the request's spans, log records and, if due, metrics."""
    try:
        flush.tracer_provider.force_flush(FLUSH_TIMEOUT_MILLIS)
        flush.logger_provider.force_flush(FLUSH_TIMEOUT_MILLIS)
        _collect_metrics_if_due(flush.reader)
    except Exception as e:  # never fail the request it rides on
        logging.warning("Exporting the request's telemetry failed: %s", e)


def _collect_metrics_if_due(reader: MetricReader) -> None:
    global _last_collect
    # While another request collects, skip rather than wait: this request's
    # points go out with the next collect, as they do within the 5 s window.
    if not _collect_lock.acquire(blocking=False):
        return
    try:
        if time.monotonic() - _last_collect >= _MIN_COLLECT_SECONDS:
            _last_collect = time.monotonic()
            reader.collect(timeout_millis=FLUSH_TIMEOUT_MILLIS)
    finally:
        _collect_lock.release()
