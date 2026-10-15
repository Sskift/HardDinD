import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location('workflow', Path(__file__).resolve().parents[1] / 'scripts/run-workflow-timing.py')
wf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wf)

class TransportTests(unittest.TestCase):
    def test_real_stdin_binary_and_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / 'cli.py'
            fake.write_text("import sys,hashlib,json; data=sys.stdin.buffer.read(); print(json.dumps({'args':sys.argv[1:],'sha':hashlib.sha256(data).hexdigest(),'bytes':len(data)}))")
            log = io.StringIO()
            client = wf.Client([sys.executable, str(fake), 'docker'], log, time.monotonic()+5, {'command': 3}, root)
            data = bytes(range(256))*4096
            obs = {'components': []}
            c = client.call(obs, 'import', ['import', '-', 'fixture'], data=data, need_output=True)
            out = json.loads(c['stdout'])
            self.assertEqual(out, {'args': ['docker', 'import', '-', 'fixture'], 'sha': wf.sha256_bytes(data), 'bytes': len(data)})
            self.assertIn('seconds=%.6f' % c['seconds'], log.getvalue())
            self.assertTrue(c['cleanup']['clean_shutdown'])
            self.assertEqual(wf.group_members(c['pid']), ([], []))
            raw = c['raw']['stdout']
            self.assertEqual(wf.sha256_file(Path(raw['path'])), raw['sha256'])
            self.assertEqual(Path(raw['path']).read_text(), c['stdout'])

    def test_partial_output_and_nonzero_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = wf.Client([sys.executable, '-c', "import sys;print('partial');print('failure',file=sys.stderr);sys.exit(7)"], io.StringIO(), time.monotonic()+5, {'command': 3}, root)
            obs = {'components': []}
            with self.assertRaises(wf.CommandError): client.call(obs, 'bad', [])
            c = obs['components'][0]
            self.assertEqual(c['exit_code'], 7)
            self.assertIn('partial', c['stdout'])
            self.assertIn('failure', c['stderr'])
            self.assertTrue(c['cleanup']['clean_shutdown'])
            self.assertFalse(wf.component_ok(c))

    def test_timeout_reaps_actual_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = wf.Client([sys.executable, '-c', "import time;print('before timeout',flush=True);time.sleep(60)"], io.StringIO(), time.monotonic()+3, {'command': .1}, root)
            obs = {'components': []}
            with self.assertRaises(wf.CommandError): client.call(obs, 'sleep', [])
            c = obs['components'][0]
            self.assertTrue(c['timed_out'])
            self.assertIn('before timeout', c['stdout'])
            self.assertEqual(wf.group_members(c['pid']), ([], []))
            self.assertFalse(wf.component_ok(c))

    def test_background_wait_records_launch_and_reap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = io.StringIO()
            client = wf.Client([sys.executable, '-c', "print('137')"], log, time.monotonic()+5, {'command': 3}, root)
            p, started = client.spawn([])
            rc, out, err, timeout = client.reap_spawned(p)
            c = client.component('wait', [], rc, out, err, started, timeout, seconds=p._harddind_seconds)
            self.assertEqual(rc, 0)
            self.assertEqual(out, b'137\n')
            self.assertFalse(timeout)
            self.assertTrue(p._harddind_cleanup['clean_shutdown'])
            self.assertGreater(c['seconds'], 0)
            self.assertIn('seconds=%.6f' % c['seconds'], log.getvalue())

    def test_build_content_compares_layers_and_runtime_config(self):
        image = {'Id': 'sha256:'+'1'*64, 'RootFS': {'Type':'layers','Layers':['sha256:'+'a'*64]},
                 'Config': {'Cmd':['cat','/wf/payload.sha']}, 'Os':'linux', 'Architecture':'amd64'}
        first = wf.build_image_content(json.dumps(image))
        image['Id'] = 'sha256:'+'2'*64
        image['Metadata'] = {'attestation':'new-build'}
        self.assertEqual(first, wf.build_image_content(json.dumps(image)))
        image['RootFS']['Layers'][0] = 'sha256:'+'b'*64
        self.assertNotEqual(first, wf.build_image_content(json.dumps(image)))
        image['RootFS']['Layers'][0] = 'sha256:'+'a'*64
        image['Config']['Cmd'] = ['true']
        self.assertNotEqual(first, wf.build_image_content(json.dumps(image)))
        del image['RootFS']
        with self.assertRaises(ValueError): wf.build_image_content(json.dumps(image))

    def test_real_network_missing_response(self):
        c = {'status':'ok','exit_code':1,'expect':[1], 'stdout':'[]\n',
             'stderr':'Error response from daemon: network harddind-wf-wf4-net-net0 not found\n'}
        self.assertTrue(wf.object_missing(c))
        c['stderr'] += 'exit status 1\n'
        self.assertTrue(wf.object_missing(c))
        c['stderr'] = 'Error response from daemon: network harddind-wf-wf4-net-net0 has active endpoints\n'
        self.assertFalse(wf.object_missing(c))

    def test_real_lowercase_removal_error(self):
        c = {'status': 'ok', 'exit_code': 1, 'expect': [1], 'stderr': 'error: no such object: fixture', 'stdout': '[]'}
        self.assertTrue(wf.object_missing(c))
        c['stderr'] = 'Cannot connect to the Docker daemon'
        self.assertFalse(wf.object_missing(c))
        c['stderr'] = 'No such object: fixture'
        c['timed_out'] = True
        self.assertFalse(wf.object_missing(c))

    def test_failed_command_cannot_pass_without_error_annotation(self):
        obs = {'rep':0, 'components':[], 'checks':[], 'cleanup_errors':[]}
        for name in wf.REQUIRED_CHECKS['volume']: obs['checks'].append({'name':name,'ok':True})
        for stage in wf.REQUIRED_STAGES['volume']:
            obs['components'].append({'stage':stage,'status':'ok','exit_code':0,'timed_out':False})
        obs['components'][0].update(status='fail',exit_code=2)
        self.assertEqual(wf.finalize_observation(obs,'volume')['status'],'fail')

if __name__ == '__main__': unittest.main()
