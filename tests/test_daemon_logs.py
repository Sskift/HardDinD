"""Daemon logs survive guest shutdown, with bounded tails and per-boot names."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline import instance


class DaemonLogTests(unittest.TestCase):
    def test_tail_across_chunks(self):
        tail = instance.LogTail(8)
        source = b''
        for chunk in (b'', b'abc', b'defg', b'hijkl', b'0123456789', b'xy', b''):
            source += chunk
            tail.append(chunk)
            self.assertEqual(tail.data(), source[-8:])
            self.assertLessEqual(tail.size, 8)
            self.assertEqual(tail.total, len(source))
        with self.assertRaises(ValueError):
            instance.LogTail(0)

    def test_capture_bound_and_boot_names(self):
        limit = instance.DAEMON_LOG_LIMIT
        source = b'\nfirst real line\n' + b'x' * limit + b'last line\n'
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            boot = '123456abcdef'
            state = {'run': boot, 'mconsole': str(path / 'mconsole')}
            def reply(root, state, filename, receive):
                self.assertEqual((root, filename), (path, '/run/dockerd.log'))
                for offset in range(0, len(source), 511):
                    receive(source[offset:offset + 511])
                receive(b'')
            with patch.object(instance, 'read_guest_file', side_effect=reply):
                result = instance.capture_daemon_logs(path, state)
            self.assertEqual(result, {
                'source': '/run/dockerd.log', 'combined': ['dockerd', 'containerd'],
                'files': [f'dockerd-{boot}.log', f'containerd-{boot}.log'],
                'bytes_read': len(source), 'bytes_kept': limit, 'truncated': True})
            for name in result['files']:
                self.assertEqual((path / name).read_bytes(), source[-limit:])
            # A reboot must preserve the first boot's logs and a real leading newline.
            source = b'\nsecond boot\n'
            with patch.object(instance, 'read_guest_file', side_effect=reply):
                second = instance.capture_daemon_logs(path, {**state, 'run': 'abcdef123456'})
            self.assertFalse(second['truncated'])
            self.assertEqual(len(list(path.glob('*.log'))), 4)
            for name in second['files']:
                self.assertEqual((path / name).read_bytes(), source)
            with patch.object(instance, 'mconsole') as read:
                with self.assertRaisesRegex(ValueError, 'boot id'):
                    instance.capture_daemon_logs(path, {**state, 'run': '../bad'})
                read.assert_not_called()

    def test_archive_keeps_logs_not_guest_disk(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'instance'
            path.mkdir()
            names = ['guest-a.log', 'dockerd-a.log', 'containerd-a.log', 'state.json',
                     'shutdown-diagnostic-a.json', 'ports-a.jsonl']
            for name in [*names, 'root.cow', 'config.json']:
                (path / name).write_text(name)
            target = Path(td) / 'run' / 'instance-test'
            instance.archive_logs(path, target)
            self.assertEqual(sorted(p.name for p in target.iterdir()), sorted(names))
            self.assertEqual((target / 'dockerd-a.log').read_text(), 'dockerd-a.log')

    def test_mconsole_multipart_and_invalid_reply(self):
        def packet(data, more=0):
            return struct.pack('=III', 0, more, len(data) + 1) + data + b'\0'
        with tempfile.TemporaryDirectory() as td, patch.object(instance.socket, 'socket') as socket:
            path = Path(td)
            peer = socket.return_value.__enter__.return_value
            peer.recv.side_effect = [packet(b'\n', 1), packet(b'first', 1), packet(b'last', 1), packet(b'')]
            chunks = []
            instance.mconsole(path, path / 'mconsole', 'proc 1/root/run/dockerd.log', chunks.append)
            self.assertEqual(chunks, [b'\n', b'first', b'last', b''])
            self.assertEqual(peer.recv.call_count, 4)
            for reply in (b'bad', struct.pack('=III', 0, 0, 513), struct.pack('=III', 0, 0, 4) + b'x'):
                with self.subTest(reply=reply):
                    peer.recv.side_effect = [reply]
                    with self.assertRaisesRegex(RuntimeError, 'mconsole response'):
                        instance.mconsole(path, path / 'mconsole', 'cad')

    def test_proc_fixture_page_fragments_and_resume_on_error(self):
        # UML reads 4095 bytes, then emits <=511-byte NUL-terminated datagrams.
        source = b'\n' + (b'0123456789abcdef\n' * 800)
        packet = lambda data, more: struct.pack('=III', 0, more, len(data) + 1) + data + b'\0'
        frames = [packet(b'stopped', 0), packet(b'\n', 1)]
        for start in range(0, len(source), 4095):
            page = source[start:start + 4095]
            frames += [packet(page[i:i + 511], 1) for i in range(0, len(page), 511)]
        frames += [packet(b'', 0), packet(b'', 0)]
        with tempfile.TemporaryDirectory() as td, patch.object(instance.socket, 'socket') as socket:
            path = Path(td); state = {'mconsole': str(path / 'mconsole')}
            peer = socket.return_value.__enter__.return_value
            peer.recv.side_effect = frames
            chunks = []
            instance.read_guest_file(path, state, '/run/dockerd.log', chunks.append)
            self.assertEqual(b''.join(chunks), source)
            commands = [struct.unpack('=III512s', call.args[0])[3].rstrip(b'\0')
                        for call in peer.sendto.call_args_list]
            self.assertEqual(commands, [b'stop', b'proc 1/root/run/dockerd.log', b'go'])
            with patch.object(instance, 'mconsole', side_effect=[None, RuntimeError('read failed'), None]) as call:
                with self.assertRaisesRegex(RuntimeError, 'read failed'):
                    instance.read_guest_file(path, state, '/run/dockerd.log', chunks.append)
                self.assertEqual(call.call_args.args[2], 'go')


if __name__ == '__main__':
    unittest.main()
