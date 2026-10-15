from pathlib import Path
import datetime, hashlib, json, shutil, subprocess, sys, tarfile

w = Path('/root/harddind-rebuild')
b = w / 'cpu-faultaround-owner-v3'
old = w / 'cpu-faultaround-owner-v2'
author = w / 'arkclaude/round17/anonymous-fault-performance/v2'

def sha(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for part in iter(lambda: f.read(1048576), b''):
            h.update(part)
    return h.hexdigest()

assert not b.exists(), 'Refusing existing evidence directory'
assert json.loads((old / 'owner-status.json').read_text())['status'] == 'finished'
manifest = json.loads((author / 'MANIFEST-v2.json').read_text())
previous = json.loads((old / 'kernel-binding.json').read_text())
assert sha(author / 'work/build/linux') == manifest['build']['built_kernel_sha256']
assert sha(author / 'work/build/.config') == manifest['config']['sha256']
assert sha(w / 'cache/linux-7.2.10.tar.xz') == previous['upstream_archive_sha256']
b.mkdir()
(b / 'prepare.py').write_bytes(Path(__file__).read_bytes())
shutil.copytree(old / 'repo', b / 'repo', ignore=shutil.ignore_patterns('__pycache__'))
for n in ['baseline-kernel', 'cpu-touch-probe', 'rootfs.tar']:
    shutil.copy2(old / n, b / n)
shutil.copy2(old / 'candidate-kernel', b / 'old-candidate-kernel')
shutil.copy2(author / 'work/build/linux', b / 'candidate-kernel')
shutil.copy2(author / 'work/build/.config', b / 'kernel.config')
shutil.copytree(author / 'patches', b / 'patches')
e = b / 'author-evidence'
e.mkdir()
for n in ['MANIFEST-v2.json', 'REPORT-v2.md']:
    shutil.copy2(author / n, e / n)
for n in ['build1.log', 'build2.log', 'build-retry-1.log', 'build-retry-2.log', 'do-build-v2.sh']:
    shutil.copy2(author / 'work' / n, e / n)
shutil.copytree(author / 'tests', e / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
shutil.copytree(author / 'results-host', e / 'results-host')
reapplied = b / 'reapplied'
for n, expected in previous['baseline_source_manifest'].items():
    p = w / 'gpu-completion-owner-v1/source' / n
    assert sha(p) == expected, n
    q = reapplied / n
    q.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, q)
with tarfile.open(w / 'cache/linux-7.2.10.tar.xz') as archive:
    data = archive.extractfile('linux-7.2.10/arch/um/kernel/trap.c').read()
(reapplied / 'arch/um/kernel/trap.c').write_bytes(data)
logs = []
for p in sorted((b / 'patches').glob('*.patch')):
    assert sha(p) == manifest['patches'][p.name]['sha256'], p.name
    done = subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(p)],
                          cwd=reapplied, text=True, capture_output=True, timeout=30)
    logs.append({'patch': p.name, 'exit_code': done.returncode,
                 'stdout': done.stdout, 'stderr': done.stderr})
    assert done.returncode == 0, logs[-1]
files = {str(p.relative_to(reapplied)): sha(p) for p in reapplied.rglob('*') if p.is_file()}
assert len(files) == 36, len(files)
for n, expected in files.items():
    source = author / 'work/linux' / n
    assert sha(source) == expected, n
    q = b / 'source' / n
    q.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, q)
for n, expected in manifest['post_apply_source_sha256'].items():
    assert files[n] == expected, n
binding = {
    'frozen_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'patch_reapplication_matches': True, 'config_identical': True,
    'baseline_kernel_sha256': sha(b / 'baseline-kernel'),
    'old_candidate_kernel_sha256': sha(b / 'old-candidate-kernel'),
    'candidate_kernel_sha256': sha(b / 'candidate-kernel'),
    'baseline_source_manifest': previous['baseline_source_manifest'],
    'reapplied_sha256': files,
    'upstream_archive_sha256': previous['upstream_archive_sha256'],
    'patches': {p.name: sha(p) for p in sorted((b / 'patches').glob('*.patch'))},
    'apply_logs': logs,
    'build_evidence': 'Author v2 full link; parent independently reapplied source, did not rebuild kernel.',
    'not_acceptance': ['No accepted performance tolerance', 'Full memory semantics pending',
                       'Microbenchmark, not complete Docker performance acceptance'],
    'original_probe_sha256': sha(b / 'cpu-touch-probe'),
    'original_rootfs_sha256': sha(b / 'rootfs.tar'),
}
assert binding['baseline_kernel_sha256'] == previous['baseline_kernel_sha256']
assert binding['old_candidate_kernel_sha256'] == previous['candidate_kernel_sha256']
assert binding['candidate_kernel_sha256'] == manifest['build']['built_kernel_sha256']
assert binding['original_probe_sha256'] == previous['original_probe_sha256']
assert binding['original_rootfs_sha256'] == previous['original_rootfs_sha256']
(b / 'kernel-binding.json').write_text(json.dumps(binding, indent=2) + '\n')
s = (old / 'supervisor.py').read_text()
before = "('candidate-off', b / 'candidate-kernel', '0'),"
assert s.count(before) == 1
s = s.replace(before, "('old-off', b / 'old-candidate-kernel', '0'),\n         " + before)
before = "('candidate-off-after', b / 'candidate-kernel', '0'),"
assert s.count(before) == 1
s = s.replace(before, before + "\n         ('old-off-after', b / 'old-candidate-kernel', '0'),")
s = s.replace('Candidate counter helpers still read clocks when counters are disabled; off controls retain this cost.',
              'Old candidate off still reads clocks; new candidate gates them off. Both measured in this run without instrumentation.')
s = s.replace('kernel is author build6, not an independent owner rebuild.',
              'kernels are author build6 and corrected v2, not independent owner rebuilds.')
before = "b / 'baseline-kernel', b / 'candidate-kernel', b / 'cpu-touch-probe',"
assert s.count(before) == 1
s = s.replace(before, "b / 'baseline-kernel', b / 'candidate-kernel', b / 'old-candidate-kernel', b / 'cpu-touch-probe',")
before = "for folder in ['repo', 'source', 'reapplied', 'author-evidence']"
assert s.count(before) == 1
s = s.replace(before, "for folder in ['repo', 'source', 'reapplied', 'author-evidence', 'patches']")
compile(s, str(b / 'supervisor.py'), 'exec')
(b / 'supervisor.py').write_text(s)
(b / 'run').mkdir()
for n in ['network-backend', 'client', 'kernel-build.lock']:
    (b / 'run' / n).symlink_to(w / n)
(b / 'prepare.json').write_text(json.dumps({'status': 'prepared_not_launched',
    'files': {str(p.relative_to(b)): sha(p) for p in b.rglob('*') if p.is_file()
              and not p.is_symlink() and 'run' not in p.relative_to(b).parts}}, indent=2) + '\n')
print('prepared', b, 'source files', len(files))
