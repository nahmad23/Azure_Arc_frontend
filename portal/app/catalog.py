"""Form choices loaded from catalog.yaml."""
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Option:
    id: str
    label: str


def _options(items) -> list[Option]:
    return [Option(id=str(i["id"]), label=str(i.get("label", i["id"]))) for i in items or []]


class Catalog:
    def __init__(self, data: dict):
        self.environments = _options(data.get("environments"))
        self.domains = _options(data.get("domains"))
        sizes = data.get("sizes", {})
        self.cpu: list[int] = [int(c) for c in sizes.get("cpu", [1, 2, 4, 8])]
        self.memory_gb: list[int] = [int(m) for m in sizes.get("memory_gb", [2, 4, 8, 16])]
        os_disk = sizes.get("os_disk_gb", {})
        self.os_disk_min = int(os_disk.get("min", 40))
        self.os_disk_max = int(os_disk.get("max", 500))
        self.os_disk_default = int(os_disk.get("default", self.os_disk_min))
        data_disk = sizes.get("data_disk_gb", {})
        self.data_disk_min = int(data_disk.get("min", 10))
        self.data_disk_max = int(data_disk.get("max", 4096))
        self.max_data_disks = int(sizes.get("max_data_disks", 4))

        self.platforms: dict[str, dict] = {}
        for pid, p in (data.get("platforms") or {}).items():
            images = p.get("images", {})
            self.platforms[str(pid)] = {
                "label": p.get("label", pid),
                "locations": _options(p.get("locations")),
                "networks": _options(p.get("networks")),
                "images": {
                    "linux": _options(images.get("linux")),
                    "windows": _options(images.get("windows")),
                },
            }
        if not self.platforms:
            raise ValueError("catalog.yaml defines no platforms")

    @property
    def platform_options(self) -> list[Option]:
        return [Option(pid, p["label"]) for pid, p in self.platforms.items()]

    def locations(self, platform: str) -> list[Option]:
        return self.platforms.get(platform, {}).get("locations", [])

    def networks(self, platform: str) -> list[Option]:
        return self.platforms.get(platform, {}).get("networks", [])

    def images(self, platform: str, os_family: str) -> list[Option]:
        return self.platforms.get(platform, {}).get("images", {}).get(os_family, [])


def load_catalog(path: str) -> Catalog:
    return Catalog(yaml.safe_load(Path(path).read_text()) or {})
