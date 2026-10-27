# From Agent Starter Pack

`agents-cli` is the successor to Agent Starter Pack (ASP). It builds on the same foundation with key improvements.

---

## What Changed

**Coding agent first.** ASP was built for humans running an interactive CLI. agents-cli is built for coding agents — with 7 bundled skills that give them deep context about ADK, evaluation, deployment, and observability. Every command still works from the terminal too.

**CLI replaces Makefile.** ASP used `make` targets (`make dev`, `make eval`, `make deploy`). agents-cli replaces them with a unified CLI covering the full lifecycle, with flags, help text, and structured output.

**New capabilities.** `agents-cli` adds commands that didn't exist in ASP: `playground`, `run`, `deploy`, the full eval surface (`eval generate`, `eval grade`, `eval dataset synthesize`, `eval compare`, `eval analyze`, `eval metric list`, `eval optimize`), `lint`, `login`, and skill management (`setup`, `update`).

### Command Mapping

| Agent Starter Pack | agents-cli |
|---|---|
| `create` | `create` (alias for `scaffold create`) |
| `enhance` | `scaffold enhance` |
| `upgrade` | `scaffold upgrade` |
| `setup-cicd` | `infra cicd` |
| `register-gemini-enterprise` | `publish gemini` |

### Config Key

The configuration moved from `[tool.agent-starter-pack]` in `pyproject.toml` to a dedicated `agents-cli-manifest.yaml`:

**Before (`pyproject.toml`)**
```toml
[tool.agent-starter-pack]
agent_directory = "app"

[tool.agent-starter-pack.create_params]
deployment_target = "cloud_run"
```

**After (`agents-cli-manifest.yaml`)**
```yaml
name: my-agent
agent_directory: app
create_params:
  deployment_target: cloud_run
```

### Template Coverage

agents-cli supports the `adk_python` template (Python), with A2A built into every ADK agent — the standalone `adk_a2a` template was merged into `adk_python`. The `adk` name still works as an alias for `adk_python`. RAG is a clone-and-study recipe rather than a template (the former `agentic_rag` template was removed; adapt the `rag-vector-search` / `rag-agent-search` recipes instead). ASP had additional templates (`adk_go`, `adk_java`, `adk_ts`, `adk_live`, `custom_a2a`) that are not yet available in agents-cli. Support for these is planned.

### What Stays the Same

- **Templates** — same `adk_python` agent template (RAG is now a clone-and-study recipe), same deployment targets, same session storage options
- **Project structure** — generated projects have the same layout, your `app/agent.py` code is unchanged
- **Terraform** — same infrastructure-as-code under `deployment/terraform/`
- **CI/CD pipelines** — same Cloud Build and GitHub Actions configurations



---

## Migrating an Existing Project

Your existing ASP projects are fully compatible. The only required change is renaming the config section in `pyproject.toml`.

**Step 1: Install agents-cli**

```bash
uvx google-agents-cli setup
```

**Step 2: Rename the config section**

Open `pyproject.toml` in your editor and rename only these section headers:

- `[tool.agent-starter-pack]` to `[tool.agents-cli]`
- `[tool.agent-starter-pack.create_params]` to `[tool.agents-cli.create_params]`

**Step 3: Preview and apply the migration**

1. `agents-cli info` reads the current configuration without changing it.
2. `agents-cli scaffold upgrade --dry-run` previews the migration.
3. `agents-cli scaffold upgrade` applies the migration.

The final command writes `agents-cli-manifest.yaml` and removes the legacy
`tool.agents-cli` section from `pyproject.toml`. Your agent code, tests,
Terraform, and CI/CD pipelines continue to work as before.

!!! note "Existing eval cases under `tests/eval/evalsets/`?"
    ASP's default agent template shipped a `basic.evalset.json` using the ADK `EvalSet` schema. The eval surface in agents-cli reads a different format from `tests/eval/datasets/`. See [Migrating Eval Datasets](eval-dataset-migration.md) for the conversion.
