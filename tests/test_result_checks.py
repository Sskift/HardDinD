import importlib.util
import json
import pathlib
import unittest

spec = importlib.util.spec_from_file_location("checks", pathlib.Path(__file__).resolve().parents[1] / "scripts/result_checks.py")
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class DockerEvidenceTests(unittest.TestCase):
    def test_partial_success_followed_by_aborted_suite_is_not_pass(self):
        log = 'HARDDIND_RESULT {"name":"run","status":"pass"}\nHARDDIND_DOCKER_END status=1\n'
        result = checks.docker_evidence(log, 0, False)
        self.assertTrue(result["finished"])
        self.assertFalse(result["checks_complete"])
        self.assertFalse(result["subset_passed"])

    def test_duplicate_cannot_replace_missing_gpu_evidence(self):
        names = checks.DOCKER_CHECKS - {"gpu_compute"}
        log = "".join("HARDDIND_RESULT " + json.dumps({"name": n, "status": "pass"}) + "\n" for n in sorted(names))
        log += 'HARDDIND_RESULT {"name":"run","status":"pass"}\nHARDDIND_DOCKER_END status=0\n'
        result = checks.docker_evidence(log, 0, False)
        self.assertFalse(result["checks_complete"])
        self.assertFalse(result["subset_passed"])
