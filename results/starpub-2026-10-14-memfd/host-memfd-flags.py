import os,errno,json,platform
r={'kernel':platform.release(),'tests':{}}
for name,flags in [('explicit_exec',os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING|0x10),('legacy_exec',os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)]:
 try:
  fd=os.memfd_create('harddind-flag-check',flags);os.close(fd);r['tests'][name]={'created':True}
 except OSError as e:r['tests'][name]={'created':False,'errno':e.errno,'message':str(e)}
r['old_flag_fallback_confirmed']=r['tests']['explicit_exec'].get('errno')==errno.EINVAL and r['tests']['legacy_exec']['created']
print(json.dumps(r,indent=2))
