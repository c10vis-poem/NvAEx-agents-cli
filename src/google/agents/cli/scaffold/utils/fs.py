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

"""Shared filesystem helpers for copying project directories."""

import fnmatch
import ntpath
import os
import pathlib
import sys
from collections.abc import Iterator
from pathlib import PureWindowsPath

# Directories/files to exclude when copying a project tree (backups, local
# template copies, etc.).
STANDARD_IGNORE_PATTERNS = frozenset(
    {
        ".git",
        ".hg",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        ".next",
        "dist",
        "build",
        ".DS_Store",
        ".vscode",
        ".idea",
        "*.egg-info",
        ".mypy_cache",
        ".ruff_cache",
        ".ty",
        ".coverage",
        "htmlcov",
        ".tox",
        ".cache",
        # Terraform's provider binaries: hundreds of MB, re-fetched by `init`.
        ".terraform",
        # ADK's local session and log store, written by `adk web` and `adk run`.
        ".adk",
    }
)


def standard_ignore_patterns(dir: str, files: list[str]) -> list[str]:
    """Return the entries in ``files`` to skip when copying a project tree.

    Matches the signature expected by ``shutil.copytree(ignore=...)``.
    """
    return _matching(files, STANDARD_IGNORE_PATTERNS)


# Credentials, dropped on top of the standard patterns when a tree is copied
# *into another project* rather than kept for its owner. Deliberately not part
# of the set above: `create_project_backup` is the undo for `enhance` and
# `upgrade`, both of which merge `.env` from the template, and a generated
# project gitignores it — so the backup holds the only copy the user has.
SECRET_IGNORE_PATTERNS = frozenset({".env", ".env.*"})

# `.env.*` would otherwise take the documentation along with the secret. These
# hold no values, and a source that ships one means it to be read.
ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})


def is_ignored_name(name: str) -> bool:
    """True if *name* matches a ``STANDARD_IGNORE_PATTERNS`` entry.

    These are the version-control metadata, caches, build output, and
    dependency directories a scaffolded template never carries. Also used to refuse a
    symlink whose resolved target sits inside one of them. Case-insensitive on
    every OS: on macOS and Windows ``.Git`` opens the same directory as ``.git``.
    """
    return _matches_any_case(name, STANDARD_IGNORE_PATTERNS)


def is_secret_file(name: str) -> bool:
    """True if *name* is a credential file that must not be copied elsewhere.

    Matches a ``.env`` / ``.env.*`` file (see ``SECRET_IGNORE_PATTERNS``) but
    excludes the value-less env templates (``.env.example`` and friends), which
    hold no secrets and are meant to be shared. Case-insensitive, like
    ``is_ignored_name``.
    """
    if name.lower() in ENV_TEMPLATE_NAMES:
        return False
    return _matches_any_case(name, SECRET_IGNORE_PATTERNS)


def is_windows_device_name(name: str) -> bool:
    """True if Windows refuses *name* as a file or folder name (``con``, ``nul``).

    Uses ntpath, not os.path, so the answer is the same on every OS.
    """
    # is_reserved() is deprecated in 3.13 and removed in 3.15.
    if sys.version_info >= (3, 13):
        return ntpath.isreserved(name)
    return PureWindowsPath(name).is_reserved()


def source_ignore_patterns(dir: str, files: list[str]) -> set[str]:
    """Return the entries in ``files`` to skip when copying a source tree.

    A source is published into somebody else's project — an extension being
    vendored, say — so it leaves behind the standard junk and its credentials.
    """
    # The standard junk follows the OS, so a user folder `Build` is still copied on
    # Linux and macOS, while a credential is dropped in any letter case.
    standard = set(_matching(files, STANDARD_IGNORE_PATTERNS))
    return standard | {f for f in files if is_secret_file(f)}


def walk_source_tree(
    root: pathlib.Path, skip_dirs: frozenset[str]
) -> Iterator[tuple[pathlib.Path, list[str]]]:
    """Yield ``(directory, file names)`` under *root*, skipping hidden, ignored and *skip_dirs* dirs."""
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".") and not is_ignored_name(d) and d not in skip_dirs
        )
        yield pathlib.Path(current), files


def _matching(files: list[str], patterns: frozenset[str]) -> list[str]:
    return [f for f in files if any(fnmatch.fnmatch(f, p) for p in patterns)]


def _matches_any_case(name: str, patterns: frozenset[str]) -> bool:
    return any(fnmatch.fnmatchcase(name.lower(), p.lower()) for p in patterns)
