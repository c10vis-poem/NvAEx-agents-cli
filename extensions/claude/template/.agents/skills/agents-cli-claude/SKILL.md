---
name: agents-cli-claude
description: >
  Use when working in this project — adding tools, editing the agent, running,
  evaluating, serving or deploying it — or when guidance mentions ADK, LlmAgent,
  google.adk, adk web, or an ADK runner. This project is a Claude Agent SDK
  agent scaffolded by agents-cli, so ADK-specific instructions do not apply.
---

# Claude Agent SDK project (agents-cli)

The agent is a `ClaudeAgentOptions` exported as `root_agent` from
`app/agent.py`. There is no `google.adk` dependency and no ADK runner. Other
`google-agents-cli-*` skills assume ADK; where they describe the agent itself,
this skill wins.

**Experimental.** Deploy to agent_runtime (limits: `references/claude.md`) or cloud_run.
The model runs on Claude on Gemini platform and must be enabled in Model Garden.

## What ADK guidance maps to here

| ADK guidance | This project |
|---|---|
| `LlmAgent`, `Agent` | `ClaudeAgentOptions(model=..., system_prompt=..., ...)` assigned to `root_agent` |
| `Agent(name=...)` | `AGENT_NAME` in `app/app_utils/settings.py` (`ClaudeAgentOptions` has no name); keep it equal to `create_params.root_agent_name` in `agents-cli-manifest.yaml` |
| `google.adk.tools`, `FunctionTool` | `@tool(...)` async functions, served by `create_sdk_mcp_server` and passed in `mcp_servers` |
| `adk web`, `agents-cli playground` | Unavailable (no SDK dev UI). Use `agents-cli run`, or `uv run uvicorn app.fast_api_app:app --port 8080` |
| ADK runner behind `agents-cli run` | `agents-cli run` runs `root_agent` in process |
| `agents-cli eval dataset synthesize`, `eval optimize` | Unavailable: both drive the agent through ADK. The command says so and exits |
| `agents-cli eval generate --url` (traces from a served agent) | Unavailable: the server speaks A2A, not ADK's `/run_sse`. `eval generate` runs `root_agent` in process; the command refuses `--url` |
| ADK sessions | Claude sessions, one per A2A conversation; scaffold with `--session-type cloud_sql` to keep them across instances |

## The contract

- `app/agent.py` exports `root_agent: ClaudeAgentOptions`. Only it changes when
  you change the agent.
- `app/app_utils/claude.py` runs it. Leave `app/app_utils/` alone.
- The served agent's own `CLAUDE.md`, `.claude/` and `.mcp.json` go in `agent_config/`.
- `app/fast_api_app.py` exposes `app`. Every deployment target runs
  `uvicorn app.fast_api_app:app`.

## Adding a tool

```python
@tool("lookup_order", "Look up an order by id.", {"order_id": str})
async def lookup_order(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Order {args['order_id']}: shipped"}]}


server = create_sdk_mcp_server(name="orders", tools=[lookup_order])

root_agent = ClaudeAgentOptions(
    ...,
    mcp_servers={"orders": server},
    tools=[],  # no built-in Claude Code tools
    allowed_tools=["mcp__orders__lookup_order"],  # mcp__<server>__<tool>
)
```

A tool missing from `allowed_tools` is not available to the agent. Subagents (`agents=`
or `agent_config/.claude/agents/`) run through the built-in `Agent` tool: add `"Agent"` to both
`tools` and `allowed_tools`. Built-in tools
(`WebFetch`, `WebSearch`, `Read`, `Bash`, ...) go in both `tools` and `allowed_tools`;
keep `Bash` and file-editing tools out of a served agent unless the user asks.

## Commands

```bash
agents-cli install                  # uv sync
agents-cli run "hello"              # run root_agent in process; prints a session id
agents-cli run "and?" --session-id <id>   # continue that conversation
agents-cli eval generate --dataset tests/eval/datasets/basic-dataset.json -o tests/eval/output/
agents-cli eval grade --traces tests/eval/output/<dataset>.json --config tests/eval/eval_config.yaml
agents-cli deploy
agents-cli scaffold enhance -d cloud_run --cicd-runner github_actions   # add infra later
```

`run`, `eval generate`, `publish gemini`, `playground`, `eval dataset` and
`eval optimize` are overridden by `agents-cli-extension.yaml` at the project root.
Prefix any command with `AGENTS_CLI_DISABLE_OVERRIDES=1` to reach the built-in instead.

## Serving

A2A: JSON-RPC at `POST /a2a/app`, card at `/a2a/app/.well-known/agent-card.json`,
health at `/health`; over A2A, text streams as it is generated. On Agent Runtime, also
`:query` and `:streamQuery` (new sessions and turns), which `agents-cli run --mode adk`
and an ingress Agent Gateway use. See `references/claude.md` for sessions, telemetry
and deployment.

## Common mistakes

- Renaming `root_agent` or `app`, which breaks `run`, eval and deploy.
- Leaving `tools` unset: the agent then gets every built-in Claude Code tool.
- Using a model id that is not enabled on Gemini platform for the project.
- Reaching for `eval dataset synthesize` or `eval optimize`: they need ADK.
  Write cases into `tests/eval/datasets/` and use `eval generate` + `eval grade`.
- Expecting `/run_sse` or ADK's REST session routes; this server serves A2A and
  Agent Runtime's `:query` / `:streamQuery`.

## References

- `references/claude.md` — how a turn runs, model access, project settings, sessions,
  calling other agents, telemetry and logs.
- `references/migrate-claude-agent.md` — moving an existing Claude Agent SDK agent into
  this project and onto GCP.
- `references/migrate-from-aws.md` — extra migration steps for an agent running on AWS.
