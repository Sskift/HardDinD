"""HD_NET_NATIVE=1 ./hd check builds and exercises both Ethernet framings."""
import os
import subprocess
import sys
import unittest

from pipeline import build, config


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('HD_NET_NATIVE') == '1',
                     'requires Linux and HD_NET_NATIVE=1')
class BackendNativeTests(unittest.TestCase):
    def test_static_backend_and_both_framings(self):
        with build.lock('measure', shared=True):
            binary = build.network() / 'network-backend'
            headers = subprocess.check_output(['readelf', '-l', binary], text=True)
            self.assertNotIn('INTERP', headers, 'release backend must be statically linked')
            for vnet in ('0', '1'):
                with self.subTest(vnet=vnet):
                    env = {**os.environ, 'HARDDIND_NETWORK_BACKEND': str(binary),
                           'HARDDIND_TEST_VNET': vnet}
                    result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-v',
                                             '-s', str(config.REPO / 'tests/publish')],
                                            env=env, capture_output=True, text=True, timeout=900)
                    print(result.stdout + result.stderr, flush=True)
                    self.assertEqual(result.returncode, 0)
                    self.assertNotIn('skipped=', result.stderr, 'missing native checks are not a pass')
