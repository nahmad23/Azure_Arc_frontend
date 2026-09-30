# Azure Arc Frontend – HA nginx on two Linux nodes

Ansible automation that deploys the frontend on **nginx** across **two Linux
servers** (Azure Arc-enabled) and makes it highly available with a
**keepalived / VRRP floating virtual IP (VIP)**.

```
                    clients / DNS (app.example.com -> VIP)
                                   |
                        VIP 10.0.1.100 (floating)
                     ______________|______________
                    |                             |
          fe-node1 (MASTER, prio 150)   fe-node2 (BACKUP, prio 100)
          nginx + keepalived            nginx + keepalived
                    |<------ VRRP (unicast) ----->|
                    |                             |
              (optional) /api/  --->  backend servers
```

* Both nodes run nginx with the same content and config (active/passive on the VIP).
* keepalived checks nginx every 2 s (`/usr/local/bin/check_nginx.sh` → `http://127.0.0.1/healthz`).
  If nginx dies or stops answering on the MASTER, its priority drops by 60 (150 → 90 < 100),
  and the BACKUP takes the VIP within ~3–4 s and sends gratuitous ARP.
* The playbook runs with `serial: 1`, so upgrades roll one node at a time and the VIP
  always has a healthy owner. The nginx config is checked (`nginx -t`) before every reload.
* Every response carries an `X-Served-By: <node>` header, so you can see which node answered.

## Layout

```
ansible/
  ansible.cfg
  site.yml                    # main playbook
  requirements.yml            # ansible.posix, community.general
  inventory/hosts.ini         # the two nodes
  group_vars/frontend.yml     # VIP, nginx, TLS, backend, Azure Arc settings
  host_vars/fe-node{1,2}.yml  # MASTER/BACKUP state and priority
  roles/
    azure_arc/   # optional: install azcmagent and connect nodes to Azure Arc
    common/      # base packages, sysctl, firewall (firewalld / ufw) incl. VRRP
    nginx/       # install nginx, deploy frontend build, site config, TLS, /healthz
    keepalived/  # VRRP VIP + nginx health check
frontend/                     # frontend build output (placeholder index.html)
scripts/check-ha.sh           # check the VIP and each node
```

## Prerequisites

* 2 Linux servers (RHEL/Rocky/Alma 8+/9 or Ubuntu 20.04+/Debian 11+) **in the same L2 subnet**.
* A free IP in that subnet for the VIP.
* SSH access with sudo from the Ansible control node.
* Control node: `pip install ansible` (or `ansible-core` + `ansible-galaxy collection install -r ansible/requirements.yml`).
* RHEL-family: keepalived and nginx come from AppStream; no EPEL needed on RHEL 8/9.

> **Where the nodes run matters.** A keepalived VIP works on on-prem, VMware,
> Hyper-V, or bare-metal networks, which is the usual case for Azure Arc-enabled
> servers. It does **not** work on public-cloud VMs (Azure, AWS, GCP) because
> the cloud network ignores gratuitous ARP. There, put an Azure Load Balancer or
> Application Gateway (or the cloud's own LB) in front of the two nginx nodes
> and point its health probe at `/healthz`. You still use the `common` and
> `nginx` roles; skip the `keepalived` role.

## Configure

1. **Inventory** – `ansible/inventory/hosts.ini`: set `ansible_host` for `fe-node1` / `fe-node2`
   and `ansible_user`.
2. **VIP and HA** – `ansible/group_vars/frontend.yml`:
   * `frontend_vip`, `frontend_vip_cidr`
   * `keepalived_interface` (empty = the default-route NIC)
   * `keepalived_router_id` (unique per L2 segment), `keepalived_auth_pass` (max 8 characters)
3. **Frontend content** – copy your build output (`dist/`, `build/`) into `frontend/`
   or set `frontend_src_dir`. `nginx_spa_fallback: true` serves `index.html` for client-side routes.
4. **Optional**
   * API reverse proxy: `backend_servers: ["10.0.2.21:8080", "10.0.2.22:8080"]` → proxied at `/api/`.
   * TLS: `nginx_tls_enabled: true`, `nginx_tls_cert_src`, `nginx_tls_key_src` (the same cert on
     both nodes, issued for the DNS name that points at the VIP). HTTP redirects to HTTPS;
     `/healthz` stays on HTTP for the health checks.
   * Azure Arc onboarding: `azure_arc_onboard: true` plus subscription, tenant, resource group,
     location, and a service principal with the *Azure Connected Machine Onboarding* role.
     Keep the secret in Ansible Vault:
     `ansible-vault encrypt_string 'secret' --name azure_arc_sp_secret`.
     Nodes that are already connected are skipped.

## Deploy

```bash
cd ansible
ansible-galaxy collection install -r requirements.yml
ansible -m ping frontend                      # connectivity
ansible-playbook site.yml --check --diff      # dry run
ansible-playbook site.yml                     # deploy (add --ask-vault-pass if using vault)
```

To release a new frontend build, update `frontend/` and re-run `ansible-playbook site.yml`.
The run is idempotent and only changes what differs.

## Verify and test failover

```bash
scripts/check-ha.sh 10.0.1.100 10.0.1.11 10.0.1.12
# VIP    10.0.1.100      UP   served-by=fe-node1
# node   10.0.1.11       UP   served-by=fe-node1
# node   10.0.1.12       UP   served-by=fe-node2

# On fe-node1: see who holds the VIP
ip -4 addr show | grep 10.0.1.100

# Simulate a failure on fe-node1
sudo systemctl stop nginx
scripts/check-ha.sh 10.0.1.100 10.0.1.11 10.0.1.12    # VIP now served-by=fe-node2
sudo journalctl -t keepalived-notify -n 5              # state-change log on each node

# Recover (the VIP moves back to fe-node1, the higher priority)
sudo systemctl start nginx
```

## Monitoring with Azure Arc

Once the nodes are Arc-enabled you can also:
* install the Azure Monitor Agent extension and collect `/var/log/nginx/*.log` and the
  `keepalived-notify` syslog entries (they record VIP state changes);
* add an Azure Monitor availability test or alert against `http://<VIP>/healthz`;
* use Arc Run Command or Update Manager to patch the nodes one at a time.
