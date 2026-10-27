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

"""BigQuery Agent Analytics table schema and views, copied from google-adk 2.12.0."""

SCHEMA_VERSION = "2"

TABLE = "agent_events"

CLUSTERING_FIELDS = ["event_type", "agent", "user_id"]

VIEW_PREFIX = "v"

EVENTS_SCHEMA = [
    {
        "name": "timestamp",
        "type": "TIMESTAMP",
        "mode": "REQUIRED",
        "description": "The UTC timestamp when the event occurred. Used for ordering events "
        "within a session.",
    },
    {
        "name": "event_id",
        "type": "STRING",
        "mode": "NULLABLE",
        "description": "A unique identifier assigned before enqueue. Storage Write API "
        "retries preserve this value so duplicate rows can be identified "
        "reliably.",
    },
    {
        "name": "event_type",
        "type": "STRING",
        "mode": "NULLABLE",
        "description": "The category of the event (e.g., 'LLM_REQUEST', 'TOOL_CALL', "
        "'AGENT_RESPONSE'). Helps in filtering specific types of "
        "interactions.",
    },
    {
        "name": "agent",
        "type": "STRING",
        "mode": "NULLABLE",
        "description": "The name of the agent that generated this event. Useful for "
        "multi-agent systems.",
    },
    {
        "name": "session_id",
        "type": "STRING",
        "mode": "NULLABLE",
        "description": "A unique identifier for the entire conversation session. Used to "
        "group all events belonging to a single user interaction.",
    },
    {
        "name": "invocation_id",
        "type": "STRING",
        "mode": "NULLABLE",
        "description": "A unique identifier for a single turn or execution within a session. "
        "Groups related events like LLM request and response.",
    },
    {
        "name": "user_id",
        "type": "STRING",
        "mode": "NULLABLE",
        "description": "The identifier of the end-user participating in the session, if "
        "available.",
    },
    {
        "name": "trace_id",
        "type": "STRING",
        "mode": "NULLABLE",
        "description": "OpenTelemetry trace ID for distributed tracing across services.",
    },
    {
        "name": "span_id",
        "type": "STRING",
        "mode": "NULLABLE",
        "description": "BQAA-internal execution-tree span id for this operation. This is the "
        "plugin's own correlation id used with parent_span_id to reconstruct "
        "the agent/LLM/tool tree -- NOT the OpenTelemetry span id, except on "
        "the root/invocation row where it may reuse the ambient OTel span id. "
        "For span-level Cloud Trace correlation use attributes.otel.span_id "
        "(best-effort).",
    },
    {
        "name": "parent_span_id",
        "type": "STRING",
        "mode": "NULLABLE",
        "description": "BQAA-internal parent execution-tree span id, used to reconstruct the "
        "operation hierarchy. Points at another BQAA row, not an "
        "OpenTelemetry parent span.",
    },
    {
        "name": "content",
        "type": "JSON",
        "mode": "NULLABLE",
        "description": "The primary payload of the event, stored as a JSON string. The "
        "structure depends on the event_type (e.g., prompt text for "
        "LLM_REQUEST, tool output for TOOL_RESPONSE).",
    },
    {
        "name": "content_parts",
        "type": "RECORD",
        "mode": "REPEATED",
        "description": "For multi-modal events, contains a list of content parts (text, "
        "images, etc.).",
        "fields": [
            {
                "name": "mime_type",
                "type": "STRING",
                "mode": "NULLABLE",
                "description": "The MIME type of the content part (e.g., 'text/plain', "
                "'image/png').",
            },
            {
                "name": "uri",
                "type": "STRING",
                "mode": "NULLABLE",
                "description": "The URI of the content part if stored externally (e.g., "
                "GCS bucket path).",
            },
            {
                "name": "object_ref",
                "type": "RECORD",
                "mode": "NULLABLE",
                "description": "The ObjectRef of the content part if stored externally.",
                "fields": [
                    {
                        "name": "uri",
                        "type": "STRING",
                        "mode": "NULLABLE",
                        "description": "The URI of the object.",
                    },
                    {
                        "name": "version",
                        "type": "STRING",
                        "mode": "NULLABLE",
                        "description": "The version of the object.",
                    },
                    {
                        "name": "authorizer",
                        "type": "STRING",
                        "mode": "NULLABLE",
                        "description": "The authorizer for the object.",
                    },
                    {
                        "name": "details",
                        "type": "JSON",
                        "mode": "NULLABLE",
                        "description": "Additional details about the object.",
                    },
                ],
            },
            {
                "name": "text",
                "type": "STRING",
                "mode": "NULLABLE",
                "description": "The raw text content if the part is text-based.",
            },
            {
                "name": "part_index",
                "type": "INTEGER",
                "mode": "NULLABLE",
                "description": "The zero-based index of this part within the content.",
            },
            {
                "name": "part_attributes",
                "type": "STRING",
                "mode": "NULLABLE",
                "description": "Additional metadata for this content part as a JSON "
                "object (serialized to string).",
            },
            {
                "name": "storage_mode",
                "type": "STRING",
                "mode": "NULLABLE",
                "description": "Indicates how the content part is stored (e.g., "
                "'INLINE', 'GCS_REFERENCE', 'EXTERNAL_URI').",
            },
        ],
    },
    {
        "name": "attributes",
        "type": "JSON",
        "mode": "NULLABLE",
        "description": "A JSON object containing arbitrary key-value pairs for additional "
        "event metadata. Includes enrichment fields like 'root_agent_name' "
        "(turn orchestration), 'model' (request model), 'model_version' "
        "(response version), and 'usage_metadata' (detailed token counts). "
        "May also carry 'otel' (best-effort ambient Cloud Trace span/trace "
        "ids) and 'custom_metadata' (allowlisted event.custom_metadata "
        "keys).",
    },
    {
        "name": "latency_ms",
        "type": "JSON",
        "mode": "NULLABLE",
        "description": "A JSON object containing latency measurements, such as 'total_ms' "
        "and 'time_to_first_token_ms'.",
    },
    {
        "name": "status",
        "type": "STRING",
        "mode": "NULLABLE",
        "description": "The outcome of the event, typically 'OK' or 'ERROR'.",
    },
    {
        "name": "error_message",
        "type": "STRING",
        "mode": "NULLABLE",
        "description": "Diagnostic message for errors and model termination details; may be "
        "populated on rows whose status is 'OK', such as LLM_RESPONSE rows "
        "and rows whose content_formatter failed.",
    },
    {
        "name": "is_truncated",
        "type": "BOOLEAN",
        "mode": "NULLABLE",
        "description": "Boolean flag indicating if the content or metadata payload was "
        "truncated because it exceeded the maximum allowed size. Set when "
        "'content', captured 'custom_metadata', or A2A metadata is truncated; "
        "redaction of sensitive keys does not set this flag.",
    },
]

VIEW_COMMON_COLUMNS = [
    "timestamp",
    "event_id",
    "event_type",
    "agent",
    "session_id",
    "invocation_id",
    "user_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "status",
    "error_message",
    "is_truncated",
]

EVENT_VIEW_DEFS = {
    "USER_MESSAGE_RECEIVED": [],
    "LLM_REQUEST": [
        "JSON_VALUE(attributes, '$.model') AS model",
        "content AS request_content",
        "JSON_QUERY(attributes, '$.llm_config') AS llm_config",
        "JSON_QUERY(attributes, '$.tools') AS tools",
    ],
    "LLM_RESPONSE": [
        "JSON_QUERY(content, '$.response') AS response",
        "CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64) AS usage_prompt_tokens",
        "CAST(JSON_VALUE(content, '$.usage.completion') AS INT64) AS "
        "usage_completion_tokens",
        "CAST(JSON_VALUE(content, '$.usage.total') AS INT64) AS usage_total_tokens",
        "CAST(JSON_VALUE(attributes, "
        "'$.usage_metadata.cached_content_token_count') AS INT64) AS "
        "usage_cached_tokens",
        "CAST(JSON_VALUE(attributes, "
        "'$.usage_metadata.thoughts_token_count') AS INT64) AS "
        "usage_thinking_tokens",
        "CAST(JSON_VALUE(attributes, "
        "'$.usage_metadata.tool_use_prompt_token_count') AS INT64) AS "
        "usage_tool_use_tokens",
        "SAFE_DIVIDE(CAST(JSON_VALUE(attributes, "
        "'$.usage_metadata.cached_content_token_count') AS "
        "INT64),CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64)) AS "
        "context_cache_hit_rate",
        "CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms",
        "CAST(JSON_VALUE(latency_ms, '$.time_to_first_token_ms') AS INT64) AS ttft_ms",
        "JSON_VALUE(attributes, '$.model_version') AS model_version",
        "JSON_QUERY(attributes, '$.usage_metadata') AS usage_metadata",
        "JSON_QUERY(attributes, '$.cache_metadata') AS cache_metadata",
        "JSON_VALUE(attributes, '$.cache_type') AS cache_type",
        "JSON_VALUE(attributes, '$.finish_reason') AS finish_reason",
    ],
    "LLM_ERROR": ["CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms"],
    "TOOL_STARTING": [
        "JSON_VALUE(content, '$.tool') AS tool_name",
        "JSON_QUERY(content, '$.args') AS tool_args",
        "JSON_VALUE(content, '$.tool_origin') AS tool_origin",
    ],
    "TOOL_COMPLETED": [
        "JSON_VALUE(content, '$.tool') AS tool_name",
        "JSON_QUERY(content, '$.result') AS tool_result",
        "JSON_VALUE(content, '$.tool_origin') AS tool_origin",
        "CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms",
        "JSON_VALUE(attributes, '$.adk.pause_kind') AS pause_kind",
        "JSON_VALUE(attributes, '$.adk.function_call_id') AS function_call_id",
    ],
    "TOOL_ERROR": [
        "JSON_VALUE(content, '$.tool') AS tool_name",
        "JSON_QUERY(content, '$.args') AS tool_args",
        "JSON_VALUE(content, '$.tool_origin') AS tool_origin",
        "CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms",
    ],
    "AGENT_STARTING": [
        "COALESCE(JSON_VALUE(content, '$.text_summary'), "
        "IF(REGEXP_CONTAINS(JSON_VALUE(content, '$'), "
        "r'^(?:<(?:function|bound method) |<.+ at "
        "0x[0-9a-fA-F]+>|functools\\.partial\\()'), NULL, "
        "JSON_VALUE(content, '$'))) AS agent_instruction",
        "JSON_VALUE(attributes, '$.instruction_source') AS instruction_source",
        "JSON_VALUE(attributes, '$.static_instruction') AS static_instruction",
        "JSON_VALUE(attributes, '$.agent_description') AS agent_description",
        "JSON_VALUE(attributes, '$.model') AS model",
        "JSON_QUERY(attributes, '$.sub_agents') AS sub_agents",
    ],
    "AGENT_COMPLETED": [
        "CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms"
    ],
    "AGENT_ERROR": [
        "CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms",
        "JSON_VALUE(content, '$.error_traceback') AS error_traceback",
    ],
    "INVOCATION_STARTING": [],
    "INVOCATION_COMPLETED": [],
    "INVOCATION_ERROR": ["JSON_VALUE(content, '$.error_traceback') AS error_traceback"],
    "STATE_DELTA": ["JSON_QUERY(attributes, '$.state_delta') AS state_delta"],
    "HITL_CREDENTIAL_REQUEST": [
        "JSON_VALUE(content, '$.tool') AS tool_name",
        "JSON_QUERY(content, '$.args') AS tool_args",
    ],
    "HITL_CONFIRMATION_REQUEST": [
        "JSON_VALUE(content, '$.tool') AS tool_name",
        "JSON_QUERY(content, '$.args') AS tool_args",
    ],
    "HITL_INPUT_REQUEST": [
        "JSON_VALUE(content, '$.tool') AS tool_name",
        "JSON_QUERY(content, '$.args') AS tool_args",
    ],
    "A2A_INTERACTION": [
        "content AS response_content",
        "JSON_VALUE(attributes, '$.a2a_metadata.\"a2a:task_id\"') AS a2a_task_id",
        "JSON_VALUE(attributes, '$.a2a_metadata.\"a2a:context_id\"') AS a2a_context_id",
        "JSON_QUERY(attributes, '$.a2a_metadata.\"a2a:request\"') AS a2a_request",
        "JSON_QUERY(attributes, '$.a2a_metadata.\"a2a:response\"') AS a2a_response",
    ],
    "AGENT_RESPONSE": [
        "JSON_VALUE(content, '$.response') AS response_text",
        "JSON_VALUE(attributes, '$.source_event_id') AS source_event_id",
        "JSON_VALUE(attributes, '$.source_event_author') AS source_event_author",
        "JSON_VALUE(attributes, '$.source_event_branch') AS source_event_branch",
    ],
    "AGENT_TRANSFER": [
        "JSON_VALUE(content, '$.from_agent') AS from_agent",
        "JSON_VALUE(content, '$.to_agent') AS to_agent",
        "JSON_VALUE(attributes, '$.adk.source_event_id') AS source_event_id",
    ],
    "EVENT_COMPACTION": [
        "CAST(JSON_VALUE(content, '$.start_timestamp') AS FLOAT64) AS start_seconds",
        "CAST(JSON_VALUE(content, '$.end_timestamp') AS FLOAT64) AS end_seconds",
        "TIMESTAMP_MICROS(CAST(CAST(JSON_VALUE(content, "
        "'$.start_timestamp') AS FLOAT64) * 1000000 AS INT64)) AS "
        "window_start",
        "TIMESTAMP_MICROS(CAST(CAST(JSON_VALUE(content, "
        "'$.end_timestamp') AS FLOAT64) * 1000000 AS INT64)) AS "
        "window_end",
        "JSON_QUERY(content, '$.compacted_content') AS compacted_content",
    ],
    "AGENT_STATE_CHECKPOINT": [
        "JSON_QUERY(content, '$.agent_state') AS agent_state",
        "JSON_TYPE(JSON_QUERY(content, '$.agent_state')) AS agent_state_type",
        "SAFE_CAST(JSON_VALUE(content, '$.end_of_agent') AS BOOL) AS end_of_agent",
        "JSON_VALUE(attributes, '$.adk.source_event_id') AS source_event_id",
    ],
    "TOOL_PAUSED": [
        "JSON_VALUE(content, '$.tool') AS tool_name",
        "JSON_QUERY(content, '$.args') AS tool_args",
        "JSON_VALUE(attributes, '$.adk.pause_kind') AS pause_kind",
        "JSON_VALUE(attributes, '$.adk.function_call_id') AS function_call_id",
    ],
    "NODE_OUTPUT": [
        "JSON_VALUE(attributes, '$.adk.node.path') AS node_path",
        "JSON_VALUE(attributes, '$.adk.node.run_id') AS node_run_id",
        "JSON_VALUE(attributes, '$.adk.node.parent_run_id') AS node_parent_run_id",
        "content AS output",
    ],
    "NODE_ERROR": [
        "JSON_VALUE(attributes, '$.adk.node.path') AS node_path",
        "JSON_VALUE(attributes, '$.adk.node.run_id') AS node_run_id",
        "JSON_VALUE(attributes, '$.adk.node.parent_run_id') AS node_parent_run_id",
        "JSON_VALUE(content, '$.error_code') AS error_code",
    ],
}
