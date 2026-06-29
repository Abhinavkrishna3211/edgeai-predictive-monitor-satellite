#!/usr/bin/env python3
"""
test_storage.py — Unit tests for the Storage class.

Tests:
  1.  WAL journal mode is active after construction
  2.  log_alert + recent_alerts round-trip (with satellite filter)
  3.  recent_alerts with no filter returns all events
  4.  recent_alerts ordered newest-first
  5.  recent_alerts satellite filter
  6.  log_maintenance + get_latest_maintenance round-trip
  7.  get_latest_maintenance returns most-recent record
  8.  get_latest_maintenance returns None for unknown satellite
  9.  get_all_maintenance returns one entry per satellite
  10. save_model_state + load_model_state round-trip
  11. load_model_state returns None for missing key (not raises)
  12. save_model_state overwrites existing row (UPSERT)
  13. Different components per satellite are independent
  14. Concurrent writes from 2 threads do not corrupt the DB
  15. upsert_satellite is idempotent
  16. rotate_old_csvs gzips files older than max_age_days
  17. rotate_old_csvs leaves new files untouched
"""

import gzip
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from storage import Storage, rotate_old_csvs


class StorageTestCase(unittest.TestCase):
    """Base class that creates a fresh Storage + temp dir for each test."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db = Storage(os.path.join(self._tmpdir, 'test.db'))

    def tearDown(self):
        self.db.close()           # release WAL lock before rmtree on Windows
        shutil.rmtree(self._tmpdir, ignore_errors=True)


# ── WAL ───────────────────────────────────────────────────────────────────────

class TestWAL(StorageTestCase):
    def test_wal_mode_enabled(self):
        mode = self.db.conn.execute('PRAGMA journal_mode').fetchone()[0]
        self.assertEqual(mode, 'wal',
                         f'Expected WAL journal mode, got {mode!r}')


# ── Alert events ──────────────────────────────────────────────────────────────

class TestAlertEvents(StorageTestCase):
    def test_log_and_retrieve_alert(self):
        self.db.log_alert('SAT-01', 'OK', 'WARN', 0.75, 'K=7.2 CF=4.1 z=4.5')
        self.db.log_alert('SAT-01', 'WARN', 'FAULT', 0.97, 'K=13.1 CF=9.8 z=7.2')
        rows = self.db.recent_alerts('SAT-01', limit=10)
        self.assertEqual(len(rows), 2)

    def test_recent_alerts_ordered_newest_first(self):
        self.db.log_alert('SAT-A', 'OK', 'WARN', 0.71, 'first')
        time.sleep(0.01)
        self.db.log_alert('SAT-A', 'WARN', 'FAULT', 0.96, 'second')
        rows = self.db.recent_alerts('SAT-A', limit=10)
        self.assertEqual(rows[0][4], 'FAULT',
                         'Most recent transition (FAULT) must appear first')

    def test_recent_alerts_no_filter_returns_all(self):
        self.db.log_alert('SAT-A', 'OK', 'WARN', 0.7, '')
        self.db.log_alert('SAT-B', 'OK', 'FAULT', 0.95, '')
        rows = self.db.recent_alerts(limit=50)
        self.assertEqual(len(rows), 2)

    def test_recent_alerts_satellite_filter(self):
        self.db.log_alert('SAT-A', 'OK', 'WARN',  0.7,  '')
        self.db.log_alert('SAT-B', 'OK', 'FAULT', 0.95, '')
        rows = self.db.recent_alerts('SAT-A', limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 'SAT-A')


# ── Maintenance log ───────────────────────────────────────────────────────────

class TestMaintenance(StorageTestCase):
    def test_log_and_retrieve_maintenance(self):
        self.db.log_maintenance('AA:BB:CC:DD:EE:FF', 'Alice', 'bearing_replace',
                                '{"last_date":"2026-06-01","notes":"SKF 6205 replaced"}')
        rec = self.db.get_latest_maintenance('AA:BB:CC:DD:EE:FF')
        self.assertIsNotNone(rec)

    def test_get_latest_returns_most_recent(self):
        self.db.log_maintenance('00:11:22:33:44:55', 'Bob',   'inspection',
                                '{"notes":"ok"}')
        time.sleep(0.01)
        self.db.log_maintenance('00:11:22:33:44:55', 'Carol', 'bearing_replace',
                                '{"notes":"replaced"}')
        rec = self.db.get_latest_maintenance('00:11:22:33:44:55')
        self.assertIn('replaced', str(rec),
                      'get_latest_maintenance must return the most-recent record')

    def test_get_latest_returns_none_for_unknown(self):
        result = self.db.get_latest_maintenance('FF:EE:DD:CC:BB:AA')
        self.assertIsNone(result)

    def test_get_all_maintenance(self):
        self.db.log_maintenance('MAC-1', 'Alice', 'inspection',  '{"notes":"mac1"}')
        self.db.log_maintenance('MAC-2', 'Bob',   'lubrication', '{"notes":"mac2"}')
        all_m = self.db.get_all_maintenance()
        self.assertIn('MAC-1', all_m)
        self.assertIn('MAC-2', all_m)
        self.assertEqual(len(all_m), 2)


# ── Model state ───────────────────────────────────────────────────────────────

class TestModelState(StorageTestCase):
    def test_save_and_load_round_trip(self):
        state = {'mean': 3.14159, 'var': 0.25, 'n_updates': 1000}
        self.db.save_model_state('SAT-01', 'baselines', state)
        loaded = self.db.load_model_state('SAT-01', 'baselines')
        self.assertEqual(loaded, state)

    def test_load_missing_returns_none(self):
        result = self.db.load_model_state('DOES-NOT-EXIST', 'baselines')
        self.assertIsNone(result,
                          'load_model_state must return None for missing key, not raise')

    def test_save_overwrites_existing(self):
        self.db.save_model_state('SAT-X', 'rul', {'n_updates': 10})
        self.db.save_model_state('SAT-X', 'rul', {'n_updates': 20})
        loaded = self.db.load_model_state('SAT-X', 'rul')
        self.assertEqual(loaded['n_updates'], 20,
                         'Second save must overwrite the first (UPSERT)')

    def test_different_components_are_independent(self):
        self.db.save_model_state('SAT-Y', 'baselines', {'key': 'baselines-val'})
        self.db.save_model_state('SAT-Y', 'rul',       {'key': 'rul-val'})
        self.assertEqual(self.db.load_model_state('SAT-Y', 'baselines')['key'],
                         'baselines-val')
        self.assertEqual(self.db.load_model_state('SAT-Y', 'rul')['key'],
                         'rul-val')


# ── Concurrent writes ─────────────────────────────────────────────────────────

class TestConcurrentWrites(StorageTestCase):
    def test_concurrent_writes_no_corruption(self):
        """Two threads writing 50 alerts each must all land in the DB."""
        errors = []

        def writer(sat_name: str):
            for i in range(50):
                try:
                    self.db.log_alert(sat_name, 'OK', 'WARN',
                                      0.5 + i * 0.001, f'event {i}')
                except Exception as e:
                    errors.append(str(e))

        t1 = threading.Thread(target=writer, args=('THREAD-A',))
        t2 = threading.Thread(target=writer, args=('THREAD-B',))
        t1.start(); t2.start()
        t1.join();  t2.join()

        self.assertEqual(errors, [], f'Thread errors: {errors}')
        rows = self.db.recent_alerts(limit=200)
        self.assertEqual(len(rows), 100,
                         f'Expected 100 rows (50 per thread), got {len(rows)}')


# ── Satellite registry ────────────────────────────────────────────────────────

class TestUpsertSatellite(StorageTestCase):
    def test_upsert_is_idempotent(self):
        self.db.upsert_satellite('SAT-Z', 'AA:BB:CC:DD:EE:01')
        self.db.upsert_satellite('SAT-Z', 'AA:BB:CC:DD:EE:01')   # must not fail
        count = self.db.conn.execute(
            "SELECT COUNT(*) FROM satellites WHERE name='SAT-Z'"
        ).fetchone()[0]
        self.assertEqual(count, 1, 'Upsert should produce exactly one row')


# ── CSV rotation ──────────────────────────────────────────────────────────────

class TestCSVRotation(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_old_csv_is_gzipped(self):
        old_path = os.path.join(self._tmpdir, 'epm_SAT_20240101.csv')
        with open(old_path, 'w') as f:
            f.write('wall_time,kurtosis\n1.0,3.2\n')
        # Backdate the file's mtime to 91 days ago
        old_mtime = time.time() - 91 * 86400
        os.utime(old_path, (old_mtime, old_mtime))

        count = rotate_old_csvs(self._tmpdir, max_age_days=90)
        self.assertEqual(count, 1)
        self.assertFalse(os.path.exists(old_path), 'Original CSV must be removed')
        self.assertTrue(os.path.exists(old_path + '.gz'), 'Gzipped file must exist')
        with gzip.open(old_path + '.gz', 'rb') as gz:
            content = gz.read().decode()
        self.assertIn('kurtosis', content, 'Gzipped content must match original')

    def test_new_csv_is_not_touched(self):
        new_path = os.path.join(self._tmpdir, 'epm_SAT_today.csv')
        with open(new_path, 'w') as f:
            f.write('wall_time,kurtosis\n')
        count = rotate_old_csvs(self._tmpdir, max_age_days=90)
        self.assertEqual(count, 0)
        self.assertTrue(os.path.exists(new_path),
                        'Recent CSV must NOT be compressed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
