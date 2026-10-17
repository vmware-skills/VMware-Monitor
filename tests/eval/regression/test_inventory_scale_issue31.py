"""Regression — large-inventory scale via PropertyCollector (GitHub issue #31).

A user with ~8,000 VMs / ~340 hosts / 1,000+ datastores reported that even
``limit=20`` queries timed out (120s for VMs, 300s for hosts). Root cause: the
list functions walked a ContainerView and then touched pyVmomi *lazy* properties
(``vm.config.hardware.numCPU``, ``vm.runtime.host.name``, ``len(host.vm)``,
``folder_path(vm)`` …), each a separate SOAP round-trip. With thousands of
objects that is tens of thousands of round-trips, and ``limit`` was applied only
*after* the full collection — so it never reduced the work.

Fix locked here: every list/find function fetches all needed properties in a
single ``PropertyCollector.RetrievePropertiesEx`` call (paged via continuation
tokens); ``folder_path`` is resolved from a batched folder map instead of a
per-VM parent walk. No per-object lazy attribute access is allowed.

The fake managed objects below raise on ANY attribute access, so if the code
ever regresses to touching ``obj.config`` / ``obj.runtime`` / ``obj.parent`` the
test fails loudly rather than silently going slow.
"""

from __future__ import annotations

from pyVmomi import vim

from vmware_monitor.ops import inventory


class _FakeStub:
    """Minimal SOAP stub so a real ContainerView moref can be Destroy()'d."""

    def InvokeMethod(self, mo, info, args):  # noqa: N802 - pyVmomi contract
        return None


class _NoLazyMO:
    """Fake managed object: any attribute read is a lazy round-trip = a bug."""

    def __init__(self, label: str) -> None:
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name: str):  # pragma: no cover - only hit on regression
        if name == "_moId":
            # Set on the stub when pyVmomi builds the reference: reading it is
            # local, not a property round-trip (list_clusters reports it).
            return object.__getattribute__(self, "_label")
        raise AssertionError(
            f"lazy property access '{name}' on {object.__getattribute__(self, '_label')}"
            " — list/find must use PropertyCollector, not per-object attributes"
        )

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other


class _Prop:
    def __init__(self, name, val) -> None:
        self.name = name
        self.val = val


class _ObjContent:
    def __init__(self, obj, props: dict) -> None:
        self.obj = obj
        self.propSet = [_Prop(k, v) for k, v in props.items()]


class _Batch:
    def __init__(self, objects, token=None) -> None:
        self.objects = objects
        self.token = token


class _FakePropertyCollector:
    """Returns canned ObjectContent keyed by the requested managed-object type."""

    def __init__(self, fixtures: dict, page_size: int = 1000) -> None:
        self._fixtures = fixtures
        self._page_size = page_size
        self._pending: dict[str, list] = {}
        self._counter = 0
        self.call_count = 0

    def _pages(self, rows):
        return [rows[i:i + self._page_size] for i in range(0, len(rows), self._page_size)] or [[]]

    def RetrievePropertiesEx(self, specs, options):  # noqa: N802
        self.call_count += 1
        obj_type = specs[0].propSet[0].type
        rows = self._fixtures.get(obj_type, [])
        pages = self._pages(rows)
        token = None
        if len(pages) > 1:
            self._counter += 1
            token = f"tok{self._counter}"
            self._pending[token] = pages[1:]
        return _Batch([_ObjContent(o, p) for o, p in pages[0]], token=token)

    def ContinueRetrievePropertiesEx(self, token):  # noqa: N802
        pages = self._pending.pop(token)
        next_token = None
        if len(pages) > 1:
            self._counter += 1
            next_token = f"tok{self._counter}"
            self._pending[next_token] = pages[1:]
        return _Batch([_ObjContent(o, p) for o, p in pages[0]], token=next_token)


class _FakeViewManager:
    def CreateContainerView(self, root, obj_type, recursive):  # noqa: N802
        return vim.view.ContainerView("cv-fake", _FakeStub())


class _FakeContent:
    def __init__(self, pc) -> None:
        self.viewManager = _FakeViewManager()
        self.propertyCollector = pc
        self.rootFolder = _NoLazyMO("rootFolder")


class _FakeSI:
    def __init__(self, fixtures: dict, page_size: int = 1000) -> None:
        self.pc = _FakePropertyCollector(fixtures, page_size)
        self._content = _FakeContent(self.pc)

    def RetrieveContent(self):  # noqa: N802
        return self._content


def _si(fixtures, page_size=1000):
    return _FakeSI(fixtures, page_size)


# --------------------------------------------------------------------------
# VMs + folder_path resolved from a batched folder map
# --------------------------------------------------------------------------

def _folder_tree():
    dc = _NoLazyMO("dc")
    vmfolder = _NoLazyMO("vmfolder")   # the datacenter's root "vm" folder
    colo = _NoLazyMO("colo")
    fixtures = {
        vim.Datacenter: [(dc, {"name": "DC", "parent": None})],
        vim.Folder: [
            (vmfolder, {"name": "vm", "parent": dc}),
            (colo, {"name": "Colocation", "parent": vmfolder}),
        ],
        vim.VirtualApp: [],
    }
    return fixtures, dc, vmfolder, colo


def test_list_vms_folder_path_from_batched_map():
    fixtures, dc, vmfolder, colo = _folder_tree()
    host = _NoLazyMO("host")
    fixtures[vim.HostSystem] = [(host, {"name": "esx-01"})]
    fixtures[vim.VirtualMachine] = [
        (
            _NoLazyMO("vm-nested"),
            {"name": "app-01", "runtime.host": host, "parent": colo},
        ),
        (
            _NoLazyMO("vm-root"),
            {"name": "root-vm", "runtime.host": host, "parent": vmfolder},
        ),
    ]
    result = inventory.list_vms(_si(fixtures))
    paths = {v["name"]: v["folder_path"] for v in result["items"]}
    assert paths["app-01"] == "/Colocation"   # datacenter root "vm" folder omitted
    assert paths["root-vm"] == "/"            # VM directly in the dc vmFolder


def test_list_vms_shape_and_host_resolution():
    fixtures, dc, vmfolder, colo = _folder_tree()
    host = _NoLazyMO("host")
    fixtures[vim.HostSystem] = [(host, {"name": "esx-01"})]
    fixtures[vim.VirtualMachine] = [
        (
            _NoLazyMO(f"vm-{name}"),
            {
                "name": name,
                "parent": vmfolder,
                "runtime.powerState": "poweredOn",
                "runtime.host": host,
                "config.hardware.numCPU": 4,
                "config.hardware.memoryMB": 8192,
                "config.guestFullName": "Ubuntu 22.04",
                "config.uuid": f"uuid-{name}",
                "guest.ipAddress": "10.0.0.5",
                "guest.toolsRunningStatus": "guestToolsRunning",
            },
        )
        for name in ("web-02", "web-01")
    ]
    result = inventory.list_vms(_si(fixtures), limit=1)
    assert result["total"] == 2
    assert len(result["items"]) == 1
    assert result["items"][0]["name"] == "web-01"   # sorted
    assert result["items"][0]["host"] == "esx-01"


def test_list_vms_pages_through_continuation_tokens():
    fixtures, dc, vmfolder, colo = _folder_tree()
    host = _NoLazyMO("host")
    fixtures[vim.HostSystem] = [(host, {"name": "esx-01"})]
    fixtures[vim.VirtualMachine] = [
        (_NoLazyMO(f"vm{i}"), {"name": f"vm{i:04d}", "runtime.host": host, "parent": vmfolder})
        for i in range(2500)
    ]
    result = inventory.list_vms(_si(fixtures, page_size=1000), limit=5)
    assert result["total"] == 2500


# --------------------------------------------------------------------------
# Hosts / datastores / clusters / networks
# --------------------------------------------------------------------------

def test_list_hosts_vm_count_from_array_property():
    hosts = [
        (
            _NoLazyMO("h1"),
            {
                "name": "esx-01",
                "runtime.connectionState": "connected",
                "runtime.powerState": "poweredOn",
                "hardware.cpuInfo.numCpuCores": 32,
                "hardware.cpuInfo.numCpuThreads": 64,
                "hardware.memorySize": 512 * 1024**3,
                "config.product.version": "8.0.3",
                "config.product.build": "12345",
                "vm": [_NoLazyMO("vm-a"), _NoLazyMO("vm-b")],
                "summary.quickStats.uptime": 99999,
            },
        )
    ]
    rows = inventory.list_hosts(_si({vim.HostSystem: hosts}))["items"]
    assert rows[0]["vm_count"] == 2
    assert rows[0]["memory_gb"] == 512


def test_list_datastores_capacity_and_count():
    ds = [
        (
            _NoLazyMO("ds1"),
            {
                "name": "datastore1",
                "summary.type": "VMFS",
                "summary.freeSpace": 100 * 1024**3,
                "summary.capacity": 500 * 1024**3,
                "summary.accessible": True,
                "summary.url": "ds:///vmfs/1",
                "vm": [_NoLazyMO("vm-a")],
            },
        )
    ]
    rows = inventory.list_datastores(_si({vim.Datastore: ds}))["items"]
    assert rows[0]["free_gb"] == 100.0
    assert rows[0]["vm_count"] == 1


def test_list_clusters_config_flags():
    clusters = [
        (
            _NoLazyMO("c1"),
            {
                "name": "prod-cluster",
                "host": [_NoLazyMO("h1"), _NoLazyMO("h2")],
                "configuration.drsConfig.enabled": True,
                "configuration.drsConfig.defaultVmBehavior": "fullyAutomated",
                "configuration.dasConfig.enabled": False,
                "summary.totalCpu": 96000,
                "summary.totalMemory": 1024 * 1024**3,
            },
        )
    ]
    rows = inventory.list_clusters(_si({vim.ClusterComputeResource: clusters}))["items"]
    assert rows[0]["host_count"] == 2
    assert rows[0]["drs_enabled"] is True
    assert rows[0]["ha_enabled"] is False


def test_list_networks_defaults_accessible():
    nets = [(_NoLazyMO("n1"), {"name": "VM Network", "vm": [_NoLazyMO("vm-a")]})]
    rows = inventory.list_networks(_si({vim.Network: nets}))["items"]
    assert rows[0]["vm_count"] == 1
    assert rows[0]["accessible"] is True


def test_find_vm_by_name_returns_object_without_lazy_access():
    target = _NoLazyMO("vm:db-01")
    fixtures = {
        vim.VirtualMachine: [
            (_NoLazyMO("vm:web-01"), {"name": "web-01"}),
            (target, {"name": "db-01"}),
        ]
    }
    assert inventory.find_vm_by_name(_si(fixtures), "db-01") is target
    assert inventory.find_vm_by_name(_si(fixtures), "nope") is None
