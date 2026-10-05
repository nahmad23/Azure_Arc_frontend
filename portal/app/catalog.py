"""Form choices loaded from catalog.yaml."""
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Option:
    id: str
    label: str
    default: bool = False


@dataclass(frozen=True)
class OperatingSystem:
    id: str
    label: str
    family: str  # linux | windows


@dataclass(frozen=True)
class Vlan:
    id: str
    label: str
    network: ipaddress.IPv4Network | None
    gateway: str


@dataclass
class Platform:
    id: str
    label: str
    images: dict[str, str] = field(default_factory=dict)  # os id -> template


def _options(items) -> list[Option]:
    return [
        Option(id=str(i["id"]), label=str(i.get("label", i["id"])), default=bool(i.get("default", False)))
        for i in items or []
    ]


def _ints(items, fallback) -> list[int]:
    return [int(x) for x in (items or fallback)]


class Catalog:
    def __init__(self, data: dict):
        self.departments = _options(data.get("departments"))
        self.environments = _options(data.get("environments"))
        self.request_types = _options(data.get("request_types"))
        self.server_roles = _options(data.get("server_roles"))
        self.domains = _options(data.get("domains"))
        self.ad_groups = _options(data.get("ad_groups"))
        self.security_options = _options(data.get("security_options"))
        self.automation_options = _options(data.get("automation_options"))
        self.approvers = _options(data.get("approvers"))

        self.operating_systems = [
            OperatingSystem(str(o["id"]), str(o.get("label", o["id"])), str(o.get("family", "linux")))
            for o in data.get("operating_systems") or []
        ]

        self.platforms: dict[str, Platform] = {
            str(pid): Platform(str(pid), str(p.get("label", pid)), {str(k): str(v) for k, v in (p.get("images") or {}).items()})
            for pid, p in (data.get("platforms") or {}).items()
        }

        self.locations = _options(data.get("locations"))
        self._datacenters = {
            str(loc["id"]): _options(loc.get("datacenters")) for loc in data.get("locations") or []
        }

        self.networks = _options(data.get("networks"))
        self._vlans: dict[str, list[Vlan]] = {}
        for net in data.get("networks") or []:
            vlans = []
            for v in net.get("vlans") or []:
                cidr = v.get("cidr")
                vlans.append(Vlan(
                    id=str(v["id"]),
                    label=str(v.get("label", v["id"])),
                    network=ipaddress.IPv4Network(cidr) if cidr else None,
                    gateway=str(v.get("gateway", "")),
                ))
            self._vlans[str(net["id"])] = vlans

        compute = data.get("compute") or {}
        self.cpu = _ints(compute.get("cpu"), [2, 4, 8, 16])
        custom = compute.get("custom_cpu") or {}
        self.custom_cpu_min = int(custom.get("min", 1))
        self.custom_cpu_max = int(custom.get("max", 64))
        self.memory_gb = _ints(compute.get("memory_gb"), [8, 16, 32, 64])
        self.os_disk_gb = _ints(compute.get("os_disk_gb"), [80, 100, 150])
        extra = compute.get("additional_disk_gb") or {}
        self.additional_disk_min = int(extra.get("min", 10))
        self.additional_disk_max = int(extra.get("max", 4096))
        self.disk_types = _options(compute.get("disk_types") or [{"id": "ssd", "label": "SSD"}, {"id": "standard", "label": "Standard"}])

        if not self.platforms:
            raise ValueError("catalog.yaml defines no platforms")
        if not self.operating_systems:
            raise ValueError("catalog.yaml defines no operating_systems")

    # --- lookups -------------------------------------------------------------
    @staticmethod
    def ids(options) -> set[str]:
        return {o.id for o in options}

    def os(self, os_id: str) -> OperatingSystem | None:
        return next((o for o in self.operating_systems if o.id == os_id), None)

    def datacenters(self, location: str) -> list[Option]:
        return self._datacenters.get(location, [])

    def vlans(self, network: str) -> list[Vlan]:
        return self._vlans.get(network, [])

    def vlan(self, network: str, vlan_id: str) -> Vlan | None:
        return next((v for v in self.vlans(network) if v.id == vlan_id), None)

    def platforms_for_os(self, os_id: str) -> list[Platform]:
        """Platforms that have a template for this OS (all platforms when no OS is chosen)."""
        return [p for p in self.platforms.values() if not os_id or os_id in p.images]

    def label(self, options, option_id: str) -> str:
        return next((o.label for o in options if o.id == option_id), option_id)


def load_catalog(path: str) -> Catalog:
    return Catalog(yaml.safe_load(Path(path).read_text()) or {})
