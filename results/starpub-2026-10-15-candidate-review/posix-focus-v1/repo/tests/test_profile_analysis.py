import importlib.util
import pathlib
import unittest

spec = importlib.util.spec_from_file_location("profile_analysis", pathlib.Path(__file__).resolve().parents[1] / "scripts/analyze-profile.py")
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


class TraceTests(unittest.TestCase):
    begin = '10 12:00:00.000000 write(1, "PROFILE_BEGIN case=getpid repeat=0 iterations=1\\n", 1) = 1\n'
    end = '10 12:00:00.000010 write(1, "PROFILE_END {\\"case\\":\\"getpid\\",\\"repeat\\":0,\\"iterations\\":1,\\"verified\\":true}\\n", 1) = 1\n'

    def test_trapped_calls_resumed_calls_and_console_duplicates(self):
        lines = [self.begin,
                 self.begin.replace('\\n"', '"'),
                 '10 12:00:00.000002 getpid() = 39\n',
                 '10 12:00:00.000003 --- SIGSYS {si_syscall=__NR_getpid} ---\n',
                 '10 12:00:00.000004 futex(0x123, FUTEX_WAIT, 2, NULL <unfinished ...>\n',
                 '10 12:00:00.000005 <... futex resumed>) = 0\n',
                 '10 12:00:00.000006 rt_sigreturn({mask=[]}) = 15\n',
                 self.end, self.end.replace('\\n"', '"')]
        rows = analysis.parse_trace(lines)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trapped_guest_syscalls"], {"getpid": 1})
        self.assertNotIn("getpid", rows[0]["host_syscalls"])
        self.assertEqual(rows[0]["host_syscalls"]["futex"], 1)
        self.assertEqual(rows[0]["signals"], {"SIGSYS": 1})

    def test_incomplete_or_mismatched_phases_are_rejected(self):
        for lines in ([self.begin], [self.end], [self.begin, self.begin],
                      [self.begin, self.end.replace('getpid', 'fork')],
                      [self.begin, self.end.replace('true', 'false')]):
            with self.assertRaises(ValueError):
                analysis.parse_trace(lines)
