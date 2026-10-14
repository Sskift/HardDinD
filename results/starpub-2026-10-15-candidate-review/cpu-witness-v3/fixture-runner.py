from pathlib import Path
import importlib.util,sys,unittest
b=Path(__file__).resolve().parent;suite=unittest.TestSuite()
for name in ['test_host_correlate_v3','test_run_cpu_witness']:
 spec=importlib.util.spec_from_file_location(name,b/'diag/tests'/(name+'.py'));m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m)
 if name=='test_run_cpu_witness':m.WITNESS_BIN=b/'fixture-witness'
 suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(m))
r=unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if r.wasSuccessful() and r.testsRun==28 and not r.skipped else 1)
