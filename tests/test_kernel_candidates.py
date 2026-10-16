"""Check the real series and optional patches when the pinned source is cached."""
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from pipeline import build, config, runs

DEPENDENCIES = {'core-syscall-profile': ['fault-rendezvous-counters'],
                'core-syscall-hot': ['core-syscall-patch'], 'share-coherent': ['share-dir-cache']}
NEEDS_REBASE = {'core-syscall-patch', 'core-syscall-hot', 'core-syscall-profile',
                'fault-profile', 'fault-rendezvous-counters', 'share-close', 'share-poll'}


class KernelCandidateTests(unittest.TestCase):
    def test_share_worker_default_and_override(self):
        artifacts = dict.fromkeys(('kernel', 'rootfs', 'network_backend', 'client'), 'unused')
        for opts, expected in [((), 0), (['thread_pool_size=4'], 4)]:
            result = runs.instance_options(artifacts, shares=['share'], share_opts=opts)
            self.assertEqual(result['share_options']['thread_pool_size'], expected)
        self.assertIsNone(runs.instance_options(artifacts)['share_options'])

    def test_cached_kernel_series_and_candidates_apply_without_fuzz(self):
        spec = build.LOCK['linux']
        prefix = 'linux-' + spec['version'] + '/'
        archive = config.WORK / 'cache' / (prefix[:-1] + '.tar.xz')
        if not archive.exists():
            self.skipTest('pinned kernel archive not cached; run ./hd check on build host')
        self.assertEqual(build.file_sha(archive), spec['sha256'])
        candidates = {p.stem: p for p in (config.REPO / 'kernel/candidates').glob('*.patch')}
        defaults = build.series()
        paths = {line[6:].split('\t', 1)[0] for p in [*defaults, *candidates.values()]
                 for line in p.read_text().splitlines() if line.startswith(('--- a/', '+++ b/'))}
        def apply(root, patch):
            return subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1', '-d', str(root)],
                                  input=patch.read_bytes(), capture_output=True)
        with tempfile.TemporaryDirectory(prefix='.patch-check-', dir=config.REPO) as directory:
            base = pathlib.Path(directory) / 'base'; base.mkdir()
            with tarfile.open(archive, mode='r|*') as tar:
                for member in tar:
                    if not member.name.startswith(prefix) or member.name[len(prefix):] not in paths:
                        continue
                    self.assertTrue(member.isfile())
                    target = base / member.name[len(prefix):]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(tar.extractfile(member).read())
            for patch in defaults:
                result = apply(base, patch)
                self.assertEqual(result.returncode, 0, patch.name + '\n' + result.stdout.decode())
            for name, patch in sorted(candidates.items()):
                with self.subTest(candidate=name):
                    target = pathlib.Path(directory) / name
                    shutil.copytree(base, target)
                    chain = [candidates[n] for n in DEPENDENCIES.get(name, [])] + [patch]
                    for item in chain:
                        result = apply(target, item)
                        if result.returncode:
                            break
                    if name in NEEDS_REBASE:
                        self.assertNotEqual(result.returncode, 0, name + ' now applies; update rebase list')
                        readme = (config.REPO / 'kernel/candidates/README.md').read_text()
                        self.assertIn('`' + name + '`', readme.split('<!-- needs-rebase -->')[1])
                    else:
                        self.assertEqual(result.returncode, 0, item.name + '\n' + result.stdout.decode())
