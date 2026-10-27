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

"""Redact credentials and cap the size of row content.

Adapted from google-adk 2.12.0's BigQuery Agent Analytics plugin: the same
sensitive keys and text patterns, for the plain JSON values the Claude Agent
SDK produces (dicts, lists, strings and scalars).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

REDACTED = "[REDACTED]"
_REDACTED_TEXT = "[REDACTED_SENSITIVE_TEXT]"
_MAX_DEPTH = 50

_SENSITIVE_KEYS = frozenset(
    {
        "client_secret",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "password",
        "private_key",
        "proxy_authorization",
        "google_access_id",
        "sig",
        "signature",
        "token",
        "secret",
        "authorization",
        "x_api_key",
        "x_amz_credential",
        "x_amz_signature",
        "x_goog_credential",
        "x_goog_security_token",
        "x_goog_signature",
    }
)
# `key: value` / `key=value` / `"key": "value"` for any sensitive key, in text.
_KEY_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>(?<![A-Za-z0-9_-])(?:[\"'])?(?:"
    r"temp:[A-Za-z0-9_.-]+|"
    + "|".join(
        sorted(
            (re.escape(key).replace("_", "[-_]") for key in _SENSITIVE_KEYS),
            key=len,
            reverse=True,
        )
    )
    + r")(?:[\"'])?\s*[:=]\s*)(?P<value>"
    r'"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'"
    r"|[^\s,;&}]+)"
)
_AUTH_HEADER_RE = re.compile(
    r"(?im)(?P<prefix>\b(?:authorization|proxy-authorization|x-api-key|api-key)"
    r"[ \t]*:[ \t]*)[^\r\n]*"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)(?P<prefix>\bbearer\s+)(?!of(?:\s|$))[^\s,;]+")
_BASIC_TOKEN_RE = re.compile(
    r"(?i)(?P<prefix>\bbasic\s+)(?P<value>[A-Za-z0-9+/]+={0,2})"
    r"(?![A-Za-z0-9+/=])"
)
_MAX_BASIC_TOKEN_CHARS = 16 * 1024
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def sanitize(value: Any, max_len: int) -> tuple[Any, bool]:
    """`value` with credentials redacted and strings capped at `max_len`.

    Returns the sanitized value and whether any content was cut.
    """
    return _sanitize(value, max_len, 0)


def _sanitize(value: Any, max_len: int, depth: int) -> tuple[Any, bool]:
    if depth >= _MAX_DEPTH:
        return "[MAX_DEPTH_EXCEEDED]", True
    if isinstance(value, str):
        return _sanitize_text(value, max_len, depth)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        cut = False
        for key, item in value.items():
            if isinstance(key, str) and _is_sensitive_key(key):
                out[key] = REDACTED
                continue
            out[key], item_cut = _sanitize(item, max_len, depth + 1)
            cut = cut or item_cut
        return out, cut
    if isinstance(value, (list, tuple)):
        items = [_sanitize(item, max_len, depth + 1) for item in value]
        return [item for item, _ in items], any(cut for _, cut in items)
    return value, False


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.startswith("temp:")


def _sanitize_text(text: str, max_len: int, depth: int) -> tuple[str, bool]:
    cut = False
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        # A JSON document in a string (cached credentials, a serialized
        # payload) gets the same key redaction as a structured value.
        try:
            parsed = json.loads(stripped)
        except ValueError:
            pass
        else:
            sanitized, cut = _sanitize(parsed, max_len, depth + 1)
            if sanitized != parsed:
                text = json.dumps(sanitized)
    text = _redact_patterns(text)
    # Credentials hidden behind \u, \x or % escapes can't be rewritten in
    # place safely, so the whole text is withheld.
    canonical = _decode_ascii_escapes(text)
    if canonical != text and _redact_patterns(canonical) != canonical:
        return _REDACTED_TEXT, True
    if len(text) > max_len:
        return text[:max_len] + "...[TRUNCATED]", True
    return text, cut


def _redact_patterns(text: str) -> str:
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group('prefix')}{REDACTED}", text)
    text = _BEARER_TOKEN_RE.sub(lambda m: f"{m.group('prefix')}{REDACTED}", text)
    text = _BASIC_TOKEN_RE.sub(_redact_basic_token, text)
    return _KEY_VALUE_RE.sub(_redact_key_value, text)


def _redact_key_value(match: re.Match[str]) -> str:
    value = match.group("value")
    if value == REDACTED:
        return match.group(0)
    quote = value[:1] if value[:1] in ('"', "'") else ""
    return f"{match.group('prefix')}{quote}{REDACTED}{quote}"


def _redact_basic_token(match: re.Match[str]) -> str:
    """Redact `Basic <token>` only when the token is base64 credentials."""
    encoded = match.group("value")
    if len(encoded) > _MAX_BASIC_TOKEN_CHARS:
        return f"{match.group('prefix')}{REDACTED}"
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return match.group(0)
    # "Basic plan" is prose; "Basic dXNlcjpwYXNz" decodes to "user:pass".
    if "=" not in encoded and b":" not in decoded:
        return match.group(0)
    return f"{match.group('prefix')}{REDACTED}"


def _decode_ascii_escapes(text: str) -> str:
    """Decode nested ASCII \\u, \\x and %XX escapes, in one pass."""
    output: list[str] = []
    for char in text:
        output.append(char)
        while True:
            if (
                len(output) >= 6
                and output[-6] == "\\"
                and output[-5] in ("u", "U")
                and all(c in _HEX_DIGITS for c in output[-4:])
            ):
                start, digits = len(output) - 6, "".join(output[-4:])
            elif (
                len(output) >= 4
                and output[-4] == "\\"
                and output[-3] in ("x", "X")
                and all(c in _HEX_DIGITS for c in output[-2:])
            ):
                start, digits = len(output) - 4, "".join(output[-2:])
            elif (
                len(output) >= 3
                and output[-3] == "%"
                and all(c in _HEX_DIGITS for c in output[-2:])
            ):
                start, digits = len(output) - 3, "".join(output[-2:])
            else:
                break
            decoded = int(digits, 16)
            if decoded > 0x7F:
                break
            del output[start:]
            output.append(chr(decoded))
    return "".join(output)
