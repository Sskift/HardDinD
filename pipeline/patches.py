#!/usr/bin/env python3
"""Apply an overlapping patch series reproducibly without replacing local edits."""
import hashlib
import json
import pathlib
import subprocess
import sys
import tarfile
import tempfile


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def content(path):
    return path.read_bytes() if path.exists() else None


def apply_series(source, archive, patches):
    stamp = source / ".harddind-patches.json"
    previous = json.loads(stamp.read_text()) if stamp.exists() else {}
    paths = set(previous.get("files", {}))
    for patch in patches:
        for line in patch.read_text().splitlines():
            if line.startswith(("--- a/", "+++ b/")):
                paths.add(line[6:].split("\t", 1)[0])
    for name in paths:
        p = pathlib.PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not name.startswith(("arch/um/", "arch/x86/um/", "fs/fuse/")):
            raise ValueError(f"unexpected patch path: {name}")
        if not (source / p).resolve().is_relative_to(source.resolve()):
            raise ValueError(f"patch path escapes source: {name}")
    with tempfile.TemporaryDirectory(prefix="harddind-patches-") as directory:
        stage = pathlib.Path(directory)
        # Stream xz once; random extraction repeatedly decompresses the kernel.
        with tarfile.open(archive, mode="r|*") as tar:
            prefix = source.name + "/"
            for member in tar:
                if not member.name.startswith(prefix):
                    continue
                name = member.name[len(prefix):]
                if name not in paths:
                    continue
                if not member.isfile():
                    raise ValueError(f"patch target is not a regular file: {name}")
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
        # Recognize pristine files and any already applied prefix of this series.
        known = {name: {digest(content(stage / name))} for name in paths}
        for patch in patches:
            subprocess.run(["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-d", str(stage)],
                           input=patch.read_bytes(), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for name in paths:
                known[name].add(digest(content(stage / name)))
        for name in paths:
            if name in previous.get("files", {}):
                known[name].add(previous["files"][name])
            if digest(content(source / name)) not in known[name]:
                raise ValueError(f"refusing to overwrite local kernel edit: {name}")
        # All validation happens before the first source write.
        for name in paths:
            value = content(stage / name)
            target = source / name
            if value == content(target):
                continue
            if value is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".harddind-new")
                temporary.write_bytes(value)
                temporary.replace(target)
        stamp.write_text(json.dumps({
            "patches": {p.name: digest(p.read_bytes()) for p in patches},
            "files": {name: digest(content(stage / name)) for name in sorted(paths)},
        }, indent=2) + "\n")


if __name__ == "__main__":
    # patches.py SOURCE ARCHIVE PATCH...  (order is significant)
    apply_series(pathlib.Path(sys.argv[1]).resolve(), pathlib.Path(sys.argv[2]),
                 [pathlib.Path(p).resolve() for p in sys.argv[3:]])
