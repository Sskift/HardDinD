#!/usr/bin/env python3
"""Reparse archived evidence; do not compile or execute the filter models."""
import ast
import gzip
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def sha(data):
    return hashlib.sha256(data).hexdigest()


def check(condition, message):
    if not condition:
        raise SystemExit(message)


def main():
    snapshot = ROOT / "snapshot"
    validation = ROOT / "validation"
    manifest = json.loads((snapshot / "manifest.json").read_text())
    report = json.loads((validation / "report.json").read_text())
    audit = json.loads((validation / "owner-audit.json").read_text())
    check(sha((snapshot / "manifest.json").read_bytes()) == audit["snapshot_sha256"],
          "snapshot manifest changed")
    checked = 0
    omitted = []
    for name, digest in manifest.items():
        p = snapshot / name
        if p.is_file():
            data = p.read_bytes()
        elif p.with_name(p.name + ".gz").is_file():
            data = gzip.decompress(p.with_name(p.name + ".gz").read_bytes())
        elif name.startswith("filter-build/") and name.endswith(".inc"):
            data = (validation / Path(name).name).read_bytes()
        else:
            omitted.append(name)
            continue
        check(sha(data) == digest, "snapshot hash mismatch: " + name)
        checked += 1
    # The remote manifest also lists binaries and the stopped build observer.
    allowed_omissions = {"stopped-observer.json"}
    allowed_omissions.update(f"filter-build/{kind}-b{b}-a{a}"
                             for kind in ("sim", "real", "probe")
                             for b in (0, 1) for a in (0, 1))
    check(set(omitted) == allowed_omissions, "unexpected missing snapshot files")
    check(sum(n.startswith("source/") for n in manifest) == 14,
          "expected 14 frozen kernel sources")
    for name, digest in report["source_sha256"].items():
        check(sha((validation / name).read_bytes()) == digest,
              "validation hash mismatch: " + name)
    check((validation / "extractor.py").read_bytes() ==
          (snapshot / "tests/models/build_seccomp_filter_test.py").read_bytes(),
          "extractor differs from frozen candidate")
    source = (snapshot / "source/arch/um/kernel/skas/stub_exe.c").read_text()
    match = re.search(r"struct sock_filter filter\[\]\s*=\s*\{(.*?)\n\s*\};",
                      source, re.S)
    check(match is not None, "missing filter array")
    body = match.group(1).replace("(init_data.stub_start)", "(STUB_PAGE_ADDR)")
    body = body.replace("init_data.gpu_bridge", "GPU_BRIDGE_CONST")
    body = body.replace("init_data.gpu_async", "GPU_ASYNC_CONST")
    check(body == (validation / "seccomp_filter_body.inc").read_text(),
          "tested BPF differs from frozen kernel filter")
    tree = ast.parse((validation / "extractor.py").read_text())
    headers = [ast.literal_eval(node.value) for node in tree.body
               if isinstance(node, ast.Assign) and any(
                   isinstance(t, ast.Name) and t.id == "HEADER" for t in node.targets)]
    check(headers == [(validation / "seccomp_filter_prelude.inc").read_text()],
          "test prelude differs from frozen extractor")
    check(report["status"] == "finished" and report["exit_code"] == 0,
          "owner run did not finish successfully")
    expected = {f"{kind}-b{b}-a{a}" for kind in ("sim", "real")
                for b in (0, 1) for a in (0, 1)}
    rows = report["rows"]
    check(len(rows) == 8 and {r["name"] for r in rows} == expected,
          "missing or duplicated test combination")
    check(set(audit["groups"]) == {str(r["pid"]) for r in rows},
          "independent audit does not cover all process groups")
    check(audit["all_empty"] is True and
          all(v == [[], []] for v in audit["groups"].values()),
          "independent cleanup audit is not empty")
    for row in rows:
        name = row["name"]
        check(row["build_exit"] == 0 and row["exit_code"] == 0 and
              row["timed_out"] is False, "failed execution: " + name)
        cleanup = row["cleanup"]
        check(cleanup["process_group"] == row["pid"] and
              cleanup["clean_shutdown"] is True and
              cleanup["forced_cleanup"] is False and
              not cleanup["remaining_live_pids"] and
              not cleanup["remaining_zombie_pids"], "cleanup failed: " + name)
        kind, b, a = re.fullmatch(r"(sim|real)-b([01])-a([01])", name).groups()
        check(f"-DGPU_BRIDGE={b}" in row["build_command"] and
              f"-DGPU_ASYNC={a}" in row["build_command"],
              "wrong build configuration: " + name)
        expected_stdout = (f"seccomp filter sim: OK bridge={b} async={a} (24 probes)\n"
                           if kind == "sim" else
                           f"seccomp real-filter model: OK bridge={b} async={a}\n")
        check(gzip.decompress((validation / (name + ".stdout.gz")).read_bytes())
              .decode() == expected_stdout, "unexpected model result: " + name)
        check(not gzip.decompress((validation / (name + ".stderr.gz")).read_bytes()),
              "unexpected model stderr: " + name)
        for stream in ("stdout", "stderr"):
            check(not gzip.decompress((validation / f"{name}.build.{stream}.gz")
                                      .read_bytes()), "unexpected compiler output: " + name)
    failed = gzip.decompress((snapshot / "failed-build.log.gz").read_bytes()).decode()
    check('arithmetic expression: expecting EOF: "16#00000726"' in failed and
          "Error 2" in failed,
          "missing preserved full-kernel build failure")
    print(json.dumps({"archive_recheck": "pass", "snapshot_files_verified": checked,
                      "explicitly_omitted_files": omitted, "model_executions": 8,
                      "interpreter_probes": 96, "real_syscall_representatives": 20,
                      "process_groups_empty": 8, "full_kernel_build": "failed",
                      "internal_gpu_acceptance": "not_tested"}, indent=2))


if __name__ == "__main__":
    main()
