# Server Provisioning Portal

A self-service web form for requesting Linux and Windows VMs. Submitting the
form queues an **Azure DevOps pipeline** that runs your existing **Terraform**
code against **VMware vSphere** or **SCVMM / Hyper-V**.

Stack: **FastAPI** (Python) + **HTMX** + Jinja2 templates, served by
**uvicorn** behind the existing **nginx + keepalived** HA pair
(`uxus1sitaarc01` / `uxus1sitaarc02`, VIP `10.192.2.153`).

## Architecture

```
 Browser ──HTTPS──> VIP 10.192.2.153 (keepalived)
                        │
            ┌───────────┴───────────┐
     uxus1sitaarc01           uxus1sitaarc02        (active / standby)
     nginx :80/:443           nginx :80/:443
       └─> portal :8000         └─> portal :8000    FastAPI + HTMX, systemd service
                        │
          ① sign-in     │  Microsoft Entra ID (OIDC)
          ② queue run   │  Azure DevOps REST: POST _apis/pipelines/{id}/runs
          ③ poll status │  Azure DevOps REST: GET  _apis/pipelines/{id}/runs/{runId}
                        ▼
              Azure DevOps pipeline (pipelines/provision-vm.yml)
                Plan stage  ─> terraform plan   (request.auto.tfvars.json)
                Apply stage ─> approval for prod ─> terraform apply
                        │  self-hosted agents
              ┌─────────┴──────────┐
          vCenter (VMware)     SCVMM / Hyper-V
```

### Request flow

1. The user signs in with Entra ID. Access can be limited to Entra groups or app roles.
2. The form adapts as the user fills it in (HTMX). OS images, clusters and networks follow
   the chosen platform and OS. The hostname is checked live (format, Windows 15-character
   limit, and whether it already resolves in DNS). Static IP fields appear only when needed.
3. On submit, the server validates everything against `catalog.yaml`. For example, an image
   must belong to the selected platform, the gateway must be inside the subnet, and
   Production needs a change ticket.
4. The portal queues the pipeline with these parameters: `platform`, `osFamily`, `hostname`,
   `environment`, `requestedBy`, plus `requestJson` (the full validated request).
5. The result page polls the run status every 10 seconds until it succeeds or fails, and
   links to the run in Azure DevOps.
6. The pipeline writes `requestJson` to `request.auto.tfvars.json`, runs `terraform plan`,
   waits for approval when the target is Production (Azure DevOps Environment checks),
   then runs `terraform apply`.

### Design choices

* **Stateless app, Azure DevOps as the system of record.** Every request is a pipeline run
  with its parameters, logs and approvals. There is no database to keep in sync between
  the two nodes. The signed session cookie uses the same key on both nodes, so a VIP
  failover does not log users out.
* **Approvals live in Azure DevOps.** Add "Approvals and checks" to the
  `vm-provisioning-prod` environment, with no code in the portal.
* **Validation on both sides.** The portal validates user input, and the pipeline re-checks
  the hostname. Parameters reach scripts only through environment variables, so free text
  such as notes cannot inject shell commands.
* **The catalog is data, not code.** Platforms, images, clusters, networks, sizes and
  domains come from `catalog.yaml`. Adding a platform such as AWS or OCI later means
  adding a catalog entry and a Terraform directory, plus a branch in the pipeline.

## Project layout

```
portal/
  app/
    main.py        routes: form, HTMX fragments, submit, run status, run list, /healthz
    models.py      request validation against the catalog
    ado.py         Azure DevOps client (PAT or service principal) + dry-run client
    auth.py        Entra ID sign-in (MSAL, authorization code flow)
    config.py      settings from PORTAL_* environment variables
    catalog.py     loads catalog.yaml
    templates/     Jinja2 + HTMX templates
    static/        app.css, htmx.min.js (2.0.4, served locally, no CDN)
  catalog.yaml     choices offered by the form, edit this
  requirements.txt
  tests/           pytest suite (Azure DevOps API mocked)
pipelines/provision-vm.yml   example pipeline the portal queues
ansible/roles/portal/        deploys the app on both nodes
```

## Rollout plan

| Phase | What | Settings |
|---|---|---|
| 1. Deploy in dry-run | Portal live on the VIP. Requests are validated and shown as succeeded, but nothing is queued. Use this to review the form with users. | defaults (`portal_dry_run: true`, `portal_auth_mode: none`) |
| 2. Connect Azure DevOps | Import the pipeline, test it with a dev VM | `portal_dry_run: false` + `portal_ado_*` (requires phase 3) |
| 3. Sign-in and HTTPS | Entra ID app registration, TLS certificate, DNS name for the VIP | `nginx_tls_enabled: true`, `portal_auth_mode: entra`, `portal_entra_*` |
| 4. Hardening | Prod approvals in Azure DevOps, restrict to an Entra group, monitoring | `portal_allowed_groups` |
| 5. Later | Per-user history and request database, IPAM lookup for free IPs, decommission requests, AWS/OCI | — |

The portal refuses to start with `auth_mode: none` and `dry_run: false`, so it can't
provision VMs anonymously. Phases 2 and 3 therefore go live together.

## Setup

### 1. Edit the catalog

Edit `portal/catalog.yaml`. It holds every dropdown and checkbox list on the form:
departments, environments, request types, operating systems, platforms (with the template
for each OS), locations and their datacenters, server roles, CPU/memory/disk sizes, networks
and their VLANs (with subnet and gateway, used to check static IPs), domains, AD groups, the
security and automation checkboxes, and approvers. Each `id` is passed to Terraform, so use
the values your Terraform code expects, such as the vSphere template name.

### 2. Azure DevOps pipeline

1. Copy `pipelines/provision-vm.yml` into the repository that holds your Terraform code.
   Adjust `tfDir`, the agent pools, the variable group, the backend `key`, and the secrets
   mapped into `TF_VAR_*`.
2. In Azure DevOps: **Pipelines → New pipeline → Existing YAML file**, choose that file,
   and save it without running.
3. Note the pipeline ID, the `definitionId=` number in the pipeline URL.
4. Create the Environments `vm-provisioning-dev`, `-test`, `-uat` and `-prod`, and add an
   **Approval** check on `-prod`.
5. Run the pipeline once by hand with a dev VM, so you can **Permit** its access to the
   variable group, environments and agent pools. Otherwise runs from the portal will wait
   for that approval.
6. Give the portal permission to queue runs, using one of:
   * **PAT**: a personal access token for a service account, scope **Build: Read & execute**.
   * **Service principal**: add an Entra app as a user in the Azure DevOps organization with
     the Basic access level. Grant it **Queue builds** and **View builds** on the pipeline.
     Set `portal_ado_auth: service_principal`.

`requestJson` contains these keys. Map them to your Terraform variable names in the
"write tfvars" step if they differ:

```
1 request:    requester_name, requester_email, department, environment, request_type, source_server
2 server:     hostname, platform, os, os_family, os_template, location, datacenter, server_role,
              application, business_owner, technical_owner
3 compute:    cpu, memory_gb, os_disk_gb, additional_disk_gb, disk_type, network, vlan,
              ip_assignment, ip_address, prefix_length, gateway
4 security:   domain_join, domain, ad_groups[], security{monitoring, backup, vulnerability_scan,
              ansible_hardening, edr_av}
5 automation: automation{terraform_provisioning, ansible_hardening, domain_join, zabbix_agent,
              monitoring_config, backup_config, security_baseline, patch_config}
6 approval:   business_justification, change_ticket, approver, planned_date, required_by_date,
              comments, requested_by
```

The pipeline also gets `requestType` (new / rebuild / clone) as its own parameter, so it can
choose a different flow for rebuilds and clones.

### 3. Entra ID sign-in

1. **Entra ID → App registrations → New registration**, single tenant. Add a **Web**
   redirect URI `https://<portal-dns-name>/auth/callback`. Entra ID requires HTTPS here,
   so turn on `nginx_tls_enabled` with a certificate for that DNS name, and point the
   DNS name at `10.192.2.153`.
2. **Certificates & secrets → New client secret.**
3. To restrict access, either create an **App role** such as `Provisioning.Requester` and
   assign users or groups to it (recommended, because it avoids the 200-group token limit),
   or add a **groups claim** under Token configuration. Put the role names or group object
   IDs in `portal_allowed_groups`.

### 4. Settings (`ansible/group_vars/frontend.yml`)

```yaml
portal_dry_run: false
portal_ado_org_url: https://dev.azure.com/<org>
portal_ado_project: <project>
portal_ado_pipeline_id: "<id>"
portal_ado_pat: !vault |        # ansible-vault encrypt_string '<pat>' --name portal_ado_pat
  ...
portal_auth_mode: entra
portal_entra_tenant_id: <tenant-guid>
portal_entra_client_id: <app-client-id>
portal_entra_client_secret: !vault |
  ...
portal_entra_redirect_uri: https://<portal-dns-name>/auth/callback
portal_allowed_groups: [Provisioning.Requester]
nginx_tls_enabled: true
nginx_tls_cert_src: files/portal.crt
nginx_tls_key_src: files/portal.key
# If the nodes reach the internet through a proxy:
# portal_proxy_env: {HTTPS_PROXY: "http://proxy:8080", NO_PROXY: "localhost,127.0.0.1"}
```

The nodes need outbound HTTPS to `dev.azure.com` and `login.microsoftonline.com`, or to
your Azure DevOps Server. During deployment they also need PyPI, or an internal mirror
set via `portal_pip_extra_args`.

## Deploy

```bash
cd ansible
ansible-playbook site.yml --ask-vault-pass      # omit --ask-vault-pass while nothing is vaulted
```

The playbook rolls one node at a time. On each node it installs the app into
`/opt/provisioning-portal`, writes `/etc/provisioning-portal/portal.env` (mode 0640) and
`catalog.yaml`, starts the `provisioning-portal` systemd service, and points nginx at it.
The portal session key is generated once on the controller, in
`ansible/credentials/portal_secret_key` (git-ignored), and the same key goes to both nodes.

## Validate

```bash
cd ansible
# app running on both nodes
ansible frontend -b -m shell -a "systemctl is-active provisioning-portal nginx keepalived"
ansible frontend -m uri -a "url=http://127.0.0.1:8000/healthz return_content=true"

# through the VIP
curl -s  http://10.192.2.153/healthz          # nginx on the active node
curl -sI http://10.192.2.153/                  # 200 (or 307 to /login with Entra sign-in)
../scripts/check-ha.sh 10.192.2.153 10.192.2.150 10.192.2.149
```

Then open `http://10.192.2.153/` in a browser, or `https://<portal-dns-name>/` once TLS
is on, and submit a request:

* **In dry-run:** the result page shows *Running*, then *Succeeded*, after about 20 seconds.
  Nothing is provisioned.
* **Live:** the result page links to the run in Azure DevOps, so check the parameters and
  the `terraform plan` output there.

**Failover with the portal:** if the portal service stops on the active node, the VIP moves
just as it does when nginx fails, because the keepalived health check covers both:

```bash
ansible uxus1sitaarc01 -b -m service -a "name=provisioning-portal state=stopped"   # VIP -> uxus1sitaarc02
ansible uxus1sitaarc01 -b -m service -a "name=provisioning-portal state=started"   # VIP -> back
```

**Logs:** `journalctl -u provisioning-portal -f` on a node shows sign-ins, queued runs
(with requester and hostname), and Azure DevOps errors.

## Local development

```bash
cd portal
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
PORTAL_AUTH_MODE=none PORTAL_DRY_RUN=true uvicorn app.main:create_app --factory --reload
# open http://127.0.0.1:8000
```
