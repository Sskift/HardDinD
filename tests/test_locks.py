import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
HOLD = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from pipeline import locks
    with locks.measure(shared=sys.argv[2] == 'shared'):
        print('got', sys.argv[2], time.monotonic(), flush=True)
        time.sleep(float(sys.argv[3]))
''')


class MeasureLockTests(unittest.TestCase):
    def test_writer_is_not_starved_by_later_readers(self):
        with tempfile.TemporaryDirectory() as root:
            env = {**os.environ, 'HD_ROOT': root}
            spawn = lambda mode, hold: subprocess.Popen([sys.executable, '-c', HOLD, str(REPO), mode, str(hold)],
                                                        env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            first = spawn('shared', 0.6)
            time.sleep(0.2)
            writer = spawn('exclusive', 0.1)
            time.sleep(0.2)
            late = spawn('shared', 0.1)
            got = {name: float(p.communicate(timeout=10)[0].split()[2]) for name, p in
                   (('first', first), ('writer', writer), ('late', late))}
            self.assertLess(got['first'], got['writer'])
            self.assertLess(got['writer'], got['late'])


if __name__ == '__main__':
    unittest.main()
