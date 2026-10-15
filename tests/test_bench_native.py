"""Opt-in Linux native smoke, via HD_BENCH_NATIVE=1 ./hd check (never boots UML)."""
import json
import gzip
import hashlib
import io
import os
import pathlib
import sys
import tempfile
import subprocess
import tarfile
import types
import unittest

from pipeline import bench, build, config


@unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('HD_BENCH_C_SYNTAX') == '1',
                     'requires macOS and HD_BENCH_C_SYNTAX=1')
class CrossSyntaxTests(unittest.TestCase):
    def test_linux_c_with_pinned_musl_headers(self):
        pin = build.LOCK['bench_apk_musl_dev']
        apk = config.REPO / '.work/bench-inputs' / pin['url'].rsplit('/', 1)[-1]
        data = apk.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), pin['sha256'])
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), ignore_zeros=True) as archive:
                for member in archive:
                    path = pathlib.PurePosixPath(member.name)
                    if path.parts[:2] == ('usr', 'include') and '..' not in path.parts and member.isfile():
                        dest = root / path; dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(archive.extractfile(member).read())
            for source in sorted((config.REPO / 'bench').glob('*.c')):
                with self.subTest(source=source.name):
                    subprocess.run(['clang', '--target=x86_64-linux-musl', '-nostdinc', '-isystem', str(root / 'usr/include'),
                                    '-O2', '-Wall', '-Wextra', '-Werror', '-fsyntax-only', str(source)], check=True)


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('HD_BENCH_NATIVE') == '1',
                     'requires Linux and HD_BENCH_NATIVE=1')
class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifact = bench.bench_root()
        config.RUNS.mkdir(parents=True, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix='native-check-', dir=config.RUNS)
        cls.ctx = types.SimpleNamespace(run_id='native-check', run_dir=pathlib.Path(cls.tmp.name),
                                        bench_cpus=config.BENCH_CPUS, quick=True, log=print)
        cls.runner = bench.Workloads(cls.ctx)
        build.run(['cp', '-a', cls.artifact / 'root', cls.runner.root])
        cls.ctx.share = cls.ctx.run_dir / 'share'; cls.ctx.share.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.runner.close(); cls.tmp.cleanup()

    def test_native_workloads(self):
        samples = {}
        for name in bench.METRICS:
            if name.startswith(('docker.', 'net.')):
                continue
            with self.subTest(metric=name):
                directory = self.ctx.share if name in bench.BIND_METRICS else None
                samples[name] = bench.parse_sample(self.runner.native('/bench/workload', name, '1', workdir=directory))
        print('native samples: ' + json.dumps(samples, sort_keys=True), flush=True)
        self.assertEqual(len(samples), 21)

    def test_native_network(self):
        with self.runner.native_server() as port:
            for mode in ('stream', 'rr'):
                with self.subTest(mode=mode):
                    value = bench.parse_sample(self.runner.native('/bench/network', 'client', '127.0.0.1', str(port), mode, '0.2'))
                    print(f'native network {mode}: {value}', flush=True)
                    self.assertGreater(value, 0)


if __name__ == '__main__':
    unittest.main()
