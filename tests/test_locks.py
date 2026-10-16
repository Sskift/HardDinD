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
    from pipeline import locks, build
    import contextlib
    mode = sys.argv[2]
    with (contextlib.nullcontext() if mode == 'build' else locks.measure(shared=mode == 'shared')):
        if mode in ('build', 'nested'):
            build.artifact('test', 'key', lambda tmp, final: None)
        print('got', sys.argv[2], time.time(), flush=True)
        time.sleep(float(sys.argv[3]))
''')


class MeasureLockTests(unittest.TestCase):
    def test_build_waits_for_measurement_and_nested_build_does_not_deadlock(self):
        with tempfile.TemporaryDirectory() as root:
            env = {**os.environ, 'HD_ROOT': root}
            def spawn(mode, hold):
                return subprocess.Popen([sys.executable, '-c', HOLD, str(REPO), mode, str(hold)], env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            writer = spawn('exclusive', .5)
            first = float(writer.stdout.readline().split()[2])
            builder = spawn('build', 0)
            try:
                second = float(builder.communicate(timeout=5)[0].split()[2])
                self.assertGreater(second - first, .4)
                writer.communicate(timeout=5)
                nested = spawn('nested', 0)
                try:
                    self.assertIn('got nested', nested.communicate(timeout=5)[0])
                    self.assertEqual(nested.returncode, 0)
                finally:
                    if nested.poll() is None:
                        nested.kill(); nested.communicate()
            finally:
                for child in (writer, builder):
                    if child.poll() is None:
                        child.kill(); child.communicate()

    def test_writer_is_not_starved_by_later_readers(self):
        with tempfile.TemporaryDirectory() as root:
            env = {**os.environ, 'HD_ROOT': root}
            spawn = lambda mode, hold: subprocess.Popen([sys.executable, '-c', HOLD, str(REPO), mode, str(hold)],
                                                        env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            first = spawn("shared", 2.0)
            time.sleep(0.7)
            writer = spawn('exclusive', 0.1)
            time.sleep(0.7)
            late = spawn('shared', 0.1)
            got = {name: float(p.communicate(timeout=10)[0].split()[2]) for name, p in
                   (('first', first), ('writer', writer), ('late', late))}
            self.assertLess(got['first'], got['writer'])
            self.assertLess(got['writer'], got['late'])


if __name__ == '__main__':
    unittest.main()
