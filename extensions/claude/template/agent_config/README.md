# agent_config

Claude Code settings for the served agent. Each conversation loads them as
project settings: put the agent's `CLAUDE.md`, `.claude/` (subagents in
`.claude/agents/`, skills, `settings.json`) and `.mcp.json` here.

The `CLAUDE.md`, `.claude/` and `.mcp.json` at the project root belong to your own
coding assistant: the served agent never reads them, and they are not built into the
image.
