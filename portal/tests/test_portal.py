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
    "requester_name": "Ahmad",
    "requester_email": "ahmad@company.com",
    "department": "infrastructure",
    "environment": "dev",
    "request_type": "new",
    "hostname": "UXUS1ARCWRK01",
    "os": "rhel9",
    "platform": "vmware",
    "location": "us",
    "datacenter": "us1",
    "server_role": "application",
    "application": "ARC",
    "business_owner": "Application Team",
    "technical_owner": "Unix Team",
    "cpu": "4",
    "memory_gb": "16",
    "os_disk_gb": "100",
    "additional_disk_gb": "200",
    "disk_type": "ssd",
    "network": "production",
    "vlan": "vlan-110",
    "ip_assignment": "dhcp",
    "domain_join": "yes",
    "domain": "unitedlex.global",
    "ad_groups": ["GRP_ServerAdmin_Access"],
    "security": ["monitoring", "backup"],
    "automation": ["terraform_provisioning", "domain_join", "zabbix_agent"],
    "business_justification": "New ARC worker node.",
    "approver": "unix-lead",
    "planned_date": "2099-01-10",
    "required_by_date": "2099-01-20",
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


def post(client, **changes):
    data = dict(VALID)
    for k, v in changes.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    return client.post("/requests", data=data, headers=HX)


def test_form_renders_all_sections(client):
    r = client.get("/")
    assert r.status_code == 200
    for title in ("Request Information", "Server Details", "Compute Resources",
                  "Access &amp; Security", "Automation Options", "Approval / Change Information"):
        assert title in r.text
    for label in ("Requester Name", "Department", "Request Type", "Server Hostname", "Operating System",
                  "Server Location", "Server Role", "Business Owner", "Technical Owner", "Disk Type",
                  "IP Assignment", "AD Groups", "Business Justification", "Approver",
                  "Planned Provisioning Date", "Required By Date", "Additional Comments"):
        assert label in r.text, label
    assert "VMware vSphere" in r.text and "Microsoft Hyper-V (SCVMM)" in r.text
    assert "Zabbix Agent Installation" in r.text and "GRP_ServerAdmin_Access" in r.text
    # automation steps default to checked
    assert 'name="automation" value="patch_config" checked' in r.text
    assert "Dry-run mode" in r.text


def test_requester_fields_are_editable_when_sign_in_is_off(client):
    r = client.get("/")
    assert 'id="requester_name" name="requester_name" type="text" value=""' in r.text
    assert 'readonly class="readonly"' not in r.text.split('id="requester_email"')[1].split(">")[0]


def test_dependent_fields(client):
    assert ">UK1<" in client.get("/form/datacenters", params={"location": "uk"}).text
    assert "US1" not in client.get("/form/datacenters", params={"location": "uk"}).text
    vlans = client.get("/form/vlans", params={"network": "nonproduction"}).text
    assert "VLAN-210" in vlans and "VLAN-110" not in vlans
    assert 'name="cpu_custom"' in client.get("/form/cpu-custom", params={"cpu": "custom"}).text
    assert 'name="cpu_custom"' not in client.get("/form/cpu-custom", params={"cpu": "4"}).text
    static = client.get("/form/ip-fields", params={"ip_assignment": "static", "network": "production", "vlan": "vlan-110"}).text
    assert 'name="ip_address"' in static and "10.192.10.0/24" in static
    assert "Auto-generated" in client.get("/form/ip-fields", params={"ip_assignment": "dhcp"}).text
    assert 'name="domain"' in client.get("/form/domain-fields", params={"domain_join": "yes"}).text
    assert 'name="domain"' not in client.get("/form/domain-fields", params={"domain_join": "no"}).text
    assert 'name="source_server"' in client.get("/form/source-server", params={"request_type": "clone"}).text
    assert 'name="source_server"' not in client.get("/form/source-server", params={"request_type": "new"}).text


def test_platform_table_follows_os_and_search(client):
    r = client.get("/form/platforms", params={"os": "ubuntu2404"}).text
    assert "tpl-ubuntu2404" in r
    assert "No Ubuntu 24.04 template" in r  # SCVMM has no Ubuntu image -> row disabled
    r = client.get("/form/platforms", params={"platform_q": "hyper"}).text
    assert "Microsoft Hyper-V" in r and "VMware" not in r and "Found: 1 row" in r


def test_hostname_check(client):
    r = client.get("/form/check-hostname", params={"hostname": "AVERYLONGWINDOWSNAME", "os": "win2022"})
    assert "at most 15" in r.text
    r = client.get("/form/check-hostname", params={"hostname": "Bad_Name", "os": "rhel9"})
    assert "letters, digits and hyphens" in r.text


def test_submit_requires_htmx_header(client):
    assert client.post("/requests", data=VALID).status_code == 400


def test_validation_errors_are_shown_and_values_kept(client):
    r = post(client, os="win2022", hostname="UXUS1ARCWRKLONG01", environment="prod",
             datacenter="uk1", required_by_date="2099-01-01", automation=["domain_join"], domain_join="no")
    assert "at most 15 characters" in r.text
    assert "change ticket is required for Production" in r.text
    assert "Choose a datacenter" in r.text  # uk1 is not in the US
    assert "on or after the planned" in r.text
    assert "Domain Join step is selected" in r.text
    assert 'value="UXUS1ARCWRKLONG01"' in r.text
    assert 'name="ad_groups" value="GRP_ServerAdmin_Access" checked' in r.text
    assert 'name="security" value="edr_av" checked' not in r.text  # unticked box stays unticked


def test_os_must_exist_on_platform(client):
    assert "Ubuntu 24.04 is not available on Microsoft Hyper-V" in post(client, os="ubuntu2404", platform="scvmm").text


def test_static_ip_must_be_in_vlan(client):
    assert "inside 10.192.10.0/24" in post(client, ip_assignment="static", ip_address="10.50.0.5").text
    assert "VLAN gateway" in post(client, ip_assignment="static", ip_address="10.192.10.1").text


def test_custom_cpu_and_dates(client):
    assert "whole number from 1 to 64" in post(client, cpu="custom", cpu_custom="128").text
    assert "cannot be in the past" in post(client, planned_date="2001-01-01").text


def test_rebuild_needs_source_server(client):
    assert "existing server to rebuild" in post(client, request_type="rebuild").text


def test_dry_run_submit_and_status(client):
    r = post(client, cpu="custom", cpu_custom="6", ip_assignment="static", ip_address="10.192.10.25")
    assert r.status_code == 200
    assert "Request submitted" in r.text
    assert "UXUS1ARCWRK01" in r.text
    assert "6 vCPU" in r.text and "200 GB additional" in r.text
    assert "10.192.10.25/24 via 10.192.10.1" in r.text
    statuses = [client.get("/requests/1/status").text for _ in range(3)]
    assert "running" in statuses[0]
    assert "succeeded" in statuses[-1]
    assert "hx-trigger" not in statuses[-1]  # polling stops once finished
    assert "UXUS1ARCWRK01" in client.get("/requests").text


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
    "id": 101, "name": "20261002.1_UXUS1ARCWRK01", "state": "inProgress", "result": None,
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
    assert params["platform"] == "vmware" and params["hostname"] == "UXUS1ARCWRK01"
    assert params["osFamily"] == "linux" and params["requestType"] == "new"
    assert params["requestedBy"] == "ahmad@company.com"
    assert all(isinstance(v, str) for v in params.values())
    req = json.loads(params["requestJson"])
    assert req["cpu"] == 4 and req["additional_disk_gb"] == 200 and req["os_template"] == "tpl-rhel9"
    assert req["automation"]["zabbix_agent"] is True and req["automation"]["patch_config"] is False
    assert req["ad_groups"] == ["GRP_ServerAdmin_Access"] and req["datacenter"] == "us1"


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
    assert 'value="UXUS1ARCWRK01"' in r.text


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


def test_signed_in_identity_overrides_form():
    from datetime import date

    from starlette.datastructures import FormData

    from app.catalog import load_catalog
    from app.models import parse_request

    items = [(k, x) for k, v in VALID.items() for x in (v if isinstance(v, list) else [v])]
    items += [("requester_name", "Mallory"), ("requester_email", "mallory@evil.example")]
    user = {"oid": "123", "name": "Ahmad", "email": "ahmad@company.com"}
    req, errors = parse_request(FormData(items), load_catalog(CATALOG), user, today=date(2026, 10, 5))
    assert errors == {}
    assert (req.requester_name, req.requester_email, req.requested_by) == ("Ahmad", "ahmad@company.com", "ahmad@company.com")
