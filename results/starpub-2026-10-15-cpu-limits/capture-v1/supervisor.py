from pathlib import Path
import subprocess,json,datetime,os,fcntl,traceback
b=Path(__file__).resolve().parent;repo=b/'repo';work=b/'run';state={'status':'running','stage':'validation','supervisor_pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
def save():(b/'status.json').write_text(json.dumps(state,indent=2)+'\n')
save();rc=1
try:
 with (b/'validation.log').open('x') as log:
  def call(cmd,**kw):
   log.write(json.dumps(cmd)+'\n');log.flush();r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=60,**kw);log.write(r.stdout.decode(errors='replace')+f'\n[exit={r.returncode}]\n');log.flush();r.check_returncode();return r.stdout
  lock=(work/'kernel-build.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX)
  binary=b/'validated-probe'
  call(['gcc','-O2','-Wall','-Wextra','-Werror','-static','-pthread',str(repo/'src/cpu_capture_probe.c'),'-o',str(binary)])
  call([str(binary),'selftest'])
  fixture=b/'fixture';fixture.mkdir();(fixture/'max').write_text('50000 100000\n');(fixture/'stat').write_text('usage_usec 1000\nnr_periods 10\nnr_throttled 2\nthrottled_usec 500\n');(fixture/'proc').write_text('cpu 1 2 3 4 5 6 7 8 9 0\n')
  cmd=[str(binary),'capture-test','--max',str(fixture/'max'),'--stat',str(fixture/'stat'),'--proc',str(fixture/'proc'),'--count','3']
  rows=[json.loads(line) for line in call(cmd).decode().splitlines()];assert len(rows)==3
  for row in rows:
   assert row['max_stable']==1 and row['stat_errno']==0
   previous=row['wall_ns']
   assert [x['name'] for x in row['stages']]==['cpu.max.first','cpu.stat','cpu.max.second','/proc/stat','process_clock']
   for st in row['stages']:
    assert previous<=st['begin_ns']<=st['work_begin_ns']<=st['work_end_ns']<=st['end_ns']<=row['cap_end_ns'];assert st['thread_cpu_end_ns']>=st['thread_cpu_begin_ns'];previous=st['end_ns']
  (b/'fixture-results.json').write_text(json.dumps(rows,indent=2)+'\n')
  env=dict(os.environ,CPU_LIMIT_PROBE_BIN=str(binary));call(['python3','-m','unittest','discover','-s','tests'],cwd=repo,env=env)
  fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
 state['stage']='runtime';state['validation_exit_code']=0
 cmd=['python3',str(repo/'scripts/run-cpu-capture.py'),str(work),'--kernel','/root/harddind-rebuild/cpu-cpuset-kernel-v1','--disk','/root/harddind-rebuild/runtime-shares-v1.ext4','--name','cpu-capture-v1','--cpus','4'];state['command']=cmd
 with (b/'runtime.log').open('x') as log:
  p=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT);state['pid']=p.pid;save();rc=p.wait()
except BaseException:
 (b/'exception.log').write_text(traceback.format_exc())
state.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
