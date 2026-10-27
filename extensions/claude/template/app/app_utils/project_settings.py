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

"""The served agent's Claude Code settings: CLAUDE.md, .claude/ and .mcp.json.

They live in agent_config/, not at the project root, so the developer's own
Claude Code setup (permission rules, hooks, notes) never reaches the served agent.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.app_utils import workspaces

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_CONFIG_DIR = PROJECT_ROOT / "agent_config"

_FILES = ("CLAUDE.md", "CLAUDE.local.md", ".mcp.json")


def copy_project_settings(workspace: Path) -> bool:
    """Copy agent_config/'s CLAUDE.md, .claude/ and .mcp.json into `workspace`.

    Returns whether there are settings to load there.
    """
    config = AGENT_CONFIG_DIR
    if workspace.resolve() == config.resolve():
        return (config / ".claude").is_dir() or any(
            (config / name).is_file() for name in _FILES
        )
    if not workspace.resolve().is_relative_to(workspaces.WORKSPACES.resolve()):
        # A root_agent cwd of its own, such as the project root or a repo the
        # agent works on: its files are not ours to replace or delete.
        return False
    # Replace, don't merge: settings the agent wrote into its workspace on an
    # earlier turn (hooks, MCP servers) must not load on the next one.
    shutil.rmtree(workspace / ".claude", ignore_errors=True)
    found = False
    if (config / ".claude").is_dir():
        shutil.copytree(config / ".claude", workspace / ".claude")
        found = True
    for name in _FILES:
        if (config / name).is_file():
            shutil.copy2(config / name, workspace / name)
            found = True
        else:
            (workspace / name).unlink(missing_ok=True)
    return found


def settings_allow_rules(setting_sources: list[str]) -> list[str]:
    """The permissions.allow rules from agent_config/'s settings the CLI loads.

    The CLI ignores them in an untrusted directory, which every workspace is,
    so they are passed as allowed_tools instead. They are read from
    agent_config/, never from the workspace copy, which the agent can write to.
    """
    rules: list[str] = []
    for source, name in (
        ("project", "settings.json"),
        ("local", "settings.local.json"),
    ):
        path = AGENT_CONFIG_DIR / ".claude" / name
        if source not in setting_sources or not path.is_file():
            continue
        try:
            settings = json.loads(path.read_text())
        except ValueError:
            # The CLI reports a malformed settings file itself.
            continue
        permissions = (
            settings.get("permissions") if isinstance(settings, dict) else None
        )
        allow = permissions.get("allow") if isinstance(permissions, dict) else None
        if isinstance(allow, list):
            rules.extend(rule for rule in allow if isinstance(rule, str))
    return rules
