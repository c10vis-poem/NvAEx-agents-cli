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

"""Create the `agent_events` table and its `v_*` views, as ADK's plugin does."""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Any

from app.app_utils.bq_analytics import schema

_VIEW_HASH_LABEL = "view_query_sha256"


def ensure_table(client: Any, table_id: str) -> None:
    """Create the events table unless it exists."""
    from google.api_core.exceptions import Conflict, NotFound
    from google.cloud import bigquery

    try:
        client.get_table(table_id)
    except NotFound:
        table = bigquery.Table(
            table_id,
            schema=[
                bigquery.SchemaField.from_api_repr(f) for f in schema.EVENTS_SCHEMA
            ],
        )
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field="timestamp"
        )
        table.clustering_fields = schema.CLUSTERING_FIELDS
        table.labels = {"adk_schema_version": schema.SCHEMA_VERSION}
        try:
            client.create_table(table)
        except Conflict:
            pass  # another instance created it first


def ensure_views(client: Any, project: str, dataset: str, table_id: str) -> None:
    """Create or update the views whose query hash label is out of date."""
    from google.cloud import bigquery

    labels = {
        item.table_id: (item.labels or {}).get(_VIEW_HASH_LABEL)
        for item in client.list_tables(f"{project}.{dataset}")
    }
    columns = list(schema.VIEW_COMMON_COLUMNS)
    writes = []
    for event_type, extra in schema.EVENT_VIEW_DEFS.items():
        # The tables API, unlike a query job, needs only dataset write.
        view = bigquery.Table(
            f"{project}.{dataset}.{schema.VIEW_PREFIX}_{event_type.lower()}"
        )
        selected = ",\n  ".join(columns + extra)
        view.view_query = (
            f"SELECT\n  {selected}\nFROM\n  `{table_id}`\n"
            f"WHERE\n  event_type = '{event_type}'\n"
        )
        digest = hashlib.sha256(view.view_query.encode()).hexdigest()[:16]
        if labels.get(view.table_id, "") == digest:
            continue
        view.labels = {_VIEW_HASH_LABEL: digest}
        writes.append(
            threading.Thread(
                target=_write_view,
                args=(client, view, view.table_id in labels),
                daemon=True,
            )
        )
    # Creating the views one by one takes over 10 s.
    for thread in writes:
        thread.start()
    for thread in writes:
        thread.join()


def _write_view(client: Any, view: Any, exists: bool) -> None:
    from google.api_core.exceptions import Conflict

    try:
        if not exists:
            try:
                client.create_table(view)
                return
            except Conflict:
                pass  # another instance created it first
        client.update_table(view, ["view_query", "labels"])
    except Exception as e:
        logging.warning("BigQuery Agent Analytics view %s: %s", view.table_id, e)
