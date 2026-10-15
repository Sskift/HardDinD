from pathlib import Path
import ast, copy, datetime, hashlib, importlib.util, io, json, shutil, statistics, subprocess, tarfile, tempfile

w = Path('/root/harddind-rebuild')
b = w / 'cpu-clock-owner-v1'
a = w / 'arkclaude/round12/cpu-capture-stall'
v = a / 'clock-v5'
old = w / 'cpu-witness-owner-v3'
prior = json.loads((w / 'cpu-faultaround-owner-v3/owner-status.json').read_text())
assert prior['status'] == 'finished' and prior['groups_empty']
assert not b.exists(), 'Refusing existing output'

def sha(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for part in iter(lambda: f.read(1048576), b''):
            h.update(part)
    return h.hexdigest()

assert sha(v / 'obj-smp/linux') == '5b3d3c507a00cd6c56dfb0dfec06f07b62633c4e9a487e5905ab960a105c10b0'
assert sha(w / 'cpu-cpuset-kernel-v1') == 'fb5e56e48240f96a997aa0f1d234992f128d23dd428eb8256dfccccc4d217fca'
assert (v / 'obj-smp/.config').read_bytes() == (w / 'cpu-cpuset-owner-v1/kernel.config').read_bytes()
b.mkdir()
for n, p in [('supervisor.py', w / 'cpu-clock-owner-v1-supervisor.py'),
             ('prepare.py', Path(__file__)), ('baseline-kernel', w / 'cpu-cpuset-kernel-v1'),
             ('candidate-kernel', v / 'obj-smp/linux'), ('baseline.config', w / 'cpu-cpuset-owner-v1/kernel.config'),
             ('candidate.config', v / 'obj-smp/.config'),
             ('cpu-capture-witness', old / 'run/results/witness-v3/cpu-capture-witness'),
             ('cpu_capture_witness.c', old / 'diag/cpu_capture_witness.c'),
             ('host_correlate.py', old / 'diag/host_correlate.py')]:
    shutil.copy2(p, b / n)
assert sha(b / 'cpu-capture-witness') == '375b3d3bc6d37b9d33a48f2a6d528021cd12cde5bd5a949a555d9e15e656b666'
shutil.copytree(old / 'repo', b / 'repo', ignore=shutil.ignore_patterns('__pycache__'))
p = b / 'repo/scripts/harddind.py'
s = p.read_text()
needle = '        if a.gpu_control_experiment:\n'
assert s.count(needle) == 1
s = s.replace(needle, "        hr_clock = os.environ.get('HARDDIND_HR_CLOCK')\n"
              "        if hr_clock is not None:\n"
              "            if hr_clock not in ('0', '1'): raise ValueError('bad HARDDIND_HR_CLOCK')\n"
              "            cmd.append('harddind_hr_sched_clock=' + hr_clock)\n" + needle)
compile(s, str(p), 'exec')
p.write_text(s)
baseline = json.loads((w / 'cpu-cpuset-owner-v1/kernel-patches.json').read_text())
(b / 'baseline-patches.json').write_text(json.dumps(baseline, indent=2) + '\n')
for n, h in baseline['files'].items():
    p = w / 'linux-7.2.10' / n
    assert sha(p) == h, n
    q = b / 'source' / n
    q.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, q)
archive = w / 'cache/linux-7.2.10.tar.xz'
assert sha(archive) == '2feaa70453e91f541be6a5c9078d8168e081af45a3d9718054850538cff2ebf3'
with tarfile.open(archive) as inp:
    data = inp.extractfile('linux-7.2.10/kernel/sched/fair.c').read()
p = b / 'source/kernel/sched/fair.c'
p.parent.mkdir(parents=True)
p.write_bytes(data)
shutil.copytree(b / 'source', b / 'reapplied')
shutil.copytree(a / 'analysis/kernel-candidate-v5', b / 'author-evidence')
shutil.copy2(a / 'REPORT-CLOCK-v5.md', b / 'author-evidence/REPORT-CLOCK-v5.md')
for n in ['link.log', 'link.rc', 'cfg.log', 'cfg.rc']:
    if (v / 'obj-smp' / n).exists():
        shutil.copy2(v / 'obj-smp' / n, b / 'author-evidence' / n)
apply = []
for p in sorted((b / 'author-evidence').glob('*.patch')):
    r = subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(p)],
                       cwd=b / 'reapplied', capture_output=True, text=True, timeout=30)
    apply.append({'patch': p.name, 'sha256': sha(p), 'exit_code': r.returncode,
                  'stdout': r.stdout, 'stderr': r.stderr})
    assert r.returncode == 0, apply[-1]
reapplied = {str(p.relative_to(b / 'reapplied')): sha(p) for p in (b / 'reapplied').rglob('*') if p.is_file()}
assert len(reapplied) == len(baseline['files']) + 2
for n, h in reapplied.items():
    assert sha(v / 'src' / n) == h, n
with tarfile.open(b / 'rootfs.tar', 'w') as out:
    for n in ['dev', 'proc', 'sys', 'tmp']:
        t = tarfile.TarInfo(n);t.type = tarfile.DIRTYPE;t.mode = 0o755;out.addfile(t)
    data = (b / 'cpu-capture-witness').read_bytes()
    t = tarfile.TarInfo('cpu-capture-witness');t.mode = 0o755;t.size = len(data)
    out.addfile(t, io.BytesIO(data))
# Reuse the actual analysis function on historical raw evidence and failures.
tree = ast.parse((b / 'supervisor.py').read_text())
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'analyze')
spec = importlib.util.spec_from_file_location('clock_witness_check', b / 'host_correlate.py')
checker = importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)
raw = (old / 'run/results/witness-v3/quiet-quota/witness.stdout').read_text()
err = (old / 'run/results/witness-v3/quiet-quota/witness.stderr').read_text()
tests = []
with tempfile.TemporaryDirectory(dir=b) as tmp:
    tmp = Path(tmp)
    env = {'b': tmp, 'json': json, 'statistics': statistics, 'checker': checker}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(b / 'supervisor.py'), 'exec'), env)
    (tmp / 'case.stdout').write_text(raw);(tmp / 'case.stderr').write_text(err)
    env['analyze']('case', True);tests.append('historical_quota_accept')
    cases = []
    bad = json.loads(err);bad['status'] = 'deadline';cases.append((raw, json.dumps(bad), 'deadline'))
    rows = [json.loads(x) for x in raw.splitlines()]
    cases.append(('\n'.join(json.dumps(x) for x in rows[:-2]), err, 'missing'))
    for kind in ['quota', 'errno', 'stage_order']:
        bad = copy.deepcopy(rows);cap = next(x for x in bad if x['event'] == 'cap')
        if kind == 'quota':cap['max1']['raw'] = 'max 100000'
        if kind == 'errno':cap['stat']['errno'] = 5
        if kind == 'stage_order':cap['stages'][1]['begin_ns'] = cap['stages'][0]['begin_ns'] - 1
        cases.append(('\n'.join(json.dumps(x) for x in bad), err, kind))
    for output, error, label in cases:
        (tmp / 'case.stdout').write_text(output);(tmp / 'case.stderr').write_text(error)
        try:env['analyze']('case', True)
        except AssertionError:tests.append(label + '_reject')
        else:raise AssertionError('accepted ' + label)
(b / 'parser-fixtures.json').write_text(json.dumps({'pass': tests, 'fail': [],
    'supervisor_sha256': sha(b / 'supervisor.py'), 'historical_stdout_sha256': hashlib.sha256(raw.encode()).hexdigest(),
    'historical_stderr_sha256': hashlib.sha256(err.encode()).hexdigest()}, indent=2) + '\n')
(b / 'run').mkdir()
for n in ['network-backend', 'client', 'kernel-build.lock']:(b / 'run' / n).symlink_to(w / n)
prepared = {'status': 'prepared_not_launched', 'source_reapplication_matches': True,
            'config_identical': True, 'apply_logs': apply, 'reapplied_sha256': reapplied,
            'files': {str(p.relative_to(b)): sha(p) for p in b.rglob('*') if p.is_file()
                      and not p.is_symlink() and 'run' not in p.relative_to(b).parts and '__pycache__' not in p.parts}}
(b / 'prepare.json').write_text(json.dumps(prepared, indent=2) + '\n')
print('prepared', b, 'source files', len(reapplied), 'parser checks', len(tests))
