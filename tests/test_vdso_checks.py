import json
import pathlib
import tempfile
import unittest
from unittest import mock

from pipeline import functional, probe
from suites.functional import clocks


class ClockResultTests(unittest.TestCase):
    def test_probe_instances_do_not_share_dirty_revision_suffix(self):
        a = probe.instance_path('20261015-120000-abcdef12-dirty-probe', 'A')
        b = probe.instance_path('20261015-121000-abcdef12-dirty-probe', 'A')
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, probe.instance_path('20261015-120000-abcdef12-dirty-probe', 'B'))
        self.assertLessEqual(len(a.name), 30)

    def test_missing_failed_or_skipped_checker_never_passes(self):
        with tempfile.TemporaryDirectory() as root:
            case = mock.Mock(root=pathlib.Path(root))
            case.object.return_value = 'check:local'
            with mock.patch.object(clocks.probe, 'program'), mock.patch.object(clocks.probe, 'image_tar', return_value=b'tar'):
                for row in ({}, {'check': 'summary', 'pass': 2, 'fail': 1},
                            {'check': 'summary', 'pass': 0, 'fail': 0, 'status': 'pass'}):
                    case.output.return_value = json.dumps(row)
                    with self.assertRaises(AssertionError):
                        clocks.run(case, 'clock_ordinary')
                case.output.return_value = json.dumps({'check': 'summary', 'pass': 2, 'fail': 0,
                                                       'not_tested': 1, 'status': 'not_tested'})
                with self.assertRaises(functional.NotTested):
                    clocks.run(case, 'clock_privileged')
                case.output.return_value = json.dumps({'check': 'summary', 'pass': 2, 'fail': 0,
                                                       'not_tested': 0, 'status': 'pass'})
                clocks.run(case, 'clock_ordinary')
                self.assertEqual(case.output.call_args.kwargs['opts'], ())
                clocks.run(case, 'clock_privileged')
                self.assertEqual(case.output.call_args.kwargs['opts'], ('--privileged',))
