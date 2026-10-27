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

"""Read-only checks that the Google Cloud requests a deploy will make would succeed."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum

import click
import requests
from google.genai.errors import APIError

from google.agents.cli._agent_platform import AgentPlatformClient
from google.agents.cli._gcp_project import get_gcp_project_number
from google.agents.cli.auth import get_access_token
from google.agents.cli.deploy._utils import cloud_run_service_url, parse_secret_refs
from google.agents.cli.deploy.agent_runtime import (
    _resolve_target_agent,
    should_use_agent_identity,
)

# (connect, read) seconds, so an unreachable host fails fast.
_REQUEST_TIMEOUT = (5, 30)
_RETRY_STATUS_CODES = (429, 503)
_RETRY_DELAY_SECONDS = 1

_SECRET_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,255}")

_VALIDATION_HEADER = "\n🔎 Validating the deployment (no resources are changed)..."

# Continues a multi-line detail under its check in the report.
_DETAIL_INDENT = "\n      "

_AGENT_RUNTIME_API = "aiplatform.googleapis.com"
_CLOUD_RUN_API = "run.googleapis.com"
_CLOUD_BUILD_API = "cloudbuild.googleapis.com"
_ARTIFACT_REGISTRY_API = "artifactregistry.googleapis.com"
_SECRET_MANAGER_API = "secretmanager.googleapis.com"
_CREATE_SERVICE_PERMISSION = "run.services.create"
_UPDATE_SERVICE_PERMISSION = "run.services.update"
_CLOUD_BUILD_PERMISSION = "cloudbuild.builds.create"
_CREATE_ENGINE_PERMISSION = "aiplatform.reasoningEngines.create"
_UPDATE_ENGINE_PERMISSION = "aiplatform.reasoningEngines.update"
_GET_PROJECT_IAM_POLICY_PERMISSION = "resourcemanager.projects.getIamPolicy"
_SET_PROJECT_IAM_POLICY_PERMISSION = "resourcemanager.projects.setIamPolicy"
_ACT_AS_PERMISSION = "iam.serviceAccounts.actAs"


class CheckStatus(Enum):
    """Outcome of a single pre-deploy check."""

    PASSED = "passed"
    WARNED = "warned"
    FAILED = "failed"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class ValidationReport:
    """The check results, already printed. A dry run prints its plan, then
    calls ``raise_if_failed``."""

    results: tuple[CheckResult, ...]

    def raise_if_failed(self) -> None:
        """Stop the command if a check failed, else warn if a check only warned."""
        failures = [r for r in self.results if r.status is CheckStatus.FAILED]
        if failures:
            raise click.ClickException(
                f"Pre-deploy validation failed ({len(failures)} of "
                f"{len(self.results)} checks): "
                f"{', '.join(r.name for r in failures)}.\n"
                "  Fix the ❌ items, then run the dry run again."
            )
        if any(r.status is CheckStatus.WARNED for r in self.results):
            logging.warning("Some checks only warned, so the deploy may still fail.")


def validate_agent_runtime_deployment(
    *,
    project: str,
    region: str,
    display_name: str,
    service_account: str | None,
    secrets: Mapping[str, str],
    update_only: bool,
    agent_identity: bool | None,
    identity_type_default: str,
) -> ValidationReport:
    """Check what an Agent Runtime deploy needs, and print the results."""
    click.echo(_VALIDATION_HEADER)
    secret_ids_result = (
        _check_agent_runtime_secret_ids(project=project, secrets=secrets)
        if secrets
        else None
    )
    token = _get_token()
    if token is None:
        return _report_results(
            [*filter(None, [secret_ids_result]), _no_credentials_result()]
        )

    apis = (_AGENT_RUNTIME_API,)
    if secrets:
        apis += (_SECRET_MANAGER_API,)
    apis_result, disabled_apis = _check_apis(project=project, apis=apis, token=token)
    results = [apis_result]

    engine, engine_known = None, False
    if _AGENT_RUNTIME_API not in disabled_apis:
        engine_result, engine, engine_known = _check_target_engine(
            project=project,
            region=region,
            display_name=display_name,
            update_only=update_only,
        )
        results.append(engine_result)
    # None when the engine lookup could not run.
    will_create = (engine is None) if engine_known else None

    uses_agent_identity = should_use_agent_identity(
        agent_identity_flag=agent_identity,
        identity_default=identity_type_default,
        is_update=not will_create,
    )

    results.append(
        _iam_permissions_result(
            project=project,
            token=token,
            permissions=_agent_runtime_permissions(
                creates=will_create,
                update_only=update_only,
                agent_identity=uses_agent_identity,
            ),
            resource=_engine_iam_resource(region=region, engine=engine)
            if engine
            else None,
        )
    )

    # With Agent Identity the agent is its own principal and the deploy drops
    # --service-account. Checking that account would fail on one the deploy
    # never uses.
    if service_account and not uses_agent_identity:
        results.append(
            _run_check(
                "Service account",
                lambda: _check_service_account(
                    service_account=service_account, token=token
                ),
            )
        )

    if secret_ids_result:
        results.append(secret_ids_result)
    elif secrets:
        results.extend(
            _secret_version_checks(
                project=project,
                secrets=secrets,
                token=token,
                disabled_apis=disabled_apis,
                runtime_identity="the runtime principal",
            )
        )
    return _report_results(results)


def validate_cloud_run_deployment(
    *,
    project: str,
    region: str | None,
    service_name: str,
    service_account: str | None,
    secrets: Mapping[str, str],
    image: str | None,
) -> tuple[ValidationReport, bool | None]:
    """Check what a Cloud Run deploy needs, and print the results.

    ``image`` is a prebuilt container, so the deploy skips the build. With no
    ``region``, gcloud picks one. Also returns whether the service exists, or
    None when that is unknown.
    """
    click.echo(_VALIDATION_HEADER)
    token = _get_token()
    if token is None:
        return _report_results([_no_credentials_result()]), None

    builds_image = not image
    enabled_by_deploy = (_ARTIFACT_REGISTRY_API, _CLOUD_BUILD_API) if builds_image else ()
    apis = (_CLOUD_RUN_API, *enabled_by_deploy)
    if secrets and _has_secret_in_deploy_project(secrets):
        apis += (_SECRET_MANAGER_API,)
    apis_result, disabled_apis = _check_apis(
        project=project, apis=apis, token=token, enabled_by_deploy=enabled_by_deploy
    )
    results = [apis_result]

    service_exists, current_account = None, None
    if not region:
        results.append(
            _warn_result(
                "Target service",
                "not looked up, because no region is set, so gcloud uses its "
                "run/region setting, or asks for a region when that is not set — "
                "set region in agents-cli-manifest.yaml, or pass --region",
            )
        )
    elif _CLOUD_RUN_API not in disabled_apis:
        service_result, service_exists, current_account = _check_cloud_run_service(
            project=project, region=region, service_name=service_name, token=token
        )
        results.append(service_result)

    results.append(
        _iam_permissions_result(
            project=project,
            token=token,
            permissions=_cloud_run_permissions(
                service_exists=service_exists,
                builds_image=builds_image,
            ),
            resource=_IamResource(
                kind="service",
                url=cloud_run_service_url(
                    project=project, region=region, service=service_name
                )
                + ":testIamPermissions",
                permission=_UPDATE_SERVICE_PERMISSION,
            )
            if service_exists and region
            else None,
        )
    )
    results.extend(
        _cloud_run_service_account_checks(
            service_account=service_account,
            current_account=current_account,
            service_exists=service_exists,
            project=project,
            token=token,
        )
    )
    if secrets:
        results.extend(
            _secret_version_checks(
                project=project,
                secrets=secrets,
                token=token,
                disabled_apis=disabled_apis,
                runtime_identity="the service account of the service",
            )
        )
    return _report_results(results), service_exists


_STATUS_ICONS = {
    CheckStatus.PASSED: "✅",
    CheckStatus.WARNED: "⚠️ ",
    CheckStatus.FAILED: "❌",
}


@dataclass(frozen=True)
class _Permissions:
    """Permissions to test. A missing ``required`` one fails, a missing
    ``maybe_required`` one only warns."""

    required: tuple[str, ...] = ()
    maybe_required: tuple[str, ...] = ()


@dataclass(frozen=True)
class _IamResource:
    """A resource that can grant ``permission`` on itself, apart from the project."""

    kind: str
    url: str
    permission: str


@dataclass
class _States:
    """Labels sorted by what a batch of state lookups returned.

    ``denied`` holds the labels of a 403, ``unreadable`` the labels of any other
    error, with the HTTP code.
    """

    not_found: list[str] = field(default_factory=list)
    not_enabled: dict[str, str] = field(default_factory=dict)
    denied: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def _pass_result(name: str, detail: str) -> CheckResult:
    return CheckResult(name, CheckStatus.PASSED, detail)


def _warn_result(name: str, detail: str) -> CheckResult:
    return CheckResult(name, CheckStatus.WARNED, detail)


def _fail_result(name: str, detail: str) -> CheckResult:
    return CheckResult(name, CheckStatus.FAILED, detail)


def _no_credentials_result() -> CheckResult:
    return _warn_result(
        "Google Cloud credentials",
        "no access token, so the Google Cloud checks were skipped — run "
        "`agents-cli login`",
    )


def _combined_result(
    name: str, *, failures: list[str], warnings: list[str], passed: str
) -> CheckResult:
    """Fail if anything failed, else warn if anything warned, else pass.

    A failure also lists the warnings, so one run shows everything to fix.
    """
    if failures:
        return _fail_result(name, _DETAIL_INDENT.join(failures + warnings))
    if warnings:
        return _warn_result(name, _DETAIL_INDENT.join(warnings))
    return _pass_result(name, passed)


def _describe_problem(
    *, prefix: str, items: list[str], hint: str | None = None
) -> list[str]:
    """One report line naming ``items``, or no line when there are none."""
    if not items:
        return []
    line = f"{prefix}: {', '.join(items)}"
    return [f"{line} — {hint}" if hint else line]


def _run_check(name: str, check: Callable[[], CheckResult]) -> CheckResult:
    """Run one check. A ``ClickException`` fails it, any other error only warns.

    The deploy raises the same ``ClickException`` for the same problem. Any
    other error, such as no network, says nothing about the deploy.
    """
    try:
        return check()
    except click.ClickException as exc:
        return _fail_result(name, exc.format_message())
    except Exception as exc:
        logging.debug("Validation check %r could not run", name, exc_info=exc)
        return _warn_result(name, f"could not verify — {type(exc).__name__}: {exc}")


def _call_api(*, url: str, token: str, json: dict | None = None) -> requests.Response:
    """GET ``url``, or POST ``json`` to it when given. Retries once when throttled."""
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(2):
        if json is None:
            resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        else:
            resp = requests.post(
                url, headers=headers, json=json, timeout=_REQUEST_TIMEOUT
            )
        if attempt or resp.status_code not in _RETRY_STATUS_CODES:
            break
        time.sleep(_RETRY_DELAY_SECONDS)
    if not resp.ok:
        logging.debug("Validation request to %s returned HTTP %s", url, resp.status_code)
    return resp


def _read_states(*, urls: dict[str, str], token: str) -> _States:
    """GET each labeled URL and sort the label by the ``state`` it reports."""
    states = _States()
    for label, url in urls.items():
        resp = _call_api(url=url, token=token)
        if resp.status_code == 404:
            states.not_found.append(label)
        elif resp.status_code == 403:
            states.denied.append(label)
        elif not resp.ok:
            states.unreadable.append(f"{label} (HTTP {resp.status_code})")
        elif (state := resp.json().get("state")) != "ENABLED":
            states.not_enabled[label] = state
    return states


def _api_error_field(resp: requests.Response, key: str) -> str | None:
    """Read ``error.<key>`` from a Google API error body, or None."""
    try:
        return resp.json()["error"][key]
    except (ValueError, KeyError, TypeError):
        return None


def _get_token() -> str | None:
    """Return the access token, or None when there are no credentials."""
    try:
        return get_access_token()
    except Exception as exc:
        logging.debug("Validation checks have no access token", exc_info=exc)
        return None


def _check_apis(
    *,
    project: str,
    apis: tuple[str, ...],
    token: str,
    enabled_by_deploy: tuple[str, ...] = (),
) -> tuple[CheckResult, list[str]]:
    """Return the check result and the APIs found disabled.

    An API in ``enabled_by_deploy`` only warns, because gcloud asks to enable it
    during the deploy.
    """
    disabled: list[str] = []

    def check() -> CheckResult:
        states = _read_states(
            urls={
                api: f"https://serviceusage.googleapis.com/v1/projects/{project}/services/{api}"
                for api in apis
            },
            token=token,
        )
        disabled.extend(states.not_enabled)
        must_enable = [api for api in disabled if api not in enabled_by_deploy]
        offered = [api for api in disabled if api in enabled_by_deploy]
        return _combined_result(
            "APIs enabled",
            failures=_describe_problem(
                prefix="not enabled",
                items=must_enable,
                hint=f"enable with:{_DETAIL_INDENT}"
                f"gcloud services enable {' '.join(must_enable)} --project={project}",
            ),
            warnings=[
                *_describe_problem(
                    prefix="not enabled",
                    items=offered,
                    hint="gcloud asks to enable them during the deploy",
                ),
                *_describe_problem(
                    prefix="could not verify",
                    items=[f"{api} (HTTP 403)" for api in states.denied],
                    hint="check --project, or ask for "
                    "roles/serviceusage.serviceUsageViewer so the dry run can check "
                    "the APIs",
                ),
                *_describe_problem(
                    prefix="could not verify",
                    items=[
                        *states.unreadable,
                        *(f"{api} (HTTP 404)" for api in states.not_found),
                    ],
                ),
            ],
            passed=", ".join(apis),
        )

    return _run_check("APIs enabled", check), disabled


def _check_target_engine(
    *, project: str, region: str, display_name: str, update_only: bool
) -> tuple[CheckResult, str | None, bool]:
    """Find the engine the deploy will update, with the deploy's own lookup.

    Returns the result, the engine to update (None to create one), and whether
    the lookup gave an answer.
    """
    found: list[str] = []

    def check() -> CheckResult:
        client = AgentPlatformClient(project=project, location=region)
        try:
            agents = _resolve_target_agent(client, display_name)
        except click.ClickException as exc:
            code = _transient_api_error_code(exc)
            if code is None:
                raise
            logging.debug("Target engine lookup returned HTTP %s", code, exc_info=exc)
            return _warn_result("Target engine", f"could not verify — HTTP {code}")
        found.extend(agent.api_resource.name for agent in agents)
        if found:
            return _pass_result("Target engine", f"updates {found[0]}")
        if update_only:
            return _fail_result(
                "Target engine",
                f"no engine named '{display_name}' in {project}/{region}, and "
                "--update-only forbids creating one",
            )
        return _pass_result(
            "Target engine", f"creates a new engine named '{display_name}'"
        )

    result = _run_check("Target engine", check)
    engine = found[0] if found else None
    return result, engine, result.status is CheckStatus.PASSED


def _transient_api_error_code(exc: click.ClickException) -> int | None:
    """The HTTP code when ``exc`` wraps a transient API error.

    ``APIError.code`` is None when the error response has no code.
    """
    cause = exc.__cause__
    if (
        isinstance(cause, APIError)
        and cause.code is not None
        and _is_transient_status(cause.code)
    ):
        return cause.code
    return None


def _is_transient_status(code: int) -> bool:
    """Throttled (429) or server-side (5xx), which says nothing about the request."""
    return code == 429 or code >= 500


def _agent_runtime_permissions(
    *, creates: bool | None, update_only: bool, agent_identity: bool | None
) -> _Permissions:
    """The permissions the deploy needs. ``creates`` is None when it is unknown.

    When it is unknown, create and update only warn: the engine check has
    already warned that the lookup could not run. Agent Identity creates a new engine bare, updates it, and then edits the
    project IAM policy to grant the agent its roles. An existing engine edits
    the policy only when it moves to Agent Identity, so that only warns.
    """
    iam_policy = (_GET_PROJECT_IAM_POLICY_PERMISSION, _SET_PROJECT_IAM_POLICY_PERMISSION)
    if creates is None and not update_only:
        if agent_identity:
            return _Permissions(
                required=(_UPDATE_ENGINE_PERMISSION,),
                maybe_required=(_CREATE_ENGINE_PERMISSION, *iam_policy),
            )
        return _Permissions(
            maybe_required=(_CREATE_ENGINE_PERMISSION, _UPDATE_ENGINE_PERMISSION)
        )
    if creates and agent_identity:
        return _Permissions(
            required=(_CREATE_ENGINE_PERMISSION, _UPDATE_ENGINE_PERMISSION, *iam_policy)
        )
    if creates:
        return _Permissions(required=(_CREATE_ENGINE_PERMISSION,))
    return _Permissions(
        required=(_UPDATE_ENGINE_PERMISSION,),
        maybe_required=iam_policy if agent_identity else (),
    )


def _engine_iam_resource(*, region: str, engine: str) -> _IamResource:
    return _IamResource(
        kind="engine",
        url=f"https://{region}-aiplatform.googleapis.com/v1/{engine}:testIamPermissions",
        permission=_UPDATE_ENGINE_PERMISSION,
    )


def _check_cloud_run_service(
    *, project: str, region: str, service_name: str, token: str
) -> tuple[CheckResult, bool | None, str | None]:
    """Look up the service the deploy writes to.

    Returns the result, whether the service exists (None when unknown), and the
    service account it runs as.
    """
    exists: bool | None = None
    account: str | None = None

    def check() -> CheckResult:
        nonlocal exists, account
        resp = _call_api(
            url=cloud_run_service_url(
                project=project, region=region, service=service_name
            ),
            token=token,
        )
        if resp.status_code == 404:
            # A host for a region that does not exist answers with an HTML 404.
            if _api_error_field(resp, "status") != "NOT_FOUND":
                return _fail_result(
                    "Target service",
                    f"Cloud Run has no region '{region}' — check --region",
                )
            exists = False
            return _pass_result(
                "Target service", f"creates a new service named '{service_name}'"
            )
        if _is_transient_status(resp.status_code):
            return _warn_result(
                "Target service", f"could not verify — HTTP {resp.status_code}"
            )
        if not resp.ok:
            detail = (
                f"Cloud Run rejected the service lookup (HTTP {resp.status_code}). "
                "The deploy does the same lookup first, so it would stop here too "
                "— check --project and --region, and that you have run.services.get"
            )
            if message := _api_error_field(resp, "message"):
                detail += f"{_DETAIL_INDENT}{message}"
            return _fail_result("Target service", detail)
        account = (resp.json().get("template") or {}).get("serviceAccount")
        exists = True
        return _pass_result("Target service", f"updates service '{service_name}'")

    result = _run_check("Target service", check)
    return result, exists, account


def _cloud_run_permissions(
    *, service_exists: bool | None, builds_image: bool
) -> _Permissions:
    """Create for a new service, update for an existing one, either when unknown.

    A source deploy also builds the image.
    """
    build = (_CLOUD_BUILD_PERMISSION,) if builds_image else ()
    if service_exists is None:
        return _Permissions(
            required=build,
            maybe_required=(_CREATE_SERVICE_PERMISSION, _UPDATE_SERVICE_PERMISSION),
        )
    write = _UPDATE_SERVICE_PERMISSION if service_exists else _CREATE_SERVICE_PERMISSION
    return _Permissions(required=(write, *build))


def _iam_permissions_result(
    *,
    project: str,
    token: str,
    permissions: _Permissions,
    resource: _IamResource | None,
) -> CheckResult:
    return _run_check(
        "IAM permissions",
        lambda: _check_permissions(
            project=project,
            token=token,
            permissions=permissions,
            resource=resource,
        ),
    )


def _check_permissions(
    *,
    project: str,
    token: str,
    permissions: _Permissions,
    resource: _IamResource | None,
) -> CheckResult:
    """Test the permissions on the project.

    ``resource.permission`` granted on the resource itself also counts: a user
    can hold it on a single resource and not on the project.
    """
    requested = permissions.required + permissions.maybe_required
    resp = _call_api(
        url=f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:testIamPermissions",
        token=token,
        json={"permissions": list(requested)},
    )
    if not resp.ok:
        return _warn_result(
            "IAM permissions", f"could not verify — HTTP {resp.status_code}"
        )
    granted = set(resp.json().get("permissions", []))
    unverified: list[str] = []
    unverified_hint = None
    if resource and resource.permission in set(requested) - granted:
        resource_granted, problem = _resource_grants(resource=resource, token=token)
        granted |= resource_granted
        if problem:
            unverified = [resource.permission]
            unverified_hint = (
                f"not granted on the project, and the {resource.kind} could not "
                f"be checked ({problem})"
            )
    # testIamPermissions grants nothing on a project that does not exist.
    ask_admin = "ask a project admin for a role that grants it"
    if not granted:
        ask_admin = f"check that '{project}' is the right project ID, or {ask_admin}"
    missing = [
        p for p in permissions.required if p not in granted and p not in unverified
    ]
    return _combined_result(
        "IAM permissions",
        failures=_describe_problem(
            prefix=f"missing on '{project}'", items=missing, hint=ask_admin
        ),
        warnings=[
            *_describe_problem(
                prefix=f"missing on '{project}'",
                items=[p for p in permissions.maybe_required if p not in granted],
                hint="the deploy may fail if it needs these permissions",
            ),
            *_describe_problem(
                prefix=f"missing on '{project}'",
                items=unverified,
                hint=unverified_hint,
            ),
        ],
        passed=f"{', '.join(requested)} granted",
    )


def _resource_grants(
    *, resource: _IamResource, token: str
) -> tuple[set[str], str | None]:
    """Return the permission if granted on ``resource``, and why it gave no answer."""
    try:
        resp = _call_api(
            url=resource.url, token=token, json={"permissions": [resource.permission]}
        )
    except Exception as exc:
        logging.debug("%s permission check could not run", resource.kind, exc_info=exc)
        return set(), type(exc).__name__
    if not resp.ok:
        return set(), f"HTTP {resp.status_code}"
    return set(resp.json().get("permissions", [])), None


def _check_service_account(
    *,
    service_account: str,
    token: str,
    not_found_hint: str = "check the email",
) -> CheckResult:
    """Check that the account exists and that you may deploy with it.

    ``projects/-`` lets IAM find the account's project from the email, so an
    account from another project is found too.
    """
    resp = _call_api(
        url=f"https://iam.googleapis.com/v1/projects/-/serviceAccounts/{service_account}:testIamPermissions",
        token=token,
        json={"permissions": [_ACT_AS_PERMISSION]},
    )
    if resp.status_code == 404:
        return _fail_result(
            "Service account", f"{service_account} does not exist — {not_found_hint}"
        )
    if not resp.ok:
        return _warn_result(
            "Service account",
            f"could not verify {service_account} — HTTP {resp.status_code}",
        )
    if _ACT_AS_PERMISSION not in resp.json().get("permissions", []):
        return _fail_result(
            "Service account",
            f"missing {_ACT_AS_PERMISSION} on {service_account} — ask for "
            "roles/iam.serviceAccountUser on that account",
        )
    return _pass_result(
        "Service account", f"{service_account} exists and you can act as it"
    )


def _cloud_run_service_account_checks(
    *,
    service_account: str | None,
    current_account: str | None,
    service_exists: bool | None,
    project: str,
    token: str,
) -> list[CheckResult]:
    """Check the account the service will run as, when it is known."""
    if service_account:
        return [
            _run_check(
                "Service account",
                lambda: _check_cloud_run_service_account(
                    account=service_account, project=project, token=token
                ),
            )
        ]
    # Cloud Run checks actAs on the runtime account for every new revision, even
    # when the deploy keeps the service's current account.
    if current_account:
        return [
            _run_check(
                "Service account",
                lambda: _check_service_account(
                    service_account=current_account,
                    token=token,
                    not_found_hint="the service runs as this account; pass "
                    "--service-account to replace it",
                ),
            )
        ]
    if service_exists is not False:
        return []
    return [
        _run_check(
            "Service account",
            lambda: _check_default_service_account(project=project, token=token),
        )
    ]


def _check_cloud_run_service_account(
    *, account: str, project: str, token: str
) -> CheckResult:
    """Check the account the service runs as. An ID without ``@`` is an account
    in the deploy project.

    The email holds the project ID, so it cannot be built from a project number
    or a domain-scoped project ID.
    """
    if "@" in account:
        return _check_service_account(service_account=account, token=token)
    if project.isdigit() or ":" in project:
        return _warn_result(
            "Service account",
            f"'{account}' was not checked, because its email cannot be built from "
            f"project '{project}' — pass the full email to check it",
        )
    return _check_service_account(
        service_account=f"{account}@{project}.iam.gserviceaccount.com",
        token=token,
        not_found_hint="check the account ID",
    )


def _check_default_service_account(*, project: str, token: str) -> CheckResult:
    """Check the default compute account, which a new service runs as when no
    ``--service-account`` is set."""
    project_number = get_gcp_project_number(project)
    if not project_number:
        return _warn_result(
            "Service account",
            "could not read the project number, so the default compute service "
            "account was not checked",
        )
    return _check_service_account(
        service_account=f"{project_number}-compute@developer.gserviceaccount.com",
        token=token,
        not_found_hint="a new service runs as this default account; pass "
        "--service-account",
    )


def _has_secret_in_deploy_project(secrets: Mapping[str, str]) -> bool:
    """True when any secret is a bare ID, which Cloud Run reads from the deploy
    project.

    A full ``projects/<p>/secrets/<s>`` path points at its own project, so it
    does not need Secret Manager enabled in the deploy project.
    """
    return any(
        not spec["secret"].startswith("projects/")
        for spec in parse_secret_refs(secrets).values()
    )


def _check_agent_runtime_secret_ids(
    *, project: str, secrets: Mapping[str, str]
) -> CheckResult | None:
    """Fail on a ``--secrets`` value the deploy would reject, or return None.

    It needs no network call, so it runs even without credentials. Agent Runtime
    accepts only the bare ID of a secret in the deploy project, and its API
    rejects anything else, a path such as ``projects/p/secrets/s`` included.
    """
    invalid = [
        spec["secret"]
        for spec in parse_secret_refs(secrets).values()
        if not _SECRET_ID_RE.fullmatch(spec["secret"])
    ]
    if not invalid:
        return None
    return _fail_result(
        "Secrets",
        _describe_problem(
            prefix="not a bare secret ID",
            items=invalid,
            hint="Agent Runtime only reads secrets from the deploy project; pass the "
            f"ID of a secret in '{project}' (letters, digits, - and _)",
        )[0],
    )


def _secret_version_checks(
    *,
    project: str,
    secrets: Mapping[str, str],
    token: str,
    disabled_apis: list[str],
    runtime_identity: str,
) -> list[CheckResult]:
    """Look up the secret versions, unless the Secret Manager API is disabled.

    Secret Manager answers 403 while its API is disabled, and the APIs check
    already fails on that.
    """
    if _SECRET_MANAGER_API in disabled_apis:
        logging.warning(
            "Could not verify the secrets: the Secret Manager API is disabled in "
            "'%s'. Enable it, then run the dry run again.",
            project,
        )
        return []
    return [
        _run_check(
            "Secrets",
            lambda: _check_secret_versions(
                project=project,
                secrets=secrets,
                token=token,
                runtime_identity=runtime_identity,
            ),
        )
    ]


def _check_secret_versions(
    *, project: str, secrets: Mapping[str, str], token: str, runtime_identity: str
) -> CheckResult:
    """Look up the exact secret *version* the deploy will mount.

    Reading the version catches a secret or version that does not exist, and a
    version that is disabled or destroyed. The deploy would only hit these at
    runtime. A 403 is not a failure: ``roles/secretmanager.secretAccessor`` lets
    ``runtime_identity`` use the secret but not read its metadata, so a GET can
    be denied for a secret that works fine.
    """
    states = _read_states(
        urls={
            f"{spec['secret']}:{spec['version']}": "https://secretmanager.googleapis.com"
            f"/v1/{_secret_path(project=project, secret=spec['secret'])}/versions/{spec['version']}"
            for spec in parse_secret_refs(secrets).values()
        },
        token=token,
    )
    return _combined_result(
        "Secrets",
        failures=[
            *_describe_problem(
                prefix=f"not found in '{project}'",
                items=[s for s in states.not_found if not s.startswith("projects/")],
                hint="check the secret ID and version, or create them",
            ),
            *_describe_problem(
                prefix="not found",
                items=[s for s in states.not_found if s.startswith("projects/")],
                hint="check the secret path and version, or create them",
            ),
            *_describe_problem(
                prefix="not enabled",
                items=[
                    f"{label} ({state or 'no state'})"
                    for label, state in states.not_enabled.items()
                ],
                hint="enable the version, or point --secrets at an enabled one",
            ),
        ],
        warnings=_describe_problem(
            prefix="could not verify",
            items=[
                *(f"{label} (HTTP 403)" for label in states.denied),
                *states.unreadable,
            ],
            hint=f"the deploy still works if {runtime_identity} holds "
            "roles/secretmanager.secretAccessor",
        ),
        passed="all referenced secret versions exist and are enabled",
    )


def _secret_path(*, project: str, secret: str) -> str:
    return (
        secret
        if secret.startswith("projects/")
        else f"projects/{project}/secrets/{secret}"
    )


def _report_results(results: list[CheckResult]) -> ValidationReport:
    """Print the check results."""
    for result in results:
        click.echo(f"  {_STATUS_ICONS[result.status]} {result.name}: {result.detail}")
    return ValidationReport(tuple(results))
