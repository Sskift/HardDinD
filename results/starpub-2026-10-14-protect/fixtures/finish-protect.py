import importlib.util, json, pathlib, subprocess, time
w = pathlib.Path('/root/harddind-rebuild')
out = w / 'results'
spec = importlib.util.spec_from_file_location('cleanup', w / 'scripts/guest_cleanup.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
groups = []
for p in sorted(out.glob('*.json')):
    if not p.name.startswith(('protect-', 'docker-protect-')):
        continue
    d = json.loads(p.read_text())
    if 'cleanup' not in d:
        continue
    c = d['cleanup']
    live, zombies = m.group_members(c['process_group'])
    groups.append({'report': p.name, 'process_group': c['process_group'],
                   'historical_forced_cleanup': c['forced_cleanup'],
                   'historical_clean_shutdown': c['clean_shutdown'],
                   'current_live_pids': live, 'current_zombie_pids': zombies})
cmd = ['/root/udock/bin/docker-cli', '--host=tcp://127.0.0.1:23760', '--tlsverify',
       '--tlscacert=/root/udock/certs/ca.pem', '--tlscert=/root/udock/certs/cert.pem',
       '--tlskey=/root/udock/certs/key.pem', 'info',
       '--format', 'server={{.ServerVersion}} running={{.ContainersRunning}} images={{.Images}}']
health = subprocess.run(cmd, text=True, capture_output=True, timeout=30)
report = {'checked_at_unix': time.time(), 'groups': groups,
          'all_groups_empty': all(not r['current_live_pids'] and not r['current_zombie_pids'] for r in groups),
          'remaining_experiment_cows': sorted(str(p) for p in (w / 'tmp').glob('*.cow')),
          'old_service_health_exit_code': health.returncode,
          'old_service_health': health.stdout.strip(),
          'product_acceptance': False}
with (out / 'protect-final-checks.json').open('x') as f:
    json.dump(report, f, indent=2)
    f.write('\n')
print(json.dumps(report))
