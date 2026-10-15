import importlib.util
import json
import pathlib
import unittest

spec = importlib.util.spec_from_file_location("checks", pathlib.Path(__file__).resolve().parents[1] / "scripts/result_checks.py")
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class DockerEvidenceTests(unittest.TestCase):
    def test_mprotect_option_requires_actual_work(self):
        for log in ["", "HARDDIND_MPROTECT_SYNC enabled=1 pages=0\n",
                    "HARDDIND_MPROTECT_SYNC enabled=1 pages=10\n" * 2]:
            self.assertFalse(checks.mprotect_evidence(log)["verified"])
        self.assertTrue(checks.mprotect_evidence("HARDDIND_MPROTECT_SYNC enabled=1 pages=10\n")["verified"])

    def test_deferred_ipi_option_requires_actual_delivery(self):
        for log in ["", "HARDDIND_DEFER_IPI enabled=1 received=4 deferred=0 handled=4\n",
                    "HARDDIND_DEFER_IPI enabled=1 received=4 deferred=5 handled=4\n",
                    "HARDDIND_DEFER_IPI enabled=1 received=4 deferred=2 handled=5\n",
                    "HARDDIND_DEFER_IPI enabled=1 received=4 deferred=2 handled=3\n" * 2]:
            self.assertFalse(checks.deferred_ipi_evidence(log)["verified"])
        self.assertTrue(checks.deferred_ipi_evidence("HARDDIND_DEFER_IPI enabled=1 received=4 deferred=2 handled=3\n")["verified"])

    def test_missing_unused_or_duplicate_cache_is_not_verified(self):
        for log in ["", "HARDDIND_BROKER_CACHE hits=0 stores=2\n",
                    "HARDDIND_BROKER_CACHE hits=3 stores=2\n",
                    "HARDDIND_BROKER_CACHE hits=1 stores=2\n" * 2]:
            self.assertFalse(checks.broker_cache_evidence(log)["verified"])
        self.assertTrue(checks.broker_cache_evidence("HARDDIND_BROKER_CACHE hits=3 stores=4\n")["verified"])

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
