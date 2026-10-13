import importlib.util
import pathlib
import unittest

path = pathlib.Path(__file__).resolve().parents[1] / "scripts/acceptance.py"
spec = importlib.util.spec_from_file_location("acceptance", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class AcceptanceTests(unittest.TestCase):
    def test_missing_results_do_not_pass(self):
        self.assertTrue(module.failures({}))

    def test_boot_and_outer_gpu_are_not_product_acceptance(self):
        self.assertTrue(module.failures({"kernel_boot": "pass", "native_gpu": "pass"}))

    def test_skip_cannot_replace_gpu(self):
        report = {"scope": "complete_docker_native_performance_gpu", "requirements": {
            name: {"status": "pass", "evidence": ["result.json"], "same_host_and_resources": True}
            for name in module.REQUIRED}}
        report["requirements"]["gpu_in_container"]["status"] = "skip"
        self.assertTrue(module.failures(report))

    def test_missing_comparable_baseline_does_not_pass(self):
        report = {"scope": "complete_docker_native_performance_gpu", "requirements": {
            name: {"status": "pass", "evidence": ["result.json"]} for name in module.REQUIRED}}
        self.assertIn("native_performance: comparable baseline missing", module.failures(report))

if __name__ == "__main__":
    unittest.main()
