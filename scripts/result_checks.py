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
}


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
