# Parent further test review
While finalizing, read these alongside previous FINAL-REVIEW, which has not yet been addressed in current tests file:
- Current CHECK failure output invalid JSON: e.g. {"check_fail":"..."{"case":...}} or comma then bare object. Use one valid JSON format, escaping, and deliberate failing fixture parsed by json.loads.
- Existing fork/pipe/pthread error paths still use uninitialized resources/waitpid(-1); must track successes.
- PROT_NONE second write follows PROT_READ SIGSEGV whose handler exits42; NONE is never tested. Separate children.
- Current userfaultfd custom msg struct wrong size/layout vs kernel struct uffd_msg; use actual header struct. m[0]=0x11 then checking ==0 is wrong (write resumes). Handler exit4/timeout leaves parent blocked in m[0] forever; bounded watchdog/parent orchestration needed. Need negative fixtures for API/poll/read failure; do not call hang PASS.
- memcg_enforced accepts any signal/nonzero (includingSIGSEGV/ordinarytesterror). Require expected SIGKILL + memory.events oom_kill delta and recorded limit/current; allocation failure is not OOM-kill proof. cgroup cleanup checked, no false success from staleexistingdir.
- Current build5 failed missing __uml_setup declaration. Final manifest remainsold; do not state BUILD_OK until final rebuild and exact patch/source/config/kernel binding.
Parent will only run frozen coherent candidate; no benchmark threshold accepted.
