import importlib.util
import pathlib
import unittest

spec = importlib.util.spec_from_file_location('port_collisions', pathlib.Path(__file__).parent / 'publish/port_collisions.py')
ports = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ports)


class PortCollisionTests(unittest.TestCase):
    def test_next_ephemeral_port_reservations_and_wrap(self):
        self.assertEqual(ports.next_port(30000, '30000 30005', '30001-30003,30005'), 30004)
        self.assertEqual(ports.next_port(30005, '30000 30005', ''), 30000)
        with self.assertRaises(ValueError):
            ports.next_port(30005, '30000 30005', '30000-30005')

    def test_collision_requires_matching_failed_driver_request(self):
        row = {'request': {'method': 'POST', 'body': {'parentIP': '127.0.0.1', 'parentPort': 30000, 'proto': 'tcp4'}},
               'response': {'status': 500, 'body': {'message': 'host port unavailable: Address already in use'}}}
        self.assertTrue(ports.collision([row], '127.0.0.1', 30000, 'tcp'))
        self.assertFalse(ports.collision([row], '127.0.0.2', 30000, 'tcp'))
        self.assertFalse(ports.collision([row], '127.0.0.1', 30001, 'tcp'))
        self.assertFalse(ports.collision([row], '127.0.0.1', 30000, 'udp'))
        row['response']['status'] = 201
        self.assertFalse(ports.collision([row], '127.0.0.1', 30000, 'tcp'))
