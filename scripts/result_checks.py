"""Evidence requirements for the current Docker subset, not full acceptance."""
import json
import re

DOCKER_CHECKS = {
    "image_import", "run", "exit_code", "stdin", "tty", "lifecycle", "mount_tmpfs",
    "privileged_mount", "net_admin", "guest_user_namespace", "read_only", "bind_mount",
    "volume", "memory_oom", "pids_limit", "default_seccomp", "container_compute",
    "bridge_dns", "published_port_guest", "docker_cp", "save_load", "buildkit_multistage",
    "restart_policy", "healthcheck", "ipv6", "signals", "gpu_fixture_import", "gpu_compute",
    "external_network", "full_docker_compatibility", "container_threads", "container_thread_semantics",
    "container_dispatch_semantics",
    "container_mapping_semantics",
    "inspect_rename", "restart", "pause_unpause", "update_limits", "top_stats", "wait_exit", "attach",
    "commit_diff_history", "export_import", "image_tag_remove", "events", "network_connect_disconnect",
    "port_inspect", "context", "system_df", "prune_objects", "swarm_init", "swarm_service",
    "swarm_secret_config", "swarm_vip", "swarm_scale", "swarm_cleanup",
}


def broker_cache_evidence(log):
    rows = re.findall(r"^HARDDIND_BROKER_CACHE hits=(\d+) stores=(\d+)$", log, flags=re.MULTILINE)
    if len(rows) != 1:
        return {"verified": False, "hits": None, "stores": None}
    hits, stores = map(int, rows[0])
    return {"verified": 0 < hits <= stores, "hits": hits, "stores": stores}


def deferred_ipi_evidence(log):
    rows = re.findall(r"^HARDDIND_DEFER_IPI enabled=1 received=(\d+) deferred=(\d+) handled=(\d+)$", log, flags=re.MULTILINE)
    if len(rows) != 1:
        return {"verified": False, "received": None, "deferred": None, "handled": None}
    received, deferred, handled = map(int, rows[0])
    return {"verified": 0 < deferred <= received and 0 < handled <= received,
            "received": received, "deferred": deferred, "handled": handled}


def mprotect_evidence(log):
    rows = re.findall(r"^HARDDIND_MPROTECT_SYNC enabled=1 pages=(\d+)$", log, flags=re.MULTILINE)
    pages = int(rows[0]) if len(rows) == 1 else None
    return {"verified": pages is not None and pages > 0, "pages": pages}


def docker_evidence(log, exit_code, timed_out):
    rows = []
    for line in log.splitlines():
        if line.startswith("HARDDIND_RESULT "):
            rows.append(json.loads(line.removeprefix("HARDDIND_RESULT ")))
    endings = re.findall(r"^HARDDIND_DOCKER_END status=(\d+)$", log, flags=re.MULTILINE)
    names = [row.get("name") for row in rows]
    finished = not timed_out and exit_code == 0 and len(endings) == 1
    complete = len(names) == len(DOCKER_CHECKS) and set(names) == DOCKER_CHECKS
    passed = finished and complete and endings == ["0"] and all(r.get("status") == "pass" for r in rows)
    return {"checks": rows, "finished": finished, "checks_complete": complete,
            "suite_status": int(endings[0]) if len(endings) == 1 else None,
            "subset_passed": passed}


def memfd_backing_evidence(log, ram=False, stub=False):
    """Require the actual backing report, not just an accepted boot option."""
    memory = re.findall(r'^HARDDIND_RAM backend=(file|memfd) bytes=(\d+)$', log, re.M)
    executable = re.findall(r'^HARDDIND_STUB backend=(file|memfd)$', log, re.M)
    expected_ram = 'memfd' if ram else 'file'
    verified = (bool(memory) and all(mode == expected_ram and int(size) > 0 for mode, size in memory)
                and any(int(size) >= 1024 * 1024 for _, size in memory)
                and len(executable) == 1 and (not stub or executable[0] == 'memfd'))
    return {'verified': verified, 'ram_requested': ram, 'stub_requested': stub,
            'ram_allocations': [{'backend': mode, 'bytes': int(size)} for mode, size in memory],
            'stub_backend': executable[0] if len(executable) == 1 else None}
