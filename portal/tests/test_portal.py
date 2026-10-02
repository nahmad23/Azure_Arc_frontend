import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ado import AdoClient, AdoError
from app.config import Settings
from app.main import create_app

CATALOG = str(Path(__file__).resolve().parents[1] / "catalog.yaml")
HX = {"HX-Request": "true"}

VALID = {
    "platform": "vmware",
    "os_family": "linux",
    "os_image": "tpl-rhel9",
    "hostname": "app-web-01",
    "environment": "dev",
    "location": "dc1-cluster01",
    "network": "vlan110-app",
    "cpu": "2",
    "memory_gb": "8",
    "os_disk_gb": "80",
    "data_disk_gb": ["100", "200"],
    "ip_mode": "dhcp",
    "domain_join": "no",
    "application": "Payroll",
    "owner_email": "owner@example.com",
}


def settings(**kw) -> Settings:
    base = dict(catalog_path=CATALOG, auth_mode="none", dry_run=True)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def client():
    return TestClient(create_app(settings()))


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_form_renders_with_catalog_choices(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Request a new server" in r.text
    assert "VMware vSphere" in r.text and "SCVMM / Hyper-V" in r.text
    assert "RHEL 9" in r.text  # linux images for the default platform
    assert "Dry-run mode" in r.text


def test_platform_options_follow_platform_and_os(client):
    r = client.get("/form/platform-options", params={"platform": "scvmm", "os_family": "windows"})
    assert "vhdx-win2022" in r.text
    assert "tpl-win2022" not in r.text
    assert "hv-cloud-prod" in r.text


def test_static_ip_fields_toggle(client):
    assert 'name="gateway"' in client.get("/form/ip-fields", params={"ip_mode": "static"}).text
    assert 'name="gateway"' not in client.get("/form/ip-fields", params={"ip_mode": "dhcp"}).text


def test_hostname_check(client):
    r = client.get("/form/check-hostname", params={"hostname": "averyverylongwinname", "os_family": "windows"})
    assert "at most 15" in r.text
    r = client.get("/form/check-hostname", params={"hostname": "Bad_Name", "os_family": "linux"})
    assert "Use lowercase" in r.text


def test_submit_requires_htmx_header(client):
    assert client.post("/requests", data=VALID).status_code == 400


def test_submit_validation_errors_are_shown(client):
    data = dict(VALID, os_family="windows", os_image="tpl-win2022", hostname="this-name-is-too-long",
                environment="prod", change_ticket="")
    r = client.post("/requests", data=data, headers=HX)
    assert r.status_code == 200
    assert "at most 15 characters" in r.text
    assert "required for Production" in r.text
    # entered values are kept
    assert 'value="this-name-is-too-long"' in r.text
    assert 'value="100"' in r.text


def test_static_ip_gateway_must_be_in_subnet(client):
    data = dict(VALID, ip_mode="static", ip_address="10.1.2.10", prefix_length="24",
                gateway="10.9.9.1", dns_servers="10.1.0.10")
    r = client.post("/requests", data=data, headers=HX)
    assert "Gateway must be inside 10.1.2.0/24" in r.text


def test_image_must_match_platform(client):
    data = dict(VALID, os_image="vhdx-rhel9")  # SCVMM image on VMware
    assert "Choose an OS image" in client.post("/requests", data=data, headers=HX).text


def test_dry_run_submit_and_status(client):
    r = client.post("/requests", data=VALID, headers=HX)
    assert r.status_code == 200
    assert "Request submitted" in r.text
    assert "app-web-01" in r.text
    assert "100, 200 GB" in r.text
    statuses = [client.get("/requests/1/status").text for _ in range(3)]
    assert "running" in statuses[0]
    assert "succeeded" in statuses[-1]
    assert "hx-trigger" not in statuses[-1]  # polling stops once finished
    runs = client.get("/requests")
    assert "app-web-01" in runs.text


def test_anonymous_without_dry_run_is_refused():
    with pytest.raises(RuntimeError, match="lets anyone provision"):
        settings(dry_run=False, ado_org_url="https://dev.azure.com/o", ado_project="p",
                 ado_pipeline_id=7, ado_pat="x", secret_key="s" * 32).check()


def test_entra_mode_redirects_to_login():
    s = settings(auth_mode="entra", entra_tenant_id="t", entra_client_id="c",
                 entra_client_secret="x", entra_redirect_uri="https://portal/auth/callback")
    c = TestClient(create_app(s), follow_redirects=False)
    r = c.get("/")
    assert r.status_code == 307 and r.headers["location"] == "/login"
    r = c.get("/form/ip-fields", headers=HX)
    assert r.headers.get("HX-Redirect") == "/login"
    assert c.get("/healthz").status_code == 200  # health check stays open


# --- Azure DevOps client against a mocked API --------------------------------

def ado_settings(**kw) -> Settings:
    return settings(dry_run=False, allow_anonymous=True, secret_key="s" * 32,
                    ado_org_url="https://dev.azure.com/contoso/", ado_project="Infra",
                    ado_pipeline_id=42, ado_pat="secret-pat", **kw)


RUN_JSON = {
    "id": 101, "name": "20261002.1_app-web-01", "state": "inProgress", "result": None,
    "createdDate": "2026-10-02T10:15:30.1234567Z",
    "_links": {"web": {"href": "https://dev.azure.com/contoso/Infra/_build/results?buildId=101"}},
}


def test_ado_queue_run_request_shape():
    seen = {}

    def handler(request: httpx.Request):
        seen["method"], seen["url"] = request.method, str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=RUN_JSON)

    client = TestClient(create_app(ado_settings(),
                                   ado=AdoClient(ado_settings(), transport=httpx.MockTransport(handler))))
    r = client.post("/requests", data=VALID, headers=HX)
    assert "Pipeline run <strong>#101</strong>" in r.text
    assert "buildId=101" in r.text
    assert seen["method"] == "POST"
    assert seen["url"] == "https://dev.azure.com/contoso/Infra/_apis/pipelines/42/runs?api-version=7.1"
    assert seen["auth"] == "Basic " + base64.b64encode(b":secret-pat").decode()
    body = seen["body"]
    assert body["resources"]["repositories"]["self"]["refName"] == "refs/heads/main"
    params = body["templateParameters"]
    assert params["platform"] == "vmware" and params["hostname"] == "app-web-01"
    assert params["requestedBy"] == "anonymous"
    assert all(isinstance(v, str) for v in params.values())
    req = json.loads(params["requestJson"])
    assert req["data_disks_gb"] == [100, 200] and req["cpu"] == 2


def test_ado_bad_pat_and_errors():
    s = ado_settings()
    signin = AdoClient(s, transport=httpx.MockTransport(
        lambda r: httpx.Response(203, text="<html>sign in</html>", headers={"content-type": "text/html"})))
    with pytest.raises(AdoError, match="rejected the credentials"):
        asyncio.run(signin.get_run(1))
    missing = AdoClient(s, transport=httpx.MockTransport(
        lambda r: httpx.Response(404, json={"message": "Pipeline 42 not found"})))
    with pytest.raises(AdoError, match="404: Pipeline 42 not found"):
        asyncio.run(missing.get_run(1))


def test_ado_error_is_shown_on_form():
    def handler(request):
        return httpx.Response(400, json={"message": "Unexpected parameter 'osFamily'"})

    client = TestClient(create_app(ado_settings(),
                                   ado=AdoClient(ado_settings(), transport=httpx.MockTransport(handler))))
    r = client.post("/requests", data=VALID, headers=HX)
    assert "could not be submitted" in r.text and "Unexpected parameter" in r.text
    assert 'value="app-web-01"' in r.text


def test_ado_list_runs_sorted_and_parsed():
    older = dict(RUN_JSON, id=100, state="completed", result="failed", finishedDate="2026-10-02T11:00:00Z")
    client = AdoClient(ado_settings(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"count": 2, "value": [older, RUN_JSON]})))
    runs = asyncio.run(client.list_runs())
    assert [r.id for r in runs] == [101, 100]
    assert runs[0].status == "running" and runs[1].status == "failed"
    assert runs[0].created.year == 2026 and runs[1].finished.hour == 11


def test_head_requests_for_monitoring(client):
    assert client.head("/").status_code == 200
    assert client.head("/healthz").status_code == 200
