"""Azure DevOps Pipelines client: queue a run and read run status.

Uses the Pipelines "Runs" REST API:
  POST {org}/{project}/_apis/pipelines/{id}/runs   queue a run with templateParameters
  GET  {org}/{project}/_apis/pipelines/{id}/runs   list runs
  GET  {org}/{project}/_apis/pipelines/{id}/runs/{runId}
"""
import base64
import itertools
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import anyio
import httpx

from .config import Settings
from .models import ProvisionRequest

log = logging.getLogger("portal.ado")

# Resource ID of Azure DevOps in Microsoft Entra ID (used for service principal tokens).
ADO_RESOURCE_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"


class AdoError(Exception):
    pass


@dataclass
class Run:
    id: int
    name: str
    state: str  # unknown | inProgress | canceling | completed
    result: str | None  # succeeded | failed | canceled | None while running
    created: datetime | None
    finished: datetime | None
    web_url: str

    @property
    def done(self) -> bool:
        return self.state == "completed"

    @property
    def status(self) -> str:
        """One word for display: queued, running, succeeded, failed, canceled."""
        if self.state == "completed":
            return self.result or "completed"
        if self.state == "canceling":
            return "canceling"
        if self.state == "inProgress":
            return "running"
        return "queued"

    @classmethod
    def from_api(cls, data: dict) -> "Run":
        def ts(value):
            # e.g. "2026-10-02T10:15:30.1234567Z" (UTC, up to 7 fractional digits)
            if not value:
                return None
            return datetime.fromisoformat(value.split(".")[0].rstrip("Z")).replace(tzinfo=timezone.utc)

        return cls(
            id=int(data["id"]),
            name=data.get("name", ""),
            state=data.get("state", "unknown"),
            result=data.get("result"),
            created=ts(data.get("createdDate")),
            finished=ts(data.get("finishedDate")),
            web_url=data.get("_links", {}).get("web", {}).get("href", ""),
        )


def template_parameters(req: ProvisionRequest) -> dict[str, str]:
    """Pipeline parameters. Keep in sync with pipelines/provision-vm.yml.

    The short fields drive the pipeline (run name, agent, approvals); the full
    request goes in requestJson, which the pipeline writes to a tfvars file.
    """
    return {
        "platform": req.platform,
        "osFamily": req.os_family,
        "hostname": req.hostname,
        "environment": req.environment,
        "requestedBy": req.requested_by,
        "requestJson": json.dumps(req.to_dict(), separators=(",", ":")),
    }


class AdoClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self._transport = transport
        self._msal_app = None

    @property
    def _base(self) -> str:
        return f"{self.s.ado_org_url.rstrip('/')}/{self.s.ado_project}/_apis/pipelines/{self.s.ado_pipeline_id}/runs"

    async def _auth_header(self) -> str:
        if self.s.ado_auth == "pat":
            token = base64.b64encode(f":{self.s.ado_pat}".encode()).decode()
            return f"Basic {token}"
        if self._msal_app is None:
            import msal

            self._msal_app = msal.ConfidentialClientApplication(
                self.s.ado_client_id,
                authority=f"https://login.microsoftonline.com/{self.s.ado_tenant_id}",
                client_credential=self.s.ado_client_secret,
            )
        # msal caches the token in memory and only calls Entra ID when it expires.
        result = await anyio.to_thread.run_sync(
            lambda: self._msal_app.acquire_token_for_client(scopes=[ADO_RESOURCE_SCOPE])
        )
        if "access_token" not in result:
            raise AdoError(f"Could not get an Azure DevOps token: {result.get('error_description', result)}")
        return f"Bearer {result['access_token']}"

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        verify = self.s.ado_ca_bundle or True
        async with httpx.AsyncClient(timeout=30, verify=verify, transport=self._transport) as client:
            try:
                resp = await client.request(
                    method,
                    url,
                    params={"api-version": self.s.ado_api_version},
                    headers={"Authorization": await self._auth_header(), "Accept": "application/json"},
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise AdoError(f"Could not reach Azure DevOps: {exc}") from exc
        # A bad or expired PAT gets a 203 sign-in page instead of a 401.
        if resp.status_code == 203 or "application/json" not in resp.headers.get("content-type", ""):
            raise AdoError("Azure DevOps rejected the credentials (check the PAT / service principal).")
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", resp.text)
            except ValueError:
                message = resp.text
            raise AdoError(f"Azure DevOps returned {resp.status_code}: {message}")
        return resp.json()

    async def queue_run(self, req: ProvisionRequest) -> Run:
        body = {
            "resources": {"repositories": {"self": {"refName": self.s.ado_branch}}},
            "templateParameters": template_parameters(req),
        }
        run = Run.from_api(await self._request("POST", self._base, json=body))
        log.info("queued run %s for %s requested by %s", run.id, req.hostname, req.requested_by)
        return run

    async def get_run(self, run_id: int) -> Run:
        return Run.from_api(await self._request("GET", f"{self._base}/{run_id}"))

    async def list_runs(self, top: int = 25) -> list[Run]:
        data = await self._request("GET", self._base)
        runs = [Run.from_api(r) for r in data.get("value", [])]
        return sorted(runs, key=lambda r: r.id, reverse=True)[:top]


class DryRunClient:
    """Stand-in used while PORTAL_DRY_RUN=true: records requests in memory and
    reports them as succeeded after a few status checks. Nothing is provisioned."""

    def __init__(self):
        self._ids = itertools.count(1)
        self._runs: dict[int, tuple[Run, int]] = {}

    async def queue_run(self, req: ProvisionRequest) -> Run:
        run_id = next(self._ids)
        now = datetime.now(timezone.utc)
        run = Run(run_id, f"{now:%Y%m%d}.{run_id}_{req.hostname}", "inProgress", None, now, None, "")
        self._runs[run_id] = (run, 0)
        log.info("DRY RUN: would queue pipeline with %s", template_parameters(req))
        return run

    async def get_run(self, run_id: int) -> Run:
        if run_id not in self._runs:
            raise AdoError(f"Run {run_id} not found.")
        run, polls = self._runs[run_id]
        if polls >= 2 and not run.done:
            run.state, run.result, run.finished = "completed", "succeeded", datetime.now(timezone.utc)
        self._runs[run_id] = (run, polls + 1)
        return run

    async def list_runs(self, top: int = 25) -> list[Run]:
        return sorted((r for r, _ in self._runs.values()), key=lambda r: r.id, reverse=True)[:top]
