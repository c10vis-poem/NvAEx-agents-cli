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

"""The template's copy of ADK's agent_events schema and views is consistent."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = next(
    p
    for p in Path(__file__).resolve().parents
    if (p / "extensions" / "claude" / "template").is_dir()
)
_AGENT_EVENTS = (
    _REPO_ROOT
    / "extensions"
    / "claude"
    / "template"
    / "app"
    / "app_utils"
    / "bq_analytics"
    / "schema.py"
)


def _vendored():
    spec = importlib.util.spec_from_file_location("agent_events", _AGENT_EVENTS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_view_selects_from_the_events_table():
    module = _vendored()
    columns = {f["name"] for f in module.EVENTS_SCHEMA}
    assert set(module.VIEW_COMMON_COLUMNS) <= columns
    assert "INVOCATION_COMPLETED" in module.EVENT_VIEW_DEFS
