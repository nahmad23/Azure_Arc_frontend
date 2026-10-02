"""Validation of a submitted provisioning request."""
import ipaddress
import re
from dataclasses import asdict, dataclass, field

from .catalog import Catalog

HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Windows NetBIOS names are limited to 15 characters.
MAX_HOSTNAME = {"windows": 15, "linux": 63}


@dataclass
class ProvisionRequest:
    platform: str
    os_family: str
    os_image: str
    hostname: str
    environment: str
    location: str
    network: str
    cpu: int
    memory_gb: int
    os_disk_gb: int
    data_disks_gb: list[int] = field(default_factory=list)
    ip_mode: str = "dhcp"
    ip_address: str = ""
    prefix_length: int | None = None
    gateway: str = ""
    dns_servers: list[str] = field(default_factory=list)
    domain_join: bool = False
    domain: str = ""
    ou_path: str = ""
    application: str = ""
    owner_email: str = ""
    cost_center: str = ""
    change_ticket: str = ""
    notes: str = ""
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
        return "Use lowercase letters, digits and hyphens; must not start or end with a hyphen."
    if hostname.isdigit():
        return "Hostname cannot be all digits."
    return None


def _int(value: str, name: str, errors: dict) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        errors[name] = "Enter a whole number."
        return None


def parse_request(form, catalog: Catalog, requested_by: str) -> tuple[ProvisionRequest | None, dict[str, str]]:
    """Validate raw form data against the catalog.

    `form` is a Starlette FormData (supports .get and .getlist).
    Returns (request, {}) on success or (None, {field: message}) on failure.
    """
    errors: dict[str, str] = {}
    get = lambda k: (form.get(k) or "").strip()  # noqa: E731

    platform = get("platform")
    os_family = get("os_family")
    if platform not in catalog.platforms:
        errors["platform"] = "Choose a platform."
    if os_family not in ("linux", "windows"):
        errors["os_family"] = "Choose Linux or Windows."

    os_image = get("os_image")
    if not errors.get("platform") and not errors.get("os_family"):
        if os_image not in {o.id for o in catalog.images(platform, os_family)}:
            errors["os_image"] = "Choose an OS image."

    hostname = get("hostname").lower()
    if msg := check_hostname(hostname, os_family):
        errors["hostname"] = msg

    environment = get("environment")
    if environment not in {e.id for e in catalog.environments}:
        errors["environment"] = "Choose an environment."

    location = get("location")
    if location not in {o.id for o in catalog.locations(platform)}:
        errors["location"] = "Choose a location."
    network = get("network")
    if network not in {o.id for o in catalog.networks(platform)}:
        errors["network"] = "Choose a network."

    cpu = _int(get("cpu"), "cpu", errors)
    if cpu is not None and cpu not in catalog.cpu:
        errors["cpu"] = "Choose a CPU count from the list."
    memory = _int(get("memory_gb"), "memory_gb", errors)
    if memory is not None and memory not in catalog.memory_gb:
        errors["memory_gb"] = "Choose a memory size from the list."
    os_disk = _int(get("os_disk_gb"), "os_disk_gb", errors)
    if os_disk is not None and not catalog.os_disk_min <= os_disk <= catalog.os_disk_max:
        errors["os_disk_gb"] = f"Must be between {catalog.os_disk_min} and {catalog.os_disk_max} GB."

    data_disks: list[int] = []
    raw_disks = [d.strip() for d in form.getlist("data_disk_gb") if d and d.strip()]
    if len(raw_disks) > catalog.max_data_disks:
        errors["data_disks"] = f"At most {catalog.max_data_disks} data disks."
    for d in raw_disks:
        size = _int(d, "data_disks", errors)
        if size is None:
            break
        if not catalog.data_disk_min <= size <= catalog.data_disk_max:
            errors["data_disks"] = (
                f"Each data disk must be between {catalog.data_disk_min} and {catalog.data_disk_max} GB."
            )
            break
        data_disks.append(size)

    ip_mode = get("ip_mode") or "dhcp"
    ip_address = gateway = ""
    prefix: int | None = None
    dns: list[str] = []
    if ip_mode not in ("dhcp", "static"):
        errors["ip_mode"] = "Choose DHCP or static."
    elif ip_mode == "static":
        try:
            iface = ipaddress.IPv4Interface(f"{get('ip_address')}/{get('prefix_length') or 'x'}")
            ip_address, prefix = str(iface.ip), iface.network.prefixlen
        except ValueError:
            errors["ip_address"] = "Enter a valid IPv4 address and prefix length (e.g. 24)."
            iface = None
        try:
            gw = ipaddress.IPv4Address(get("gateway"))
            gateway = str(gw)
            if iface is not None and gw not in iface.network:
                errors["gateway"] = f"Gateway must be inside {iface.network}."
        except ValueError:
            errors["gateway"] = "Enter a valid gateway IPv4 address."
        for entry in re.split(r"[,\s]+", get("dns_servers")):
            if not entry:
                continue
            try:
                dns.append(str(ipaddress.IPv4Address(entry)))
            except ValueError:
                errors["dns_servers"] = f"'{entry}' is not a valid IPv4 address."
                break
        if not dns and "dns_servers" not in errors:
            errors["dns_servers"] = "Enter at least one DNS server."

    domain_join = get("domain_join") == "yes"
    domain = get("domain")
    ou_path = get("ou_path")
    if domain_join and domain not in {d.id for d in catalog.domains}:
        errors["domain"] = "Choose a domain."

    application = get("application")
    if not application:
        errors["application"] = "Application / service name is required."
    owner_email = get("owner_email")
    if not EMAIL_RE.match(owner_email):
        errors["owner_email"] = "Enter the owner's email address."
    change_ticket = get("change_ticket")
    if environment == "prod" and not change_ticket:
        errors["change_ticket"] = "A change / ticket number is required for Production."

    for name, limit in (("application", 64), ("cost_center", 32), ("change_ticket", 32), ("ou_path", 256), ("notes", 1000)):
        if len(get(name)) > limit:
            errors[name] = f"At most {limit} characters."

    if errors:
        return None, errors
    return (
        ProvisionRequest(
            platform=platform,
            os_family=os_family,
            os_image=os_image,
            hostname=hostname,
            environment=environment,
            location=location,
            network=network,
            cpu=cpu,
            memory_gb=memory,
            os_disk_gb=os_disk,
            data_disks_gb=data_disks,
            ip_mode=ip_mode,
            ip_address=ip_address,
            prefix_length=prefix,
            gateway=gateway,
            dns_servers=dns,
            domain_join=domain_join,
            domain=domain if domain_join else "",
            ou_path=ou_path if domain_join else "",
            application=application,
            owner_email=owner_email,
            cost_center=get("cost_center"),
            change_ticket=change_ticket,
            notes=get("notes"),
            requested_by=requested_by,
        ),
        {},
    )
