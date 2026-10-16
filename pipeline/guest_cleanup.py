"""Clean only the private session created for this experimental guest."""
import os
import pathlib
import signal
import time


def group_members(pgid):
    live, zombies = [], []
    for path in pathlib.Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(") ", 1)[1].split()
            if int(fields[2]) == pgid:
                (zombies if fields[0] == "Z" else live).append(int(path.parent.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return sorted(live), sorted(zombies)


def cleanup_group(pgid):
    if pgid == os.getpgrp():
        raise RuntimeError("guest group must differ from the runner's group")
    live, zombies = group_members(pgid)
    initial_live, initial_zombies = live, zombies
    def describe(pids):
        result = []
        for pid in pids:
            base = pathlib.Path("/proc") / str(pid)
            row = {"pid": pid}
            try:
                fields = (base / "stat").read_text().rsplit(") ", 1)[1].split()
                row.update({"state": fields[0], "ppid": int(fields[1]),
                            "process_group": int(fields[2]), "start_time_ticks": fields[19]})
            except (OSError, ProcessLookupError):
                row["stat_unavailable"] = True
            # exe can disappear before stat during exit. Keep the identity and
            # state already read instead of throwing all evidence away.
            for key in ("comm", "wchan", "exe"):
                try:
                    row[key] = os.readlink(base / key) if key == "exe" else (base / key).read_text().strip()
                except OSError as error:
                    row[key + "_errno"] = error.errno
            result.append(row)
        return result
    initial_processes = describe(live + zombies)
    # Parent-death signals and the host init's reaping can lag wait(); a runner
    # that is unmapping several GiB of guest RAM can take seconds on a loaded host.
    grace = time.monotonic() + 10
    while live and time.monotonic() < grace:
        time.sleep(0.05)
        live, zombies = group_members(pgid)
    forced = bool(live)
    forced_processes = describe(live) if forced else []
    if live:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2
    while (live or zombies) and time.monotonic() < deadline:
        time.sleep(0.05)
        live, zombies = group_members(pgid)
    return {"process_group": pgid, "forced_cleanup": forced,
            "initial_live_pids": initial_live, "initial_zombie_pids": initial_zombies,
            "initial_processes": initial_processes, "forced_processes": forced_processes,
            "remaining_live_pids": live, "remaining_zombie_pids": zombies,
            "clean_shutdown": not forced and not live and not zombies}
