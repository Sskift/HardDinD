# Guest-stack diagnostic: setup failure before guest launch

This failed attempt is retained. The unchanged lifecycle probe passes its full
native calibration, and the read-only `/proc` observer compiles. The new isolated
work directory omitted the required `network-backend`, `client` and global lock
symlinks. Instance startup therefore exits 1 before launching a guest; its log
reports the missing `run/network-backend` file. No internal GPU workload or
thread stack sampling occurs, and no native-after control runs.

The unconditional failed-start cleanup also tries `docker ps`, fails with
`instance is not ready`, and adds a second exception. Both errors are preserved
in the owner status. All ten recorded command process groups are empty, inputs
are unchanged, and final VM state is empty. Normal guest shutdown is false
because no guest was launched; this is not a successful runtime test.

The successor v5 fixes the isolated-directory setup, checks that the diagnostic
helper emits valid JSON before use, corrects the container-name filter, and only
queries/removes a container after successful instance startup. It uses a new
directory and does not overwrite this failed run.

`python3 recheck.py` verifies hashes and the preserved failure boundaries.
