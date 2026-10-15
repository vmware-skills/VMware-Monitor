"""vSphere privileges each MCP tool needs (HLD §17 D-17), read by ``get_access_info``.

Derived from the calls each tool's ops make, using pyVmomi's own metadata
(``_GetMethodInfo(name).privId`` for a method, the property's ``privId`` for a
read). Every inventory read goes through a ContainerView
(``ViewManager.CreateContainerView`` = System.View) and the PropertyCollector; the
entity properties it reads (``summary``, ``runtime``, ``config``…) carry no privId
of their own, which vSphere checks as System.Read. History collectors
(``CreateCollectorForEvents`` / ``CreateCollectorForTasks``), ``QueryPerf``,
``QueryAssignedLicenses`` and ``OptionManager.QueryOptions`` are System.View.

The family gate ``scripts/lib/access_requirements_cover_tools.py`` checks that
the keys are exactly the registered tools and every id is one vSphere defines.
"""

from __future__ import annotations

_READ = ["System.View", "System.Read"]

REQUIREMENTS: dict[str, list[str] | None] = {
    # HasPrivilegeOnEntities / RetrieveAllPermissions / roleList are all System.View.
    "get_access_info": ["System.View"],
    "list_virtual_machines": _READ,
    "list_esxi_hosts": _READ,
    "list_all_datastores": _READ,
    "list_all_clusters": _READ,
    "list_all_networks": _READ,
    "get_alarms": _READ,
    "get_events": _READ,
    "get_host_sensors": _READ,
    "get_host_services": _READ,
    "get_vm_info": _READ,
    "list_vm_snapshots": _READ,
    "find_aging_snapshots": _READ,
    "get_vm_backup_snapshot_history": _READ,
    "get_datastore_capacity": _READ,
    "get_resource_pool_usage": _READ,
    "get_cluster_health_summary": _READ,
    "get_cross_vcenter_attention": _READ,
    "investigate_vm": _READ,
    "investigate_host": _READ,
    "investigate_datastore": _READ,
    "get_vm_performance": _READ,
    "get_host_performance": _READ,
    "get_host_memory_tiering": _READ,
    "list_active_tasks": _READ,
    # SessionManager.sessionList is gated on Sessions.TerminateSession.
    "list_active_sessions": [*_READ, "Sessions.TerminateSession"],
    # HostCertificateManager.certificateInfo is gated on Certificate.Manage.
    "get_certificate_status": [*_READ, "Certificate.Manage"],
    "get_license_status": _READ,
    "get_ntp_status": _READ,
    # Events (System.View) plus DiagnosticManager.BrowseDiagnosticLog (Global.Diagnostics).
    "scan_host_logs": [*_READ, "Global.Diagnostics"],
    # vSphere Automation REST with this account, no pyVmomi call. vLCM documents
    # VcIntegrity.lifecycleSoftwareSpecification.Read for compliance; it is a vCenter
    # plugin privilege an ESXi target does not know, and one unknown id fails the whole
    # HasPrivilegeOnEntities batch. The appliance endpoint is authorized outside vCenter's
    # privilege set. So the deciding permission is not one this check can ask: None.
    "get_cluster_patch_compliance": None,
    "get_cluster_last_apply_result": None,
    "get_vcenter_deployment_size": None,
}
