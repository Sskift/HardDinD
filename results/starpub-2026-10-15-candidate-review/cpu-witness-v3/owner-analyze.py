from pathlib import Path
import datetime,fcntl,hashlib,importlib.util,json,os,statistics,time,traceback
b=Path(__file__).resolve().parent;runtime=b/'run/results/witness-v3'
r={'status':'running','started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'cells':{},'full_acceptance':False,'performance_acceptance':False}
def save():(b/'owner-analysis.json').write_text(json.dumps(r,indent=2)+'\n')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def desc(values):
 ordered=sorted(values)
 return {'count':len(values),'median_ms':statistics.median(values)/1e6,'p95_ms':ordered[int((len(ordered)-1)*.95)]/1e6,'max_ms':max(values)/1e6}
save()
try:
 assert json.loads((b/'owner-status.json').read_text())['exit_code']==0
 os.sched_setaffinity(0,{0,1,2,3})
 with (b.parent/'kernel-build.lock').open('a') as lock:
  deadline=time.monotonic()+1800
  while True:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
   except BlockingIOError:
    if time.monotonic()>deadline:raise RuntimeError('lock deadline')
    time.sleep(1)
  spec=importlib.util.spec_from_file_location('correlate',b/'diag/host_correlate.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  report=json.loads((runtime/'report.json').read_text())
  for label in ['quiet-quota','quiet-unlimited','separate-observer','same-observer']:
   d=runtime/label
   witness=[json.loads(l) for l in (d/'witness.stdout').read_text().splitlines()]
   summary=[json.loads(l) for l in (d/'witness.stderr').read_text().splitlines()]
   samples=[json.loads(l) for l in (d/'host/samples.jsonl').read_text().splitlines()]
   manifest=json.loads((d/'host/manifest.json').read_text())
   old=json.loads((d/'corr/correlation.json').read_text());new=m.analyze_capture(manifest,samples,witness,summary[0],60)
   assert old==new,'raw correlation replay differs '+label
   caps=[x for x in witness if x['event']=='cap'];assert len(caps)==60
   times=[x['stages'][-1]['end_ns']-x['mono_begin_ns'] for x in caps]
   gaps=[v['mono_begin_ns']-u['mono_begin_ns'] for u,v in zip(caps,caps[1:])]
   worst=max(caps,key=lambda x:x['stages'][-1]['end_ns']-x['mono_begin_ns'])
   top=max(old['stages'],key=lambda x:x['guest_wall_ns'])
   r['cells'][label]={'raw_correlation_replay_identical':True,'source_sha256':sha(b/'diag/host_correlate.py'),'capture':desc(times),'inter_capture':desc(gaps),'coverage':old['coverage_counts'],'alignment_width_ns':old['clock_alignment']['width_ns'],'worst_capture':worst,'longest_stage':top,'witness_summary':summary[0],'sampler':report['cells'][label]['sampler']}
   save()
 r.update(status='finished',exit_code=0)
except BaseException:r.update(status='failed',exit_code=1,error=traceback.format_exc())
r['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
