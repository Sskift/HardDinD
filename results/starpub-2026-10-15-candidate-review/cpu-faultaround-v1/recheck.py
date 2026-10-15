#!/usr/bin/env python3
from pathlib import Path
import gzip,hashlib,json,statistics,subprocess,tempfile
ROOT=Path(__file__).resolve().parent

def read(name):
 p=ROOT/name
 return p.read_bytes() if p.exists() else gzip.decompress(p.with_name(p.name+'.gz').read_bytes())

def obj(name):return json.loads(read(name))

for name,digest in obj('archive-sha256.json').items():
 assert hashlib.sha256(read(name)).hexdigest()==digest,name
r=obj('owner-status.json');binding=obj('kernel-binding.json');audit=obj('post-run-audit.json')
assert r['status']=='finished'and r['exit_code']==0
assert not any(r[k]for k in ['full_acceptance','performance_acceptance','semantic_acceptance'])
assert r['groups_empty']and r['inputs_unchanged']and r['sources_unchanged']
assert len(r['commands'])==34 and len(r['groups'])==46
assert all(x==[[],[]]for x in r['groups'].values())
for c in r['commands']:
 assert c['exit_code']==0 and not c.get('timeout')and c['cleanup']['clean_shutdown'],c['label']
for name,digest in r['sources'].items():assert hashlib.sha256(read(name)).hexdigest()==digest,name
assert binding['config_identical']and binding['patch_reapplication_matches']
assert read('baseline.config')==read('candidate.config')
for name,digest in binding['baseline_source_manifest'].items():assert hashlib.sha256(read('source/'+name)).hexdigest()==digest,name
for name,digest in binding['reapplied_sha256'].items():assert hashlib.sha256(read('reapplied/'+name)).hexdigest()==digest,name
with tempfile.TemporaryDirectory()as temp:
 temp=Path(temp)
 for p in (ROOT/'source').rglob('*'):
  if p.is_file():q=temp/p.relative_to(ROOT/'source');q.parent.mkdir(parents=True,exist_ok=True);q.write_bytes(p.read_bytes())
 for name,digest in binding['patches'].items():
  data=read('author-evidence/'+name);assert hashlib.sha256(data).hexdigest()==digest
  result=subprocess.run(['patch','--batch','--fuzz=0','-p1','-d',str(temp)],input=data,capture_output=True,timeout=30)
  assert result.returncode==0,result.stderr
 for name,digest in binding['reapplied_sha256'].items():assert hashlib.sha256((temp/name).read_bytes()).hexdigest()==digest,name
fields=['cold_ns','warm_ns','cold_minor','warm_minor','cold_major','warm_major']
labels=['native-before','baseline','candidate-off','candidate-on','candidate-off-after','native-after']
assert list(r['cells'])==labels
computed={};count=0
for label in labels:
 cell=r['cells'][label];assert cell['completed']and len(cell['samples'])==3
 allrows=[]
 for i,rows in enumerate(cell['samples']):
  raw=[json.loads(line)for line in read(f'{label}-{i}.stdout').splitlines()]
  assert raw==rows and not read(f'{label}-{i}.stderr')
  assert [(x['bytes'],x['iteration'])for x in rows]==[(s,j)for s in [1048576,4194304,16777216]for j in range(8)]
  assert all(x['verified']is True and x['page_size']==4096 and x['pages']*4096==x['bytes'] and all(type(x[f])is int and x[f]>=0 for f in fields)for x in rows)
  allrows.extend(rows);count+=len(rows)
 if not label.startswith('native-'):
  st=r['states']['fa-'+label];assert cell['normal_shutdown']and st['phase']=='stopped'and st['guest_exit_code']==0 and not st['forced_shutdown']
  cmd=cell['ready_state']['command'];assert 'ncpus=4'in cmd and 'gpu_async_wait=1'in cmd
  order=None if label=='baseline'else '4'if label=='candidate-on'else'0'
  assert [x for x in cmd if x.startswith('uml_faultaround=')]==([]if order is None else['uml_faultaround='+order])
  assert [x for x in cmd if x.startswith('uml_faultstat=')]==([]if order is None else['uml_faultstat=0'])
 computed[label]={str(s):{f:statistics.median(x[f]for x in allrows if x['bytes']==s)for f in fields}for s in [1048576,4194304,16777216]}
assert computed==r['summary']and count==432
assert audit['execution_pass']and not audit['benchmark_valid']
assert audit['commands_with_possible_overlap']and audit['wrong_build_remaining_groups_empty']
assert audit['wrong_build_forced_cleanup']
assert 'exec 9>/kernel-build.lock' in read('interfering-build.sh').decode()
overlap=obj('possible-build-overlap.json');assert overlap['cleanup']['process_group']==18151
assert not overlap['cleanup']['remaining_live_pids']and not overlap['cleanup']['remaining_zombie_pids']
print('PASS: 432 raw records, exact patch/source/config binding, 34 commands and 46 empty owner groups.')
print('Timing inference REJECTED: a separately recorded wrong-lock build overlapped the run and required forced cleanup.')
print('No Docker/GPU/semantic/performance acceptance.')
