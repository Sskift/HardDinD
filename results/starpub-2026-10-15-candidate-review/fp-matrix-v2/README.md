# Preparation failed before native or guest execution

The first attempt to run the strengthened FP probe omitted the required
`work/probe-root` baseline. Strict static compilation succeeded, but disk
preparation exited 1 with that explicit missing-directory error. The
supervisor stopped before executing the native probe or any guest. Both
recorded command groups exited normally and were empty afterward.

The subsequent run is archived separately as `../fp-matrix-v3`; this
failed attempt has not been rewritten into a successful run.
