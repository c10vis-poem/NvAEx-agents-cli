# Migrating a Claude agent that runs on AWS

Extra steps for `migrate-claude-agent.md` when the agent runs on AWS today. Add the rows in
step 4 to its Assess table and show the user the table first. Then do steps 1 to 3 here,
before that guide's step 4 (Port), and use the rest during its steps 5 to 7.

Signs the agent runs on AWS: `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_MANTLE` or
`CLAUDE_CODE_USE_ANTHROPIC_AWS` anywhere; model IDs such as `us.anthropic.claude-...-v1:0`
or `arn:aws:bedrock:...`; `bedrock_agentcore` imports, `.bedrock_agentcore.yaml` or
`agentcore/agentcore.json`; a Lambda `handler(event, context)`; `boto3` in tools; ECS task
definitions, CDK, SAM or CloudFormation files.

## 1. Remove the AWS model configuration

Leftover AWS settings beat the Gemini platform setup: with `CLAUDE_CODE_USE_BEDROCK` set, the CLI
calls Bedrock even on Cloud Run or Agent Runtime, and a value in `agent_config/.claude/settings.json` overrides the
process environment.

Search `.env*`, `Dockerfile*`, `.claude/settings*.json` (before copying them to `agent_config/`), `root_agent.env` and the options
builder for these, and remove them from everything the GCP deployment reads:

- `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_MANTLE`, `CLAUDE_CODE_USE_ANTHROPIC_AWS`
- `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`,
  `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL` (keep the model they name
  for step 2)
- `ANTHROPIC_BEDROCK_*`, `ANTHROPIC_CUSTOM_HEADERS` with Bedrock Guardrail headers,
  `AWS_BEARER_TOKEN_BEDROCK`, `modelOverrides`, `awsAuthRefresh`, `awsCredentialExport`

Files that only the AWS deployment reads (CDK, task definitions, `agentcore.json`) can keep
them.

Check, after `migrate-claude-agent.md` step 4 has written `app/agent.py`, that this prints nothing:

```bash
grep -rnE "CLAUDE_CODE_USE_(BEDROCK|MANTLE|ANTHROPIC_AWS)|anthropic\.claude-|arn:aws:bedrock" \
  .env* .claude app | grep -vE ':[0-9]+:\s*#'
```

Don't quote Bedrock IDs in code or comments under `app/`.

## 2. Translate the model

Bedrock model IDs fail on Gemini platform with `400 Invalid endpoint name`. Keep the same model
family, version and tier, under its Gemini platform name, and put it in `root_agent.model`, also
when it came from `ANTHROPIC_MODEL`. Leaving `model` unset makes the CLI use its Gemini
platform default, which is a different and more expensive model.

To translate: drop the geo prefix (`us.`, `eu.`, `apac.`, `jp.`, `au.`, `global.`,
`us-gov.`) and `anthropic.`, drop the `-vN:M` suffix, and drop the date or write it as
`@YYYYMMDD`:

| Bedrock | Gemini platform |
|---|---|
| `us.anthropic.claude-haiku-4-5-20251001-v1:0` | `claude-haiku-4-5` or `claude-haiku-4-5@20251001` |
| `anthropic.claude-sonnet-4-5-20250929-v1:0` | `claude-sonnet-4-5` |

Apply the same mapping to subagent `model:` fields. Older models have different Gemini
platform names, so check the Model Garden model list. Ask the user when the ID is an ARN (application
inference profile, provisioned throughput), or when the model isn't enabled in Model
Garden for the target project. After the translation, `migrate-claude-agent.md`'s "Never
change the model" applies to the Gemini platform name.

## 3. Remove the AWS deployment files

An AWS root `Dockerfile` has the wrong command, Bedrock settings, and for AgentCore an
arm64 image that Cloud Run and Agent Runtime can't run. `enhance` moves it and
`.dockerignore` to `.agents-cli-original/`: don't port them back.

Delete every other file only the AWS deployment reads, so it is neither uploaded nor built
into the image. `enhance` copied the whole project to `~/.agents-cli/backups/` before it
changed anything, and git history keeps them too:

- Infrastructure: SAM `template.yaml` and `samconfig.toml`, CDK apps (`cdk.json` and the
  stacks it runs), CloudFormation templates, ECS task definitions, `serverless.yml`,
  `Procfile`, `apprunner.yaml`.
- AgentCore: `.bedrock_agentcore.yaml` and the `agentcore/` folder.
- Entrypoints only that runtime calls: a Lambda `handler(event, context)` module,
  `BedrockAgentCoreApp` entrypoints. Keep any module that defines the agent's tools or
  options: `migrate-claude-agent.md` step 4 imports them.
- CI workflows that deploy to AWS (`.github/workflows/*` running `sam deploy`,
  `cdk deploy`, `agentcore deploy`, `aws ecs update-service`): left in place, they keep
  deploying the old stack on every push.

List the files to delete and the backup path `enhance` printed, and ask the user before
deleting.

Check, after `enhance`: `grep -q "app.fast_api_app" Dockerfile && ! grep -q "linux/arm64" Dockerfile`,
and `! grep -rlE "sam deploy|cdk deploy|agentcore deploy|ecs update-service" .github/workflows`.

`agents-cli deploy` copies every `.env` entry into the service as a plain environment
variable. Delete AWS keys (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`) and other secrets from `.env`. Store secrets the agent still needs in
Secret Manager and pass them with `agents-cli deploy --secrets ENV=SECRET`.

## 4. Assess rows for AWS

Add these to the Assess table:

| Finding | Consequence |
|---|---|
| AgentCore `@app.entrypoint` or Lambda `handler(event, context)` | Lift the options builder into `root_agent`. Anything built from the payload or context per request, memory reads before a turn and memory writes after it, is code between turns: not supported as-is. Tell the user |
| Own FastAPI or Flask server calling `query()` | Lift the options. A2A replaces the routes, so callers change (see Callers) |
| Agent code in a subdirectory with flat imports | Make it importable from `app/agent.py` (add the directory to `sys.path` there) instead of moving files |
| `session_store=` on S3, Redis or DynamoDB | Recommend `--session-type cloud_sql` instead (Cloud Run; on Agent Runtime it needs manual setup, see `migrate-claude-agent.md` step 3). Keeping it needs AWS credentials and adds cross-cloud latency to every turn. Existing conversations don't carry over either way |
| AgentCore Memory | No equivalent. Tell the user |
| Tools calling AWS (`boto3`) | They need AWS credentials on GCP: keys in Secret Manager, or an AWS IAM role that trusts Google's identity token. Tell the user; adds egress cost and latency |
| Secrets read from Secrets Manager or SSM | Move them to Secret Manager and read an env var (`deploy --secrets`) |
| Bedrock geo profile in the model ID, or a data-residency need | `us.` maps to `CLOUD_ML_REGION=us` and `eu.` to `eu` (set it in `.env`, and check the model is offered there; on a `429` quota error there, use a single region within it, such as `us-east5` for `us`); `global.` keeps the default `global`, which may process data anywhere. Ask about `apac.`, `jp.`, `au.` and `us-gov.` |
| AWS `OTEL_*`, `CLAUDE_CODE_ENABLE_TELEMETRY`, `opentelemetry-instrument`, `aws-opentelemetry-distro` | Remove them; they stop the CLI's spans reaching Cloud Trace |
| Bedrock Guardrails | Don't carry over. Tell the user |
| Calls to agents that stay on AWS | `remote_agent` sends Google ID tokens or static headers; no SigV4 or token refresh. Tell the user |

## 5. Before cutover

- The model is enabled in Model Garden for the target project; enabling can take a day or
  two.
- Gemini platform quota for that model and region covers the tokens per minute the agent uses on
  Bedrock.

## Callers

Clients that call the AWS endpoint must switch to A2A: on Cloud Run at
`<service-url>/a2a/app` with a Google ID token, on Agent Runtime at the agent's
passthrough URL (`.../reasoningEngines/<id>/api/a2a/app`) with an OAuth access token.
Callers running on AWS can get either through Workload Identity Federation with an AWS
provider.

## Out of scope

AgentCore Memory, Gateway and Identity equivalents; migrating stored transcripts or memory
records; SigV4; converting AWS infrastructure code or pipelines; per-session microVM
isolation; runs longer than the platform's request timeout.
