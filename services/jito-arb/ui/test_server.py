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
