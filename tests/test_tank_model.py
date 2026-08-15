"""Tank-Modell + Nutzungs-Chart (2026-08-15): Fuellstand, Nachfuellen, Buckets.

Deckt die Rechenkerne ab, die die Anzeige treiben — Zeit- vs.
Aktivierungs-Modell, Kapazitaets-Klemmen, EWMA-Kalibrierung beim
Nachfuellen, Historie und die Chart-Bucketisierung.

Imports server.py with the same hardware stubs as test_logic.py, points
TANK_DB at a temp file and stubs execute_command so no RF/MariaDB is touched.
"""

import sys
import types

for _name in ("RPi", "rpi_rf", "pymysql"):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)
_g = types.ModuleType("RPi.GPIO")
for _k, _v in (("BCM", "BCM"), ("OUT", "OUT"), ("LOW", 0), ("HIGH", 1)):
    setattr(_g, _k, _v)
for _f in ("setmode", "setwarnings", "setup", "output", "cleanup"):
    setattr(_g, _f, lambda *a, **kw: None)
sys.modules["RPi"].GPIO = _g
sys.modules["RPi.GPIO"] = _g
sys.modules["rpi_rf"].RFDevice = lambda *a, **kw: types.SimpleNamespace(
    enable_tx=lambda: None, tx_code=lambda *a, **kw: None,
    cleanup=lambda: None)
if "flask_cors" not in sys.modules:
    _c = types.ModuleType("flask_cors")
    _c.CORS = lambda app, **kw: None
    sys.modules["flask_cors"] = _c

import datetime
import os
import pathlib
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import server


def _fresh_tank():
    """Point the tank at a brand-new temp DB and re-init."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    server.TANK_DB = path
    server._tank = None
    server.init_tank_db()
    return path


class LevelModel(unittest.TestCase):
    """_level_from_row: Zeit-Modell sobald ml/s kalibriert, sonst Aktivierungen."""

    def setUp(self):
        _fresh_tank()

    def _row(self):
        with server._tank_lock:
            return server._tank_conn().execute(
                "SELECT * FROM tank WHERE id = 1").fetchone()

    def _set(self, **cols):
        sets = ", ".join(f"{k} = ?" for k in cols)
        with server._tank_lock:
            conn = server._tank_conn()
            conn.execute(f"UPDATE tank SET {sets} WHERE id = 1", tuple(cols.values()))
            conn.commit()

    def test_activation_model_when_uncalibrated(self):
        self._set(level_at_refill_ml=250, activations_since_refill=5,
                  ml_per_activation=10, ml_per_second=None)
        self.assertAlmostEqual(server._level_from_row(self._row()), 200.0)

    def test_time_model_wins_once_mls_is_calibrated(self):
        # Aktivierungen wuerden 200 ergeben — die gemessene ZEIT sagt 190
        self._set(level_at_refill_ml=250, activations_since_refill=5,
                  ml_per_activation=10, ml_per_second=0.2,
                  seconds_since_refill=300)
        self.assertAlmostEqual(server._level_from_row(self._row()), 190.0)

    def test_pending_on_phase_counts_into_the_level(self):
        self._set(level_at_refill_ml=250, ml_per_second=0.5, seconds_since_refill=0)
        row = self._row()
        self.assertAlmostEqual(server._level_from_row(row, pending_s=10), 245.0)

    def test_level_never_leaves_the_tank(self):
        self._set(level_at_refill_ml=250, activations_since_refill=999,
                  ml_per_activation=10, ml_per_second=None)
        self.assertEqual(server._level_from_row(self._row()), 0.0)   # kein Minus
        self._set(level_at_refill_ml=9999, activations_since_refill=0)
        self.assertEqual(server._level_from_row(self._row()), 250.0)  # nicht ueber cap

    def test_zero_mls_falls_back_instead_of_freezing(self):
        # ml_per_second = 0 ist "nicht kalibriert", nicht "verbraucht nichts"
        self._set(level_at_refill_ml=250, activations_since_refill=3,
                  ml_per_activation=10, ml_per_second=0, seconds_since_refill=500)
        self.assertAlmostEqual(server._level_from_row(self._row()), 220.0)


class Capacity(unittest.TestCase):
    def setUp(self):
        _fresh_tank()

    def test_clamped_to_sane_range(self):
        self.assertEqual(server.tank_set_capacity(5)["capacity_ml"], 50.0)
        self.assertEqual(server.tank_set_capacity(99999)["capacity_ml"], 5000.0)

    def test_shrinking_below_the_level_caps_the_level(self):
        server.tank_set_capacity(500)
        self.assertEqual(server.tank_state()["level_ml"], 250.0)
        s = server.tank_set_capacity(100)
        self.assertLessEqual(s["level_ml"], 100.0)

    def test_capacity_change_resets_the_cycle_counter(self):
        for _ in range(4):
            server.tank_bump_activation()
        self.assertEqual(server.tank_state()["activations_since_refill"], 4)
        server.tank_set_capacity(300)
        self.assertEqual(server.tank_state()["activations_since_refill"], 0)

    def test_percent_tracks_the_level(self):
        server.tank_set_capacity(200)
        st = server.tank_state()
        self.assertEqual(st["level_pct"], round(st["level_ml"] / 200 * 100))


class RefillFeedback(unittest.TestCase):
    """tank_refill: die EWMA-Selbstkalibrierung aus dem Nutzer-Feedback."""

    def setUp(self):
        _fresh_tank()

    def test_full_refill_tops_up_to_capacity_and_zeroes_counters(self):
        for _ in range(6):
            server.tank_bump_activation()
        server.tank_bump_seconds(45)
        s = server.tank_refill(full=True)
        self.assertEqual(s["level_ml"], 250.0)
        self.assertEqual(s["activations_since_refill"], 0)
        self.assertEqual(s["fog_seconds_since_refill"], 0.0)

    def test_empty_feedback_calibrates_from_the_finished_cycle(self):
        # 25 Aktivierungen bis leer bei 250 ml => 10 ml je Aktivierung
        for _ in range(25):
            server.tank_bump_activation()
        s = server.tank_refill(was_empty=True, full=True)
        self.assertTrue(s["calibrated"])
        self.assertAlmostEqual(s["ml_per_activation"], 10.0, places=1)

    def test_remaining_feedback_calibrates_too(self):
        for _ in range(10):
            server.tank_bump_activation()
        s = server.tank_refill(remaining_ml=150, full=True)
        # 100 ml auf 10 Aktivierungen
        self.assertTrue(s["calibrated"])
        self.assertGreater(s["ml_per_activation"], 0)

    def test_refill_without_any_activations_keeps_the_estimate(self):
        before = server.tank_state()["ml_per_activation"]
        s = server.tank_refill(full=True)
        self.assertEqual(s["ml_per_activation"], before)
        self.assertFalse(s["calibrated"], "ohne Zyklus keine Kalibrierung")

    def test_partial_refill_adds_only_what_fits(self):
        for _ in range(20):
            server.tank_bump_activation()
        lvl = server.tank_state()["level_ml"]
        s = server.tank_refill(amount_ml=20, remaining_ml=lvl)
        self.assertLessEqual(s["level_ml"], 250.0)
        self.assertGreater(s["level_ml"], lvl)

    def test_history_records_every_refill_newest_first(self):
        for _ in range(5):
            server.tank_bump_activation()
        server.tank_refill(amount_ml=30, remaining_ml=100)
        for _ in range(5):
            server.tank_bump_activation()
        server.tank_refill(amount_ml=40, remaining_ml=120)
        h = server.tank_history()
        self.assertGreaterEqual(len(h), 2)
        self.assertEqual(h[0]["amount_added_ml"], 40.0)


class UsageBuckets(unittest.TestCase):
    """Chart-Bucketisierung: lueckenlos, chronologisch, ausgerichtet."""

    def setUp(self):
        _fresh_tank()

    def test_every_range_is_gapless_and_aligned(self):
        for key, (span, bucket) in server.USAGE_RANGES.items():
            d = server.usage_analytics(key)
            ts = [b["t"] for b in d["buckets"]]
            self.assertEqual(d["bucket_s"], bucket, key)
            self.assertEqual(ts, sorted(ts), key)
            self.assertTrue(all(t % bucket == 0 for t in ts),
                            f"{key}: Buckets nicht am Raster")
            self.assertTrue(all(ts[i + 1] - ts[i] == bucket for i in range(len(ts) - 1)),
                            f"{key}: Luecke in der Reihe")

    def test_bucket_count_matches_the_span(self):
        for key, (span, bucket) in server.USAGE_RANGES.items():
            n = len(server.usage_analytics(key)["buckets"])
            self.assertAlmostEqual(n, span / bucket, delta=2, msg=key)

    def test_unknown_range_reports_the_fallback_it_used(self):
        d = server.usage_analytics("voellig-egal")
        self.assertEqual(d["range"], "24h")
        self.assertEqual(d["bucket_s"], 3600)

    def test_finer_range_means_finer_buckets(self):
        self.assertLess(server.usage_bucket_spec("1h")[1],
                        server.usage_bucket_spec("24h")[1])
        self.assertLess(server.usage_bucket_spec("24h")[1],
                        server.usage_bucket_spec("30d")[1])

    def test_refills_ride_along_in_the_payload(self):
        for _ in range(8):
            server.tank_bump_activation()
        server.tank_refill(amount_ml=25, remaining_ml=100)
        d = server.usage_analytics("24h")
        self.assertEqual(len(d["refills"]), 1)
        self.assertEqual(d["refills"][0]["ml"], 25.0)
        self.assertIn("t", d["refills"][0])

    def test_old_refills_drop_out_of_a_short_range(self):
        for _ in range(8):
            server.tank_bump_activation()
        server.tank_refill(amount_ml=25, remaining_ml=100)
        # Zeile kuenstlich altern lassen
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
        with server._tank_lock:
            conn = server._tank_conn()
            conn.execute("UPDATE refills SET ts = ?", (old.isoformat(),))
            conn.commit()
        self.assertEqual(len(server.usage_analytics("1h")["refills"]), 0)
        self.assertEqual(len(server.usage_analytics("7d")["refills"]), 1)


class BurstTiming(unittest.TestCase):
    """Die ON-Zeit-Messung, auf der das ml/s-Modell steht."""

    def setUp(self):
        _fresh_tank()
        server.config["fogSecondsTotal"] = 0.0
        server._fog_on_ts = None

    def test_pending_is_zero_while_off(self):
        self.assertEqual(server._fog_pending_s(), 0.0)

    def test_pending_grows_while_on_and_stops_at_the_cap(self):
        server._fog_on_ts = 1000.0
        self.assertAlmostEqual(server._fog_pending_s(now=1003.5), 3.5)
        self.assertAlmostEqual(server._fog_pending_s(now=1000 + 10 * server.FOG_BURST_CAP_S),
                               server.FOG_BURST_CAP_S)

    def test_booking_twice_counts_once(self):
        server._fog_on_ts = 1000.0
        server._book_fog_time(now=1005.0)
        again = server._book_fog_time(now=1010.0)
        self.assertEqual(again, 0.0)
        self.assertAlmostEqual(server.config["fogSecondsTotal"], 5.0)

    def test_seconds_land_on_the_cycle_counter(self):
        server._fog_on_ts = 2000.0
        server._book_fog_time(now=2007.0)
        self.assertAlmostEqual(server.tank_state()["fog_seconds_since_refill"], 7.0)

    def test_negative_or_zero_seconds_are_ignored(self):
        before = server.tank_state()["fog_seconds_since_refill"]
        server.tank_bump_seconds(0)
        server.tank_bump_seconds(-5)
        self.assertEqual(server.tank_state()["fog_seconds_since_refill"], before)


if __name__ == "__main__":
    unittest.main()
