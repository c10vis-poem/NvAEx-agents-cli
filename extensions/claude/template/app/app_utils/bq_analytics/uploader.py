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

"""Stream rows to the `agent_events` table from a background thread."""

from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
import time
from typing import Any

from app.app_utils.bq_analytics import schema, tables

_BATCH_SECONDS = 0.2
# BigQuery's recommended maximum rows per insertAll request.
_BATCH_ROWS = 500
# Under insertAll's 10 MB request limit, with room for row ids.
_BATCH_BYTES = 9 * 1024 * 1024
# Caps a batch's request and retries; the client default retries for 10 minutes.
_WRITE_SECONDS = 30
# Newer rows are dropped once this many are queued.
_MAX_QUEUED_ROWS = 10_000
_EXIT_FLUSH_SECONDS = 30


class Writer:
    """Batches rows on a daemon thread into `insert_rows_json`."""

    def __init__(self, project: str, dataset: str) -> None:
        from google.cloud import bigquery

        self._client = bigquery.Client(project=project)
        # Retrying is safe: the row ids let BigQuery drop duplicates.
        self._retry = bigquery.DEFAULT_RETRY.with_timeout(_WRITE_SECONDS)
        self.project, self.dataset = project, dataset
        self.table_id = f"{project}.{dataset}.{schema.TABLE}"
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(_MAX_QUEUED_ROWS)
        self._idle = threading.Condition()
        self._pending = 0
        self._dropping = False
        self._views_done = threading.Event()
        threading.Thread(target=self._run, name="bq-analytics", daemon=True).start()
        # A one-off `agents-cli run` exits before the daemon thread writes.
        atexit.register(self.flush, _EXIT_FLUSH_SECONDS)

    def put(self, row: dict[str, Any]) -> None:
        with self._idle:
            self._pending += 1
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self._written(1)
            if not self._dropping:
                logging.warning(
                    "BigQuery Agent Analytics: %d rows are waiting to be written; "
                    "new rows are dropped until writes catch up.",
                    _MAX_QUEUED_ROWS,
                )
            self._dropping = True
            return
        self._dropping = False

    def flush(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for queued rows and the views setup."""
        deadline = time.monotonic() + timeout
        with self._idle:
            written = self._idle.wait_for(lambda: self._pending == 0, timeout)
            if not written:
                logging.warning(
                    "BigQuery Agent Analytics: %d rows weren't written within "
                    "%gs of shutdown and are lost.",
                    self._pending,
                    timeout,
                )
        return self._views_done.wait(max(0, deadline - time.monotonic())) and written

    def _run(self) -> None:
        failing = not self._set_up()
        while True:
            rows = self._next_rows()
            try:
                for batch in _by_size(rows):
                    failing = self._insert(batch, failing)
            except Exception:  # a dead thread would make every flush() time out
                logging.warning(
                    "BigQuery Agent Analytics dropped %d rows", len(rows), exc_info=True
                )
            self._written(len(rows))

    def _set_up(self) -> bool:
        """Create the table, then the views in the background; False on failure."""
        try:
            tables.ensure_table(self._client, self.table_id)
            threading.Thread(
                target=self._ensure_views, name="bq-analytics-views", daemon=True
            ).start()
            return True
        except Exception as e:
            self._views_done.set()
            logging.warning(
                "BigQuery Agent Analytics table %s is not ready: %s. Check that "
                "the dataset exists (agents-cli infra single-project creates it) "
                "and that the service account can write to it. Rows that can't "
                "be written are dropped.",
                self.table_id,
                e,
            )
            return False

    def _ensure_views(self) -> None:
        try:
            tables.ensure_views(self._client, self.project, self.dataset, self.table_id)
        except Exception as e:
            logging.warning("BigQuery Agent Analytics views: %s", e)
        finally:
            self._views_done.set()

    def _next_rows(self) -> list[dict[str, Any]]:
        """Wait for a row, then take what else arrives within _BATCH_SECONDS."""
        rows = [self._queue.get()]
        deadline = time.monotonic() + _BATCH_SECONDS
        while len(rows) < _BATCH_ROWS:
            try:
                rows.append(
                    self._queue.get(timeout=max(0, deadline - time.monotonic()))
                )
            except queue.Empty:
                break
        return rows

    def _insert(self, batch: list[dict[str, Any]], failing: bool) -> bool:
        """Write one batch; returns whether writes are failing."""
        try:
            errors = self._client.insert_rows_json(
                self.table_id,
                batch,
                row_ids=[r["event_id"] for r in batch],
                retry=self._retry,
                timeout=_WRITE_SECONDS,
            )
            if errors:
                logging.warning(
                    "BigQuery Agent Analytics rejected rows: %s", errors[:3]
                )
            return False
        except Exception as e:
            if not failing:
                logging.warning(
                    "BigQuery Agent Analytics write failed: %s. Rows are "
                    "dropped until writes succeed again.",
                    e,
                )
            return True

    def _written(self, count: int) -> None:
        """Mark `count` rows as no longer pending, written or not."""
        with self._idle:
            self._pending -= count
            self._idle.notify_all()


def _by_size(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split rows into batches of at most _BATCH_BYTES of JSON each."""
    batches: list[list[dict[str, Any]]] = [[]]
    size = 0
    for row in rows:
        row_size = len(json.dumps(row, default=str))
        if batches[-1] and size + row_size > _BATCH_BYTES:
            batches.append([])
            size = 0
        batches[-1].append(row)
        size += row_size
    return batches
