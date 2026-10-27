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

"""Per-conversation working directories, and evicting the least recently used."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import claude_agent_sdk

# One working directory per agent and conversation. The CLI keys session
# transcripts by working directory, so a conversation must always land in the same one.
WORKSPACES = Path(tempfile.gettempdir()) / "claude-agent"

# Workspaces kept per instance before the least recently used are deleted. On
# Cloud Run the temp dir is memory. With Cloud SQL, evicted conversations resume.
MAX_WORKSPACES = int(os.environ.get("CLAUDE_MAX_WORKSPACES", "100"))

# This process's workspaces, least recently used first, with their running turn
# counts. Only these are evicted, since other processes on the host share WORKSPACES.
_workspaces: dict[Path, int] = {}


def claim_workspace(workspace: Path) -> None:
    """Mark a turn running in `workspace`, evicting idle ones beyond MAX_WORKSPACES."""
    _workspaces[workspace] = _workspaces.pop(workspace, 0) + 1
    idle = [w for w, turns in _workspaces.items() if not turns]
    for evicted in idle[: max(0, len(_workspaces) - MAX_WORKSPACES)]:
        del _workspaces[evicted]
        remove_workspace(evicted)


def release_workspace(workspace: Path) -> None:
    """Mark a turn in `workspace` finished, so it can be evicted once idle."""
    _workspaces[workspace] -= 1


def remove_workspace(workspace: Path) -> None:
    """Delete a workspace and the transcripts the CLI kept for it."""
    transcripts = transcripts_dir(workspace)
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(transcripts, ignore_errors=True)


def transcripts_dir(workspace: Path) -> Path:
    """Where the CLI keeps local transcripts for sessions run in `workspace`."""
    config_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return (
        config_home / "projects" / claude_agent_sdk.project_key_for_directory(workspace)
    )
