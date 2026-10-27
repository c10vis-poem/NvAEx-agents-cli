# Coding Agent Guide

This is a Claude Agent SDK project, not an ADK one: `app/agent.py` declares
`root_agent` (a `ClaudeAgentOptions`), `app/app_utils/claude.py` runs it, and
`app/fast_api_app.py` serves it over A2A. Read
`.agents/skills/agents-cli-claude/SKILL.md` before changing any of them.

> **Experimental.** Agent Runtime and Cloud Run are the supported targets. Models
> are Claude on Gemini platform: the model named in `app/agent.py` must be enabled in Model
> Garden for the project. `agents-cli playground`, `eval dataset synthesize` and `eval
> optimize` need ADK, so this project refuses them, as does `publish gemini`
> on Agent Runtime.

## The loop

1. Build in `app/agent.py`, try it with `agents-cli run "prompt"`.
2. Write eval cases by hand in `tests/eval/datasets/`, then `agents-cli eval run` until it
   holds up. `agents-cli eval compare` shows regressions; `agents-cli eval analyze` clusters
   failures.
3. `uv run pytest tests/integration` before deploying.
4. `agents-cli infra single-project --apply` and `agents-cli deploy` **only after the user
   approves them.**

## Rules

- Change only what was asked. Keep surrounding code, comments, and config values.
- **Never change the model** unless asked. On "model not available on your vertex
  deployment", the model is not enabled for the project; ask the user to enable it.
- List the agent's tools explicitly (`tools=[...]`, `allowed_tools=[...]`); never give a
  served agent Claude Code's `Bash` or file-editing tools unless the user asks.
- Run Python through uv: `uv run python script.py`, after `agents-cli install`.
- Same error three times means fix the cause, not retry.
- Terraform 409: `terraform import`, don't recreate.
