from pathlib import Path
import json,sys,subprocess,fcntl,re,hashlib,datetime,importlib.util,traceback,os
b=Path(__file__).resolve().parent;w=b.parent;snap=w/'gpu-mapping-owner-review-v3'
sys.path.insert(0,str(w/'cpu-sampler-owner-main-v5/repo/scripts'));from guest_cleanup import cleanup_group
spec=importlib.util.spec_from_file_location('extract',b/'extractor.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
body=m.extract_body((snap/'source/arch/um/kernel/skas/stub_exe.c').read_text()).replace('(init_data.stub_start)','(STUB_PAGE_ADDR)').replace('init_data.gpu_bridge','GPU_BRIDGE_CONST').replace('init_data.gpu_async','GPU_ASYNC_CONST')
assert body==(snap/'filter-build/seccomp_filter_body.inc').read_text()
(b/'seccomp_filter_body.inc').write_text(body);(b/'seccomp_filter_prelude.inc').write_text(m.HEADER)
report={'status':'running','supervisor_pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'extracted BPF reachability only; not GPU or guest acceptance','rows':[]}
def save():(b/'report.json').write_text(json.dumps(report,indent=2)+'\n')
save();rc=1
try:
 with (w/'kernel-build.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  for bridge in (0,1):
   for async_ in (0,1):
    for model in ('sim','real'):
     name=f'{model}-b{bridge}-a{async_}';binary=b/name
     cmd=['gcc','-std=gnu11','-Wall','-Wextra','-Werror','-O2',f'-DGPU_BRIDGE={bridge}',f'-DGPU_ASYNC={async_}','-I',str(b),'-o',str(binary),str(b/(model+'-model.c'))]
     if model=='real':cmd.insert(1,'-DFILTER_RUNTIME_PAGE=1')
     build=subprocess.run(cmd,capture_output=True,timeout=30);(b/(name+'.build.stdout')).write_bytes(build.stdout);(b/(name+'.build.stderr')).write_bytes(build.stderr);build.check_returncode()
     with (b/(name+'.stdout')).open('x') as out,(b/(name+'.stderr')).open('x') as err:
      p=subprocess.Popen([str(binary)],stdout=out,stderr=err,start_new_session=True);timed_out=False
      try:code=p.wait(timeout=20)
      except subprocess.TimeoutExpired:timed_out=True;code=None
      cleanup=cleanup_group(p.pid)
     row={'name':name,'build_command':cmd,'build_exit':build.returncode,'pid':p.pid,'exit_code':code,'timed_out':timed_out,'cleanup':cleanup,'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest()};report['rows'].append(row);save();assert code==0 and not timed_out and cleanup['clean_shutdown']
  rc=0
except BaseException:report['error']=traceback.format_exc()
report.update(status='finished',exit_code=rc,finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat());report['source_sha256']={str(p.relative_to(b)):hashlib.sha256(p.read_bytes()).hexdigest() for p in b.glob('*') if p.suffix in ('.c','.py','.inc')};save()
