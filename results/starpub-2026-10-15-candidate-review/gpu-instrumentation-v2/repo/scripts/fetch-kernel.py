#!/usr/bin/env python3
"""Download a checksum-pinned upstream kernel; never use a moving branch."""
import hashlib
import json
import pathlib
import sys
import tarfile
import urllib.request

repo = pathlib.Path(__file__).resolve().parents[1]
lock = json.loads((repo / "sources.lock.json").read_text())["linux"]
work = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else repo / ".work").resolve()
cache = work / "cache"
cache.mkdir(parents=True, exist_ok=True)
archive = cache / ("linux-" + lock["version"] + ".tar.xz")

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for data in iter(lambda: f.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()

if not archive.exists():
    temp = archive.with_suffix(".part")
    try:
        with urllib.request.urlopen(lock["url"], timeout=60) as src, temp.open("wb") as dst:
            for data in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(data)
        if digest(temp) != lock["sha256"]:
            raise SystemExit("download checksum mismatch")
        temp.replace(archive)
    finally:
        temp.unlink(missing_ok=True)
if digest(archive) != lock["sha256"]:
    raise SystemExit("cached source checksum mismatch")
source = work / ("linux-" + lock["version"])
if not source.exists():
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            target = (work / member.name).resolve()
            if not target.is_relative_to(work):
                raise SystemExit("unsafe archive member")
            if member.issym() or member.islnk():
                link = (target.parent / member.linkname).resolve() if member.issym() else (work / member.linkname).resolve()
                if not link.is_relative_to(work):
                    raise SystemExit("unsafe archive link")
            if member.isdev():
                raise SystemExit("unexpected device in source archive")
        tar.extractall(work)
print(source)
