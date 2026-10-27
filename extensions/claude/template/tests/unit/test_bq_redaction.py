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

"""Credential redaction and size caps for BigQuery Agent Analytics rows."""

from __future__ import annotations

import json

import pytest

from app.app_utils.bq_analytics.redaction import sanitize


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            {"endpoint": "crm", "api_key": "sk-1"},
            {"endpoint": "crm", "api_key": "[REDACTED]"},
        ),
        # Keys match case-insensitively, with - and _ alike, at any depth.
        (
            [{"headers": {"X-Api-Key": "k", "Accept": "json"}}],
            [{"headers": {"X-Api-Key": "[REDACTED]", "Accept": "json"}}],
        ),
        ({"temp:session": "v", "count": 3}, {"temp:session": "[REDACTED]", "count": 3}),
        # Only exact key names: these merely contain one.
        (
            {"prompt_token_count": 5, "tokens": 9},
            {"prompt_token_count": 5, "tokens": 9},
        ),
    ],
)
def test_sensitive_keys_are_redacted(value, expected):
    assert sanitize(value, 1000) == (expected, False)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "call failed: api_key=sk-123, retrying",
            "call failed: api_key=[REDACTED], retrying",
        ),
        ('got {"password": "hunter2"} back', 'got {"password": "[REDACTED]"} back'),
        ("Authorization: Bearer abc.def", "Authorization: [REDACTED]"),
        ("send Bearer abc123 with it", "send Bearer [REDACTED] with it"),
        ("Basic dXNlcjpwYXNz", "Basic [REDACTED]"),
        # Prose that only looks similar is kept.
        (
            "the Basic plan costs 5, tokens used: 12",
            "the Basic plan costs 5, tokens used: 12",
        ),
    ],
)
def test_credentials_in_text_are_redacted(text, expected):
    assert sanitize(text, 1000) == (expected, False)


def test_a_json_document_in_a_string_is_redacted_by_key():
    blob = json.dumps({"client_secret": "s", "client_id": "c"})

    sanitized, cut = sanitize(blob, 1000)

    assert json.loads(sanitized) == {"client_secret": "[REDACTED]", "client_id": "c"}
    assert cut is False


def test_an_escaped_credential_withholds_the_whole_text():
    assert sanitize("url?api%5Fkey=secret", 1000) == ("[REDACTED_SENSITIVE_TEXT]", True)


def test_strings_are_capped_at_any_depth():
    sanitized, cut = sanitize({"result": ["x" * 20, "short"]}, 10)

    assert sanitized == {"result": ["x" * 10 + "...[TRUNCATED]", "short"]}
    assert cut is True
