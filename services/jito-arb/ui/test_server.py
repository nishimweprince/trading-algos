import importlib.util
import pathlib
import tempfile
import time
import tracemalloc
import unittest

spec = importlib.util.spec_from_file_location('jito_ui', pathlib.Path(__file__).with_name('server.py'))
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


def record(kind=1, action=0, reason=18, outcome=0, signature=1, wall=None):
    v = list(ui.REC.unpack(bytes(ui.REC.size)))
    v[0:4] = [kind, 2, action, reason]
    v[5] = outcome
    v[6] = bytes([0, 1, 0])
    v[10] = wall if wall is not None else int(time.time() * 1000)
    v[11] = 100
    v[12] = 1000
    v[16:21] = [1000, 100, 50, 850, 85]
    v[24:27] = [30000, 50000, 0]
    v[-1] = bytes([signature]) * 64
    return ui.REC.pack(*v)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / 'decisions').mkdir()
        self.file = self.root / 'decisions/decisions-test.bin'
        self.file.write_bytes(ui.MAGIC)

    def tearDown(self):
        ui._DECISION_CACHES.pop(str(self.root), None)
        self.temp.cleanup()

    def append(self, data):
        with self.file.open('ab') as f:
            f.write(data)

    def read(self, hours=24):
        return ui.read_decisions(str(self.root), hours)

    def day(self, result):
        return next(iter(result['by_day'].values()))

    def test_incremental_signature_join_and_no_duplicate_counts(self):
        self.append(record(action=2))
        result = self.read()
        self.assertEqual(self.day(result)['sim_unsimulated'], 1)
        cache = ui._DECISION_CACHES[str(self.root)]
        before = cache.bytes_read
        self.assertEqual(self.read(), result)
        self.assertEqual(cache.bytes_read, before)
        self.append(record(kind=2, action=2, outcome=5, reason=0))
        result = self.read()
        self.assertEqual(result['records'], 1)
        self.assertEqual(self.day(result)['sim_ok'], 1)
        self.assertEqual(self.day(result)['sim_profitable'], 1)
        self.assertEqual(self.day(result)['sim_unsimulated'], 0)
        self.assertEqual(result['latest'][0]['sim_leg_units'], [30000, 50000, 0])

    def test_repeated_signature_preserves_each_signed_event(self):
        self.append(record(action=2) * 2)
        self.append(record(kind=2, action=2, outcome=5, reason=0))
        result = self.read()
        self.assertEqual(result['records'], 2)
        self.assertEqual(self.day(result)['dispatched'], 2)
        self.assertEqual(self.day(result)['sim_ok'], 2)
        self.assertEqual(self.day(result)['sim_net_lamports'], 1700)

    def test_guard_reverts_count_zero_not_losses(self):
        def outcome(signature, net, outcome=5, reason=0, sim_slot=100):
            v = list(ui.REC.unpack(record(kind=2, action=2, outcome=outcome, reason=reason, signature=signature)))
            v[19] = net
            v[11] = sim_slot
            v[27] = 100  # candidate slot
            return ui.REC.pack(*v)
        for sig in (1, 2, 3, 4):
            self.append(record(action=2, signature=sig))
        self.append(outcome(1, 50_000))                    # passes the guard: lands
        self.append(outcome(2, -900_000))                  # short: the guard reverts it at no cost
        self.append(outcome(3, 0, outcome=4, reason=2))    # failed at leg 2: reverts
        self.append(outcome(4, 20_000, sim_slot=150))      # lands, but simulated 50 slots late
        result = self.read()
        day = self.day(result)
        self.assertEqual(day['sim_land'], 2)
        self.assertEqual(day['sim_revert'], 2)
        self.assertEqual(day['sim_landed_net_lamports'], 70_000)
        self.assertEqual(day['sim_land_late'], 1)
        self.assertEqual(day['sim_landed_net_late_lamports'], 20_000)
        self.assertEqual(day['sim_net_lamports'], -830_000)  # unguarded reference keeps the loss
        rows = {r['sim']: r for r in result['latest'] if r['action'] == 'dry_run'}
        self.assertEqual(rows['ok: would revert']['sim_net_sol'], 0.0)
        self.assertAlmostEqual(rows['ok: would revert']['sim_unguarded_net_sol'], -0.0009)

    def test_setup_errors_stay_separate_from_route_failures(self):
        for signature, reason in [(1, 0x10), (2, 2)]:
            self.append(record(action=2, signature=signature))
            self.append(record(kind=2, action=2, outcome=4, reason=reason, signature=signature))
        day = self.day(self.read())
        self.assertEqual(day['sim_unsimulated'], 1)
        self.assertEqual(day['sim_failed'], 1)

    def test_partial_tail_retried_and_truncate_resets(self):
        data = record()
        self.append(data[:100])
        self.assertEqual(self.read()['records'], 0)
        self.append(data[100:])
        self.assertEqual(self.read()['records'], 1)
        self.file.write_bytes(ui.MAGIC + record(action=2))
        # Same-size replacement can preserve inode, so replace as reset-stats does.
        replacement = self.file.with_suffix('.new')
        replacement.write_bytes(ui.MAGIC + record(action=2))
        replacement.replace(self.file)
        result = self.read()
        self.assertEqual(result['records'], 1)
        self.assertEqual(self.day(result)['dispatched'], 1)

    def test_time_window_excludes_old_seconds(self):
        self.append(record(wall=int((time.time() - 7200) * 1000)))
        self.append(record(action=2))
        self.assertEqual(self.read(1)['records'], 1)
        self.assertEqual(self.read(24)['records'], 2)

    def test_large_skip_file_keeps_bounded_rows_and_low_peak_memory(self):
        self.append(record() * 50000)
        tracemalloc.start()
        result = self.read()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        cache = ui._DECISION_CACHES[str(self.root)]
        self.assertEqual(result['records'], 50000)
        self.assertEqual(len(result['latest']), 60)
        self.assertEqual(len(cache.latest), 60)
        self.assertEqual(len(cache.dispatched), 0)
        self.assertLess(peak, 12 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
