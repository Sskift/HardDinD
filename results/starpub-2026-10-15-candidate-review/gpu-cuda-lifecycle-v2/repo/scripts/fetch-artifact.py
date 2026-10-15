#!/usr/bin/env python3
"""Fetch one checksum-pinned artifact from sources.lock.json."""
import hashlib
import json
import pathlib
import sys
import urllib.parse
import urllib.request

repo = pathlib.Path(__file__).resolve().parents[1]
item = json.loads((repo / "sources.lock.json").read_text())[sys.argv[1]]
cache = pathlib.Path(sys.argv[2]).resolve()
cache.mkdir(parents=True, exist_ok=True)
path = cache / pathlib.PurePosixPath(urllib.parse.urlparse(item["url"]).path).name

def digest(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()

if not path.exists():
    part = path.with_suffix(".part")
    try:
        with urllib.request.urlopen(item["url"], timeout=60) as src, part.open("wb") as dst:
            for b in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(b)
        if digest(part) != item["sha256"]:
            raise SystemExit("artifact checksum mismatch")
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
if digest(path) != item["sha256"]:
    raise SystemExit("cached artifact checksum mismatch")
print(path)
