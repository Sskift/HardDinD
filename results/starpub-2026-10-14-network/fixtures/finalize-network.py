import fcntl, hashlib, importlib.util, json, pathlib, subprocess, time
w = pathlib.Path('/root/harddind-rebuild'); out = w/'results'
lock = (w/'kernel-build.lock').open('a'); fcntl.flock(lock, fcntl.LOCK_EX)
spec = importlib.util.spec_from_file_location('cleanup', w/'scripts/guest_cleanup.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024), b''): h.update(b)
    return h.hexdigest()
names=['ipi-kernel-idle','runtime.ext4','runtime-dns.ext4','network-backend','network-probe',
       'client/docker','client/buildx','client/ld-musl-x86_64.so.1','client/cli-plugins/docker-buildx',
       'guest/runtime-init','guest/runtime-daemon','scripts/harddind.py','scripts/run-runtime.py',
       'scripts/build-network.sh','scripts/make-runtime-root.sh','scripts/analyze-dns.py',
       'src/network_backend.c','src/network_probe.c','sources.lock.json']
files={name:sha(w/name) for name in names}
assert files['runtime-dns.ext4']==json.loads((out/'network-v3.json').read_text())['final_run']['config']['disk_sha256']
libraries={}
ldd=subprocess.check_output(['ldd',str(w/'network-backend')],text=True)
for line in ldd.splitlines():
    for word in line.split():
        if word.startswith('/') and pathlib.Path(word).is_file():libraries[word]=sha(pathlib.Path(word))
packages=subprocess.check_output(['dpkg-query','-W','-f=${Package}:${Architecture}=${Version}\n',
                                 'libslirp0','libslirp-dev','libglib2.0-0'],text=True)
expected=json.loads((w/'sources.lock.json').read_text())['network_backend']['starpub_packages']
actual=dict(line.split('=',1) for line in packages.splitlines())
assert actual==expected,(actual,expected)
groups=[]
for name in ['net-v1','network-v1','network-v2','network-v3']:
    for p in sorted((w/'instances'/name).glob('result-*.json')):
        d=json.loads(p.read_text())
        for key in ['guest_cleanup','network_cleanup']:
            c=d[key];live,zombies=m.group_members(c['process_group'])
            groups.append({'instance':name,'report':p.name,'kind':key,'pgid':c['process_group'],
                           'historical_clean_shutdown':c['clean_shutdown'],'live':live,'zombies':zombies})
r=json.loads((out/'network-v3.json').read_text())['native_cleanup']
live,zombies=m.group_members(r['process_group'])
groups.append({'instance':'native-echo','pgid':r['process_group'],'live':live,'zombies':zombies})
health=subprocess.run(['/root/udock/bin/docker-cli','--host=tcp://127.0.0.1:23760','--tlsverify',
    '--tlscacert=/root/udock/certs/ca.pem','--tlscert=/root/udock/certs/cert.pem','--tlskey=/root/udock/certs/key.pem',
    'info','--format','server={{.ServerVersion}} running={{.ContainersRunning}} images={{.Images}}'],
    text=True,capture_output=True,timeout=30)
report={'kind':'network_runtime_provenance','checked_at_unix':time.time(),'files':files,'libraries':libraries,
        'packages':actual,'outer_kernel':subprocess.check_output(['uname','-r'],text=True).strip(),
        'groups':groups,'all_groups_empty':all(not r['live'] and not r['zombies'] for r in groups),
        'old_service_health_exit_code':health.returncode,'old_service_health':health.stdout.strip(),
        'persistent_cows_retained':True,'full_acceptance':False}
with (out/'network-provenance.json').open('x') as f:json.dump(report,f,indent=2);f.write('\n')
print(json.dumps({'verified':True,'groups':len(groups),'all_empty':report['all_groups_empty'],
                  'old_service_health':report['old_service_health']}))
