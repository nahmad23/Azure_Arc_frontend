"""Validation of a submitted provisioning request."""
import ipaddress
import re
from dataclasses import asdict, dataclass, field
from datetime import date

from .catalog import Catalog

HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Windows NetBIOS names are limited to 15 characters.
MAX_HOSTNAME = {"windows": 15, "linux": 63}
CUSTOM = "custom"


@dataclass
class ProvisionRequest:
    # 1. Request information
    requester_name: str
    requester_email: str
    department: str
    environment: str
    request_type: str
    source_server: str
    # 2. Server details
    hostname: str
    platform: str
    os: str
    os_family: str
    os_template: str
    location: str
    datacenter: str
    server_role: str
    application: str
    business_owner: str
    technical_owner: str
    # 3. Compute resources
    cpu: int
    memory_gb: int
    os_disk_gb: int
    additional_disk_gb: int | None
    disk_type: str
    network: str
    vlan: str
    ip_assignment: str
    ip_address: str
    prefix_length: int | None
    gateway: str
    # 4. Access & security
    domain_join: bool
    domain: str
    ad_groups: list[str]
    security: dict[str, bool]
    # 5. Automation options
    automation: dict[str, bool]
    # 6. Approval / change information
    business_justification: str
    change_ticket: str
    approver: str
    planned_date: str
    required_by_date: str
    comments: str
    requested_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def check_hostname(hostname: str, os_family: str) -> str | None:
    """Return an error message, or None when the hostname is acceptable."""
    if not hostname:
        return "Hostname is required."
    limit = MAX_HOSTNAME.get(os_family, 63)
    if len(hostname) > limit:
        return f"Must be at most {limit} characters for {os_family.title() or 'this OS'}."
    if not HOSTNAME_RE.match(hostname):
        return "Use letters, digits and hyphens only; must not start or end with a hyphen."
    if hostname.isdigit():
        return "Hostname cannot be all digits."
    return None


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_request(form, catalog: Catalog, user: dict, today: date | None = None
                  ) -> tuple[ProvisionRequest | None, dict[str, str]]:
    """Validate raw form data against the catalog.

    `form` is a Starlette FormData (supports .get and .getlist). `user` is the
    signed-in user; when sign-in is disabled the requester fields come from the form.
    Returns (request, {}) on success or (None, {field: message}) on failure.
    """
    today = today or date.today()
    errors: dict[str, str] = {}

    def get(key: str) -> str:
        return (form.get(key) or "").strip()

    def require(key: str, valid: set[str], message: str) -> str:
        value = get(key)
        if value not in valid:
            errors[key] = message
        return value

    def require_text(key: str, label: str, limit: int) -> str:
        value = get(key)
        if not value:
            errors[key] = f"{label} is required."
        elif len(value) > limit:
            errors[key] = f"At most {limit} characters."
        return value

    def optional_text(key: str, limit: int) -> str:
        value = get(key)
        if len(value) > limit:
            errors[key] = f"At most {limit} characters."
        return value

    # --- 1. Request information --------------------------------------------
    if user.get("oid"):  # signed in: never trust the form for identity
        requester_name, requester_email = user.get("name", ""), user.get("email", "")
    else:
        requester_name = require_text("requester_name", "Requester name", 100)
        requester_email = get("requester_email")
        if not EMAIL_RE.match(requester_email):
            errors["requester_email"] = "Enter a valid email address."
    department = require("department", catalog.ids(catalog.departments), "Choose a department.")
    environment = require("environment", catalog.ids(catalog.environments), "Choose an environment.")
    request_type = require("request_type", catalog.ids(catalog.request_types), "Choose a request type.")
    source_server = ""
    if request_type in ("rebuild", "clone"):
        source_server = get("source_server")
        if msg := check_hostname(source_server, "linux"):
            errors["source_server"] = (
                "Enter the existing server to " + request_type + "." if not source_server else msg
            )

    # --- 2. Server details -------------------------------------------------
    os_id = require("os", {o.id for o in catalog.operating_systems}, "Choose an operating system.")
    os_obj = catalog.os(os_id)
    os_family = os_obj.family if os_obj else ""
    hostname = get("hostname")
    if msg := check_hostname(hostname, os_family or "linux"):
        errors["hostname"] = msg

    platform = get("platform")
    os_template = ""
    if platform not in catalog.platforms:
        errors["platform"] = "Choose an infrastructure platform."
    elif os_obj:
        os_template = catalog.platforms[platform].images.get(os_id, "")
        if not os_template:
            errors["platform"] = f"{os_obj.label} is not available on {catalog.platforms[platform].label}."

    location = require("location", catalog.ids(catalog.locations), "Choose a server location.")
    datacenter = require("datacenter", catalog.ids(catalog.datacenters(location)), "Choose a datacenter.")
    server_role = require("server_role", catalog.ids(catalog.server_roles), "Choose a server role.")
    application = require_text("application", "Application name", 64)
    business_owner = require_text("business_owner", "Business owner", 100)
    technical_owner = require_text("technical_owner", "Technical owner", 100)

    # --- 3. Compute resources ----------------------------------------------
    cpu = 0
    cpu_choice = get("cpu")
    if cpu_choice == CUSTOM:
        try:
            cpu = int(get("cpu_custom"))
            if not catalog.custom_cpu_min <= cpu <= catalog.custom_cpu_max:
                raise ValueError
        except ValueError:
            errors["cpu_custom"] = f"Enter a whole number from {catalog.custom_cpu_min} to {catalog.custom_cpu_max}."
    elif cpu_choice.isdigit() and int(cpu_choice) in catalog.cpu:
        cpu = int(cpu_choice)
    else:
        errors["cpu"] = "Choose a CPU count."

    memory = get("memory_gb")
    if not (memory.isdigit() and int(memory) in catalog.memory_gb):
        errors["memory_gb"] = "Choose a memory size."
    os_disk = get("os_disk_gb")
    if not (os_disk.isdigit() and int(os_disk) in catalog.os_disk_gb):
        errors["os_disk_gb"] = "Choose an OS disk size."

    additional_disk: int | None = None
    if raw := get("additional_disk_gb"):
        if raw.isdigit() and catalog.additional_disk_min <= int(raw) <= catalog.additional_disk_max:
            additional_disk = int(raw)
        else:
            errors["additional_disk_gb"] = (
                f"Enter a size from {catalog.additional_disk_min} to {catalog.additional_disk_max} GB, or leave empty."
            )
    disk_type = require("disk_type", catalog.ids(catalog.disk_types), "Choose a disk type.")

    network = require("network", catalog.ids(catalog.networks), "Choose a network.")
    vlan_id = get("vlan")
    vlan = catalog.vlan(network, vlan_id)
    if not vlan:
        errors["vlan"] = "Choose a VLAN."

    ip_assignment = require("ip_assignment", {"dhcp", "static"}, "Choose DHCP or static.")
    ip_address = gateway = ""
    prefix: int | None = None
    if ip_assignment == "static":
        try:
            ip = ipaddress.IPv4Address(get("ip_address"))
            ip_address = str(ip)
            if vlan and vlan.network:
                if ip not in vlan.network or ip in (vlan.network.network_address, vlan.network.broadcast_address):
                    errors["ip_address"] = f"Must be a host address inside {vlan.network} ({vlan.label})."
                prefix, gateway = vlan.network.prefixlen, vlan.gateway
                if ip_address == gateway:
                    errors["ip_address"] = "This is the VLAN gateway address."
        except ValueError:
            errors["ip_address"] = "Enter a valid IPv4 address."

    # --- 4. Access & security ----------------------------------------------
    domain_join = get("domain_join") == "yes"
    domain = ""
    if domain_join:
        domain = require("domain", catalog.ids(catalog.domains), "Choose a domain.")
    valid_groups = catalog.ids(catalog.ad_groups)
    ad_groups = [g for g in form.getlist("ad_groups") if g in valid_groups]
    chosen_security = set(form.getlist("security"))
    security = {o.id: o.id in chosen_security for o in catalog.security_options}

    # --- 5. Automation options ---------------------------------------------
    chosen_automation = set(form.getlist("automation"))
    automation = {o.id: o.id in chosen_automation for o in catalog.automation_options}
    if automation.get("domain_join") and not domain_join:
        errors["automation"] = "The Domain Join step is selected but Domain Join is set to No."

    # --- 6. Approval / change information ----------------------------------
    business_justification = require_text("business_justification", "Business justification", 2000)
    change_ticket = optional_text("change_ticket", 32)
    if environment == "prod" and not change_ticket:
        errors["change_ticket"] = "A change ticket is required for Production."
    approver = require("approver", catalog.ids(catalog.approvers), "Choose an approver.")

    planned = _date(get("planned_date"))
    required_by = _date(get("required_by_date"))
    if not planned:
        errors["planned_date"] = "Choose a planned provisioning date."
    elif planned < today:
        errors["planned_date"] = "The planned date cannot be in the past."
    if not required_by:
        errors["required_by_date"] = "Choose the date the server is required by."
    elif planned and required_by < planned:
        errors["required_by_date"] = "Must be on or after the planned provisioning date."
    comments = optional_text("comments", 2000)

    if errors:
        return None, errors
    return (
        ProvisionRequest(
            requester_name=requester_name,
            requester_email=requester_email,
            department=department,
            environment=environment,
            request_type=request_type,
            source_server=source_server,
            hostname=hostname,
            platform=platform,
            os=os_id,
            os_family=os_family,
            os_template=os_template,
            location=location,
            datacenter=datacenter,
            server_role=server_role,
            application=application,
            business_owner=business_owner,
            technical_owner=technical_owner,
            cpu=cpu,
            memory_gb=int(memory),
            os_disk_gb=int(os_disk),
            additional_disk_gb=additional_disk,
            disk_type=disk_type,
            network=network,
            vlan=vlan_id,
            ip_assignment=ip_assignment,
            ip_address=ip_address,
            prefix_length=prefix,
            gateway=gateway,
            domain_join=domain_join,
            domain=domain,
            ad_groups=ad_groups,
            security=security,
            automation=automation,
            business_justification=business_justification,
            change_ticket=change_ticket,
            approver=approver,
            planned_date=planned.isoformat(),
            required_by_date=required_by.isoformat(),
            comments=comments,
            requested_by=requester_email,
        ),
        {},
    )
