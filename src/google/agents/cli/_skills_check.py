# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Skills version drift detection for agents-cli."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import yaml

# Pin the skills npm package to avoid executing an unverified version.
SKILLS_NPX_PACKAGE = "skills@1.5.9"

_SKILLS_CHECK_INTERVAL = 12 * 60 * 60  # 12 hours in seconds
_SKILLS_CHECK_STAMP = Path.home() / ".agents" / ".acli_skills_check"


def _parse_skill_version(skill_md: Path) -> str | None:
    """Extract ``metadata.version`` from a SKILL.md YAML frontmatter.

    Returns the version string, or ``None`` on any failure.
    Warns to stderr when the file exists but cannot be parsed.
    """
    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None

    if not content.startswith("---"):
        logging.warning(f"Malformed skill file (no frontmatter): {skill_md}")
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        logging.warning(f"Malformed skill file (incomplete frontmatter): {skill_md}")
        return None
    try:
        frontmatter = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        logging.warning(f"Malformed skill file (invalid YAML): {skill_md}")
        return None

    version = (frontmatter or {}).get("metadata", {}).get("version")
    if not version:
        logging.warning(f"Malformed skill file (missing metadata.version): {skill_md}")
    return str(version) if version else None


def list_skills_all_scopes() -> list[dict]:
    """Return ``npx skills list --json`` entries for project and global scope.

    ``skills list`` without ``-g`` only reports project-scoped skills and
    ``-g`` only reports global ones, so both are queried and merged. Entries
    that share a path are reported once: run from the home directory, the
    project store (``~/.agents/skills``) is also the global store.

    Raises ``subprocess.CalledProcessError`` if the project query exits
    non-zero, or ``ValueError`` if its output is not a JSON list. If only the
    global query fails, the project entries are returned and a warning is
    logged.
    """
    import json

    from google.agents.cli._runner import run_resolved

    entries: list[dict] = []
    seen_paths: set[str] = set()
    # Sequential on purpose: concurrent `npx -y` runs race on the shared npm
    # cache when the package isn't downloaded yet.
    for scope_args in ([], ["-g"]):
        cmd = ["npx", "-y", SKILLS_NPX_PACKAGE, "list", *scope_args, "--json"]
        try:
            proc = run_resolved(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            proc.check_returncode()
            data = json.loads(proc.stdout)
            if not isinstance(data, list):
                raise ValueError(f"expected a JSON list, got {type(data).__name__}")
        except (subprocess.SubprocessError, ValueError) as e:
            if not scope_args:
                raise
            logging.warning(
                "Could not list global skills; using project skills only: %s", e
            )
            continue
        for entry in data:
            path = entry.get("path") if isinstance(entry, dict) else None
            if path in seen_paths:
                continue
            if path:
                seen_paths.add(path)
            entries.append(entry)
    return entries


def _find_installed_skills() -> dict[str, str] | None:
    """Return ``{skill_name: version}`` for every installed agents-cli skill.

    Fast path (~8 ms): scan ``~/.agents/skills/google-agents-cli-*/SKILL.md``
    directly.  Falls back to ``npx skills list [-g] --json`` (~800 ms+) if
    the well-known directory is empty or missing, which is more robust
    when skills are installed to a non-default location.

    Returns ``None`` if the npx query fails, so callers can tell "no skills
    installed" apart from "could not check".
    """
    result: dict[str, str] = {}

    # Fast path: well-known global install location
    skills_dir = Path.home() / ".agents" / "skills"
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.name.startswith("google-agents-cli-"):
                continue
            version = _parse_skill_version(skill_dir / "SKILL.md")
            if version:
                result[skill_dir.name] = version

    if result:
        return result

    # Slow path: ask npx skills for actual install locations
    try:
        entries = list_skills_all_scopes()
    except subprocess.CalledProcessError:
        return None
    except Exception as e:
        # Broad by design: this freshness check runs on every CLI invocation and
        # must never abort a user's command, so any failure degrades to "could
        # not check" rather than propagating.
        logging.warning("Could not query installed skills via npx: %s", e)
        return None

    for entry in entries:
        if not isinstance(entry, dict):
            logging.warning(
                "Skipping malformed skills entry (expected an object): %r", entry
            )
            continue
        name = entry.get("name", "")
        if not name.startswith("google-agents-cli-"):
            continue
        path = entry.get("path")
        if not path:
            continue
        version = _parse_skill_version(Path(path) / "SKILL.md")
        if version:
            result[name] = version

    return result


def get_installed_skills() -> list[dict] | None:
    """Return installed agents-cli skills as a list of dicts.

    Returns None if the query fails.
    """
    try:
        skills = list_skills_all_scopes()
        return [s for s in skills if s.get("name", "").startswith("google-agents-cli-")]
    except subprocess.CalledProcessError as e:
        logging.warning(
            "npx skills list failed (exit %d): %s",
            e.returncode,
            (e.stderr or "").strip(),
        )
        return None
    except Exception as e:
        logging.warning("Could not query installed skills: %s", e)
        return None


def find_parent_workspace_skills(start: Path) -> Path | None:
    """Return the nearest parent of ``start`` with workspace-scope agents-cli skills.

    ``agents-cli setup --workspace`` installs skills relative to the directory it
    ran in, and coding agents started in a subdirectory (e.g. a project created
    there) may not load them. Returns ``None`` when ``start`` has its own
    workspace skills or no parent has any. The home directory is skipped because
    ``~/.agents/skills`` is the global store, which agents load from anywhere.
    """
    start = start.resolve()
    if _has_workspace_skills(start):
        return None
    home = Path.home().resolve()
    for parent in start.parents:
        if parent != home and _has_workspace_skills(parent):
            return parent
    return None


def _has_workspace_skills(directory: Path) -> bool:
    skills_dir = directory / ".agents" / "skills"
    try:
        return any(
            (d / "SKILL.md").is_file() for d in skills_dir.glob("google-agents-cli-*")
        )
    except OSError:
        return False


def _is_ci() -> bool:
    """Return True when running in a CI/automation environment.

    The skills version-drift warning is a local developer-experience nicety: in
    CI there is no human to act on it and skills are not installed, so the check
    only adds noise (and may spawn a doomed ``npx`` subprocess). We skip it when
    a well-known CI marker is present.
    """
    return any(
        os.environ.get(var) for var in ("CI", "BUILD_ID", "GITHUB_ACTIONS", "GITLAB_CI")
    )


def _skills_check_is_due() -> bool:
    """Return True if enough time has elapsed since the last check."""
    try:
        last = float(_SKILLS_CHECK_STAMP.read_text().strip())
        return (time.time() - last) > _SKILLS_CHECK_INTERVAL
    except (OSError, ValueError):
        return True


def _record_skills_check() -> None:
    """Write the current timestamp to the stamp file."""
    try:
        _SKILLS_CHECK_STAMP.parent.mkdir(parents=True, exist_ok=True)
        _SKILLS_CHECK_STAMP.write_text(str(time.time()))
    except OSError:
        pass


def check_skills_version() -> None:
    """Warn if any installed skill version doesn't match the running CLI version.

    Rate-limited to once per 24 hours via a timestamp file so it can
    run globally on every command without adding latency.

    Scans all installed ``google-agents-cli-*`` skills, compares each
    ``metadata.version`` with the running ``__version__``, and lists
    the mismatched ones.  Never blocks execution. Skipped entirely in CI.
    """
    if _is_ci() or not _skills_check_is_due():
        return

    installed = _find_installed_skills()
    if installed is None:
        # The query failed: don't record the stamp, so the next command retries.
        return

    _record_skills_check()
    if not installed:
        return

    from google.agents.cli import __version__

    mismatched = {name: ver for name, ver in installed.items() if ver != __version__}
    if not mismatched:
        return

    lines = [f"  - {name} (v{ver})" for name, ver in mismatched.items()]
    logging.warning(
        "Skills version mismatch — CLI is v%s, but %d skill(s) differ:\n%s\n"
        "Run 'agents-cli update' to sync.",
        __version__,
        len(mismatched),
        "\n".join(lines),
    )
