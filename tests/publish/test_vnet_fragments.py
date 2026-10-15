"""The existing IPv6 fragment contract also applies to the vnet link."""
import os
from unittest import mock

import test_ipv6_fragmentation as fragments


class VnetMode:
    def setUp(self):
        env = mock.patch.dict(os.environ, {'HARDDIND_TEST_VNET': '1'})
        env.start()
        self.addCleanup(env.stop)
        super().setUp()


class VnetFragmentedFullPath(VnetMode, fragments.FragmentedFullPath):
    pass


class VnetReassembly(VnetMode, fragments.RawReassembly):
    pass
