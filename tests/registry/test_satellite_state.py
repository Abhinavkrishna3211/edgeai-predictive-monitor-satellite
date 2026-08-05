#!/usr/bin/env python3
"""
test_satellite_state.py — Unit tests for gateway/registry/satellite_state.py.

SatelliteState.__init__() and _sat_register() both do a lazy `import
recv_verify` inside the function body (see satellite_state.py's module
docstring), so exercising _sat_register() here requires recv_verify itself
to be importable. recv_verify.py has no import-time side effects beyond
optional-dependency probing (guarded by try/except ImportError) and a
`if __name__ == '__main__': main()` guard, so importing it is safe in a
test process — confirmed via a standalone smoke import before writing this
file.

Run with:
    python -m pytest tests/registry/test_satellite_state.py -v
    python tests/registry/test_satellite_state.py
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, 'mic_tools'))

import matplotlib
matplotlib.use('Agg')  # headless — recv_verify imports matplotlib.pyplot at module level

import recv_verify as rv
from gateway.registry.satellite_state import _satellites, _sat_lock


class TestSingleSourceOfTruth(unittest.TestCase):
    """recv_verify and satellite_state must share the identical registry objects."""

    def test_satellites_dict_is_shared_object(self):
        self.assertIs(rv._satellites, _satellites)

    def test_sat_lock_is_shared_object(self):
        self.assertIs(rv._sat_lock, _sat_lock)


class TestRegisterDisconnectCount(unittest.TestCase):
    def setUp(self):
        self._mac = 'AA:BB:CC:DD:EE:F0'
        _satellites.pop(self._mac, None)

    def tearDown(self):
        _satellites.pop(self._mac, None)

    def test_register_adds_connected_satellite(self):
        sat = rv._sat_register(self._mac, 'UNITTEST-SAT', 1, 0, ('127.0.0.1', 5100))
        self.assertIn(self._mac, _satellites)
        self.assertTrue(sat.connected)
        self.assertEqual(sat.name, 'UNITTEST-SAT')
        self.assertEqual(sat.alert, rv.EPM_ALERT_OK)

    def test_register_twice_reuses_same_state_object(self):
        sat1 = rv._sat_register(self._mac, 'UNITTEST-SAT', 1, 0, ('127.0.0.1', 5100))
        sat2 = rv._sat_register(self._mac, 'UNITTEST-SAT', 1, 1, ('127.0.0.1', 5101))
        self.assertIs(sat1, sat2)
        self.assertEqual(sat2.fw_minor, 1)

    def test_disconnect_marks_not_connected_but_keeps_entry(self):
        rv._sat_register(self._mac, 'UNITTEST-SAT', 1, 0, ('127.0.0.1', 5100))
        rv._sat_disconnect(self._mac)
        self.assertIn(self._mac, _satellites)
        self.assertFalse(_satellites[self._mac].connected)

    def test_sat_count_reflects_connected_only(self):
        before = rv._sat_count()
        rv._sat_register(self._mac, 'UNITTEST-SAT', 1, 0, ('127.0.0.1', 5100))
        self.assertEqual(rv._sat_count(), before + 1)
        rv._sat_disconnect(self._mac)
        self.assertEqual(rv._sat_count(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
