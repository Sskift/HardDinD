from pathlib import Path
import hashlib, json, shutil, sys

w = Path('/root/harddind-rebuild')
b = w / 'cpu-clock-work-owner-v1'
prior = w / 'cpu-clock-owner-v1'
assert b.is_dir() and not (b / 'prepare.json').exists()
sys.dont_write_bytecode = True
sys.path.insert(0, str(b / 'repo/scripts'))
import harddind

state = json.loads((prior / 'owner-status.json').read_text())
assert state['status'] == 'finished' and state['groups_empty']
manifest = json.loads((prior / 'prepare.json').read_text())
for name in ['baseline-kernel', 'candidate-kernel', 'baseline.config', 'candidate.config']:
    assert harddind.sha(prior / name) == manifest['files'][name], name
    shutil.copy2(prior / name, b / name)
assert (b / 'baseline.config').read_bytes() == (b / 'candidate.config').read_bytes()
probe = w / 'cpu-work-count-owner-v1/cpu-limit-probe'
assert harddind.sha(probe) == 'f784e2de33fa44c775efa8f468c0e2c00bdbb5d99ec7ed4b97e321175882b4c4'
shutil.copy2(probe, b / 'cpu-limit-probe')
(b / 'clock-source-binding.json').write_text(json.dumps(manifest, indent=2) + '\n')

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

# Private experiment wrapper only: bound acquisition instead of hanging
# indefinitely if another scheduled job holds the shared global build lock.
p = b / 'repo/scripts/run-cpu-limits.py'
s = p.read_text()
needle = '            fcntl.flock(lock_f, fcntl.LOCK_EX)\n'
assert s.count(needle) == 1
s = s.replace(needle, '''            lock_deadline = time.monotonic() + 90
            while True:
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= lock_deadline:
                        raise TimeoutError('global build lock wait exceeded 90 seconds')
                    time.sleep(0.1)
''')
compile(s, str(p), 'exec')
p.write_text(s)
(b / 'run').mkdir()
for name in ['network-backend', 'client', 'kernel-build.lock']:
    (b / 'run' / name).symlink_to(w / name)
prepared = {'source_commit': (b / 'source-commit.txt').read_text().strip(),
            'clock_source_binding': str(prior / 'prepare.json'),
            'config_identical': True,
            'private_changes': ['allow only clock boot flag 0/1', 'bound build lock wait to 90 seconds'],
            'inputs': {str(p.relative_to(b)): harddind.sha(p) for p in b.rglob('*')
                       if p.is_file() and not p.is_symlink() and 'run' not in p.relative_to(b).parts},
            'external_inputs': {str(p): harddind.sha(p) for p in [w / 'runtime-shares-v1.ext4',
                                w / 'network-backend', w / 'client/docker',
                                w / 'client/ld-musl-x86_64.so.1']}}
(b / 'prepare.json').write_text(json.dumps(prepared, indent=2) + '\n')
print('prepared', b, len(prepared['inputs']))
