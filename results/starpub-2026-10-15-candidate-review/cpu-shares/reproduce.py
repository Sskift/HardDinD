import importlib.util,json,pathlib
b=pathlib.Path(__file__).resolve().parent
s=importlib.util.spec_from_file_location('fixture',b/'tests/test_cpu_share_checks.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
rows=[]
for label,change in [('baseline',lambda e:None),('missing_required_ledger',lambda e:e.update(required_commands=[],commands=[])),('missing_input_provenance',lambda e:e.update(hashes_before={},hashes_after={})),('missing_midrun_cpuset',lambda e:[s['cgroup'].pop('cpuset.cpus.effective',None) for c in e['containers'].values() for s in c['snapshots']]),('missing_leftovers_inventory',lambda e:e.pop('leftovers_stdout',None))]:
 e=m.build_evidence();change(e);c=m.checks.evaluate(e);rows.append({'case':label,'verdict':m.checks.overall_verdict(c),'checks':{k:v['status'] for k,v in c.items()}})
print(json.dumps(rows,indent=2))
assert all(row['verdict']=='pass' for row in rows), 'frozen false-pass reproduction changed'
