#!/usr/bin/env python3
"""Fetch checksum-pinned Moby and Go inputs into the isolated build directory."""
import hashlib
import json
import pathlib
import sys
import tarfile
import urllib.request

repo = pathlib.Path(__file__).resolve().parents[1]
lock = json.loads((repo/'sources.lock.json').read_text())
work = pathlib.Path(sys.argv[1]).resolve(); cache = work/'cache'; cache.mkdir(exist_ok=True)
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''): h.update(b)
    return h.hexdigest()
def prepare(key, filename, destination):
    entry=lock[key]; archive=cache/filename
    if not archive.exists():
        temp=archive.with_suffix('.part')
        try:
            with urllib.request.urlopen(entry['url'],timeout=60) as source,temp.open('wb') as dest:
                for b in iter(lambda:source.read(1048576),b''):dest.write(b)
            if sha(temp)!=entry['sha256']:raise RuntimeError('download checksum mismatch: '+filename)
            temp.replace(archive)
        finally:temp.unlink(missing_ok=True)
    if sha(archive)!=entry['sha256']:raise RuntimeError('cached checksum mismatch: '+filename)
    dest=work/destination
    if not dest.exists():
        stage=work/(destination+'.extracting');stage.mkdir()
        with tarfile.open(archive) as tar:
            members=tar.getmembers(); roots={pathlib.PurePosixPath(m.name).parts[0] for m in members}
            if len(roots)!=1:raise RuntimeError('unexpected archive roots')
            root=next(iter(roots))
            for m in members:
                target=(stage/m.name).resolve()
                if not target.is_relative_to(stage) or m.isdev():raise RuntimeError('unsafe archive entry')
                if m.issym() or m.islnk():
                    link=(target.parent/m.linkname).resolve() if m.issym() else (stage/m.linkname).resolve()
                    if not link.is_relative_to(stage):raise RuntimeError('unsafe archive link')
            tar.extractall(stage)
            epoch=members[0].mtime
        (stage/root).rename(dest);stage.rmdir()
        (dest/'.harddind-source.json').write_text(json.dumps({'archive_sha256':entry['sha256'],'epoch':epoch})+'\n')
    metadata=json.loads((dest/'.harddind-source.json').read_text())
    if metadata['archive_sha256']!=entry['sha256']:raise RuntimeError('source identity mismatch')
    return {'path':str(dest),**metadata}
print(json.dumps({'moby':prepare('moby_port_backend','moby-'+lock['moby_port_backend']['commit']+'.tar.gz','moby-port-pristine'),
                  'go':prepare('go_build',lock['go_build']['version']+'.linux-amd64.tar.gz','go-build-toolchain')}))
