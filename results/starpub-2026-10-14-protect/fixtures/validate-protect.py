import fcntl, gzip, hashlib, importlib.util, json, pathlib, shutil, subprocess, tempfile

w = pathlib.Path('/root/harddind-rebuild')
out = w / 'results'
lock = (w / 'kernel-build.lock').open('a')
fcntl.flock(lock, fcntl.LOCK_EX)
src = w / 'linux-7.2.10'
stamp = json.loads((src / '.harddind-patches.json').read_text())

def sha(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def matches(root, files):
    return all(sha(root / p) == h if h is not None else not (root / p).exists()
               for p, h in files.items())

assert matches(src, stamp['files'])
spec = importlib.util.spec_from_file_location('patches', w / 'scripts/apply-kernel-patches.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
patches = [w / 'kernel/patches/0001-um-bounded-seccomp-polling.patch']
for num in range(3, 11):
    found = list((w / 'kernel/experiments').glob(f'{num:04d}-*.patch'))
    assert len(found) == 1
    patches.extend(found)
old = json.loads((out / 'ipi-idle-provenance.json').read_text())
with tempfile.TemporaryDirectory(prefix='verify-protect-', dir=w) as d:
    staged = pathlib.Path(d) / src.name
    for name, h in stamp['files'].items():
        if h is None:
            continue
        p = staged / name
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / name, p)
    shutil.copyfile(src / '.harddind-patches.json', staged / '.harddind-patches.json')
    m.apply_series(staged, w / 'cache/linux-7.2.10.tar.xz', patches[:-1])
    assert matches(staged, old['source_files'])
    m.apply_series(staged, w / 'cache/linux-7.2.10.tar.xz', patches)
    assert matches(staged, stamp['files'])
names = ['protect-kernel-v1', 'protect-kernel-v2', 'protect-kernel-v3', 'ipi-kernel-idle',
         'thread-build/linux', 'thread-build/.config', 'mapping.ext4', 'protect-docker.ext4',
         'dispatch.ext4', 'profile.ext4', 'mapping-root/mapping-semantics',
         'protect-docker-root/mapping-semantics', 'dispatch-root/corebench',
         'dispatch-root/dispatchbench', 'profile-root/profilebench']
names += [str(p.relative_to(w)) for folder in ['scripts', 'src', 'tests']
          for p in (w / folder).iterdir() if p.is_file()]
names += ['Makefile']
files = {name: sha(w / name) for name in names}
assert files['protect-kernel-v3'] == files['thread-build/linux']
assert files['mapping-root/mapping-semantics'] == files['protect-docker-root/mapping-semantics']
checker = subprocess.run(['python3', str(w / 'scripts/check-stub-relocations.py'),
                          str(w / 'thread-build/arch/um/kernel/skas/stub.o')],
                         text=True, capture_output=True, check=True)
with (out / 'protect-v3-relocation-check.json').open('x') as f:
    f.write(checker.stdout)
shutil.copyfile(w / 'thread-build/.config', out / 'protect-v3-kernel.config')
report = {'kind': 'mprotect_build_provenance', 'source_matches_stamp': True,
          'roundtrip_0009_to_0010': True, 'baseline_matches_previous_provenance': True,
          'mapping_binary_identical_in_both_fixtures': True,
          'files': files, 'patches': stamp['patches'], 'source_files': stamp['files']}
with (out / 'protect-v3-provenance.json').open('x') as f:
    json.dump(report, f, indent=2)
    f.write('\n')
for trace in out.glob('protect-*.strace'):
    with trace.open('rb') as inp, trace.with_suffix('.strace.gz').open('xb') as dest:
        with gzip.GzipFile(filename='', mode='wb', fileobj=dest, mtime=0) as gz:
            shutil.copyfileobj(inp, gz)
print(json.dumps({'validated': True, 'kernel': files['protect-kernel-v3']}))
