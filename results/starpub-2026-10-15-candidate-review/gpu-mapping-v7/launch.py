from pathlib import Path
import json,time,subprocess,hashlib
w=Path('/root/harddind-rebuild');a=w/'arkclaude/round5';d=a/'gpu-mapping-repair';b=w/'gpu-mapping-owner-v7'
for _ in range(60):
 if json.loads((a/'gpu-mapping-repair.resume9.status.json').read_text()).get('status')=='finished':break
 time.sleep(1)
else:raise RuntimeError('author did not stop')
assert not Path('/proc/80527').exists()
p=d/'.work/linux-7.2.10/arch/um/kernel/gpu.c';old=p.read_bytes();assert old==(w/'gpu-mapping-owner-v6/source/arch/um/kernel/gpu.c').read_bytes(),'author source changed; owner must review'
s=old.decode();needle="\tif (!gpu_ctl_flags_driver_checked(params.command) && params.flags)\n\t\treturn -EOPNOTSUPP;\n\tif (params.size != expected)\n\t\treturn -EOPNOTSUPP;"
assert s.count(needle)==1
s=s.replace(needle,"\t/* Fixed commands 0xd04/0x1f0 retain the driver's size and flag\n\t * validation order from 0025/0027; only fault in canonical bytes. */\n\tif (!gpu_ctl_flags_driver_checked(params.command) &&\n\t    (params.flags || params.size != expected))\n\t\treturn -EOPNOTSUPP;")
p.write_text(s)
(b/'owner-fix.json').write_text(json.dumps({'before_sha256':hashlib.sha256(old).hexdigest(),'after_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'reason':'restore driver-owned size validation for bounded fixed controls 0xd04/0x1f0'},indent=2)+'\n')
(d/'OWNER-V7-CONTROL-FIX.md').write_text('Owner applied only combined flags/size exception for 0xd04/0x1f0 after stopping resume9. Owner v7 rebuild/runtime active. Treat own kernel source/build read-only until this owner run finishes. Exact feature probe is /root/harddind-rebuild/gpu-mapping-owner-v6/run/results/mapping-features-v6/sources/gpu_system_features_probe.c, lines 45-62; no recursive /root searches needed. Audit remaining baseline control/alloc behavior read-only and document concrete differences. Do not rebuild or edit kernel while owner freezes.\n')
with (b/'prepare-supervisor.log').open('x') as out:
 q=subprocess.Popen(['python3',str(b/'prepare.py')],stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
(b/'prepare-launch.json').write_text(json.dumps({'pid':q.pid})+'\n')
