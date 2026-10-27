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

"""The publish override: A2A everywhere the built-in can register it."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = next(
    p / "extensions" / "claude" / "template" / "scripts" / "publish.py"
    for p in Path(__file__).resolve().parents
    if (p / "extensions" / "claude").is_dir()
)


def _load():
    spec = importlib.util.spec_from_file_location("claude_publish", str(_SCRIPT))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.mark.parametrize(
    "targets,refused",
    [
        (("cloud_run", None), False),
        (("agent_runtime", None), True),
        # `deploy --deployment-target agent_runtime` leaves the manifest as is.
        (("cloud_run", "agent_runtime"), True),
    ],
)
def test_only_an_agent_runtime_deployment_is_refused(
    monkeypatch, tmp_path, capsys, targets, refused
):
    manifest_target, deployed_target = targets
    (tmp_path / "agents-cli-manifest.yaml").write_text(
        f"create_params:\n  deployment_target: {manifest_target}\n", encoding="utf-8"
    )
    if deployed_target:
        (tmp_path / "deployment_metadata.json").write_text(
            json.dumps({"deployment_target": deployed_target}), encoding="utf-8"
        )
    monkeypatch.chdir(tmp_path)
    mod = _load()
    calls: list[dict] = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda argv, env: (
            calls.append({"argv": argv, "env": env}) or type("R", (), {"returncode": 0})()
        ),
    )
    monkeypatch.setattr(mod.sys, "argv", ["publish.py", "--registration-type", "a2a"])

    code = mod.main()

    if refused:
        assert code == 2 and not calls
        assert "not supported" in capsys.readouterr().err
    else:
        # The built-in registers over A2A, and must not re-enter the override.
        assert code == 0
        assert calls[0]["argv"] == [
            "agents-cli",
            "publish",
            "gemini-enterprise",
            "--registration-type",
            "a2a",
        ]
        assert calls[0]["env"]["AGENTS_CLI_DISABLE_OVERRIDES"] == "1"


@pytest.mark.parametrize("manifest", ["create_params:\n", "- not a mapping\n"])
def test_a_malformed_manifest_does_not_crash(monkeypatch, tmp_path, manifest):
    (tmp_path / "agents-cli-manifest.yaml").write_text(manifest, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mod = _load()
    monkeypatch.setattr(
        mod.subprocess, "run", lambda argv, env: type("R", (), {"returncode": 0})()
    )
    monkeypatch.setattr(mod.sys, "argv", ["publish.py"])

    assert mod.main() == 0
