"""Refill-calibration tests (Nachgiessen, 2026-08-14): randvoll -> nebeln -> nachfuellen.

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


class RefillCalCompute(unittest.TestCase):
    def test_happy_path_time_sample_is_primary(self):
        # 80 ml ueber 10 Aktivierungen / 400 s -> 8 ml/Stoss + 0,2 ml/s
        sample, mps = server.refill_cal_compute(80, 10, 400, 250)
        self.assertAlmostEqual(sample, 8.0)
        self.assertAlmostEqual(mps, 0.2)

    def test_short_fog_time_falls_back_to_activations(self):
        sample, mps = server.refill_cal_compute(40, 5, 4, 250)
        self.assertAlmostEqual(sample, 8.0)
        self.assertIsNone(mps, "unter _CAL_MIN_FOG_S gibt es kein Zeit-Sample")

    def test_too_little_data_has_plain_language(self):
        with self.assertRaises(ValueError) as cm:
            server.refill_cal_compute(50, 2, 4, 250)
        self.assertIn("Zu wenig genebelt", str(cm.exception))

    def test_long_run_with_few_activations_is_fine(self):
        # 2 lange manuelle on/off-Bursts: Zeit-Sample traegt allein
        sample, mps = server.refill_cal_compute(12, 2, 60, 250)
        self.assertAlmostEqual(mps, 0.2)
        self.assertAlmostEqual(sample, 6.0)

    def test_zero_or_negative_ml_rejected(self):
        for ml in (0, -5):
            with self.assertRaises(ValueError):
                server.refill_cal_compute(ml, 5, 100, 250)

    def test_more_than_capacity_rejected(self):
        with self.assertRaises(ValueError) as cm:
            server.refill_cal_compute(300, 5, 100, 250)
        self.assertIn("Tankgröße", str(cm.exception))

    def test_implausible_mps_names_the_rf_remote(self):
        # 200 ml in 20 s = 10 ml/s — weit ueber der 500-W-Klasse
        with self.assertRaises(ValueError) as cm:
            server.refill_cal_compute(200, 5, 20, 250)
        self.assertIn("Funk-Fernbedienung", str(cm.exception))

    def test_implausible_act_sample_names_the_rf_remote(self):
        with self.assertRaises(ValueError) as cm:
            server.refill_cal_compute(1, 10, 4, 250)
        self.assertIn("Funk-Fernbedienung", str(cm.exception))


class RefillCalFlow(unittest.TestCase):
    def setUp(self):
        _fresh_tank()
        server.refill_cal_abort()
        server.config["activationCount"] = 100   # kumulative Snapshot-Zaehler
        server.config["fogSecondsTotal"] = 500.0

    def _fog(self, n, secs=0.0):
        server.config["activationCount"] += n
        for _ in range(n):
            server.tank_bump_activation()
        if secs:
            server.config["fogSecondsTotal"] += secs
            server.tank_bump_seconds(secs)

    def test_start_requires_idle(self):
        server.refill_cal_start(250)
        with self.assertRaises(ValueError):
            server.refill_cal_start(250)

    def test_start_updates_capacity(self):
        server.refill_cal_start(300)
        self.assertEqual(server.tank_state()["capacity_ml"], 300.0)

    def test_state_counts_activations_and_time(self):
        server.refill_cal_start(250)
        self._fog(4)
        st = server.refill_cal_state()
        self.assertEqual(st["phase"], "fogging")
        self.assertEqual(st["acts"], 4)
        self.assertGreaterEqual(st["elapsed_s"], 0)
        self.assertEqual(st["capacity_ml"], 250.0)

    def test_finish_commits_atomically(self):
        server.refill_cal_start(250)
        self._fog(10, secs=400.0)
        res = server.refill_cal_finish(80)
        self.assertAlmostEqual(res["ml_per_activation"], 8.0)
        self.assertAlmostEqual(res["ml_per_second"], 0.2)
        self.assertEqual(res["acts"], 10)
        self.assertAlmostEqual(res["fog_seconds"], 400.0)
        s = server.tank_state()
        # randvoll = Kapazitaet, Zaehler genullt, kalibriert, Zeit-Modell aktiv
        self.assertEqual(s["level_ml"], 250.0)
        self.assertEqual(s["activations_since_refill"], 0)
        self.assertEqual(s["fog_seconds_since_refill"], 0.0)
        self.assertTrue(s["calibrated"])
        self.assertAlmostEqual(s["ml_per_activation"], 8.0)
        self.assertAlmostEqual(s["ml_per_second"], 0.2)
        # Refill-Zeile fuer die Historie
        hist = server.tank_history()
        self.assertEqual(hist[0]["amount_added_ml"], 80.0)
        self.assertEqual(hist[0]["activations_in_cycle"], 10)
        # Zustandsmaschine wieder frei
        self.assertEqual(server.refill_cal_state()["phase"], "idle")

    def test_finish_sample_replaces_ewma_outright(self):
        # Vorher grob falsche Schaetzung — die Messung ERSETZT sie komplett
        server.refill_cal_start(250)
        self._fog(5)
        server.refill_cal_finish(25)   # 5 ml/Stoss
        self.assertAlmostEqual(server.tank_state()["ml_per_activation"], 5.0)

    def test_start_books_the_brim_level_immediately(self):
        # "Tank ist randvoll" ist ein FAKT — die Anzeige darf nicht bis zum
        # Abschluss den alten Stand zeigen
        self._fog(5)   # Stand erst absenken (frische DB startet voll)
        self.assertLess(server.tank_state()["level_ml"], 250.0)
        server.refill_cal_start(250)
        self.assertEqual(server.tank_state()["level_ml"], 250.0)

    def test_abort_discards_only_the_measurement(self):
        before = server.tank_state()
        server.refill_cal_start(250)
        self._fog(5)
        server.refill_cal_abort()
        after = server.tank_state()
        self.assertEqual(server.refill_cal_state()["phase"], "idle")
        # Samples/Kalibrier-Flag unangetastet …
        self.assertEqual(before["ml_per_activation"], after["ml_per_activation"])
        self.assertEqual(before["calibrated"], after["calibrated"])
        # … der Randvoll-Stand vom Start bleibt (Fakt), die 5 Bursts wurden
        # normal gebucht und zaehlen ab randvoll
        self.assertEqual(after["activations_since_refill"], 5)
        self.assertAlmostEqual(
            after["level_ml"], 250.0 - 5 * after["ml_per_activation"], places=1)

    def test_finish_without_start_rejected(self):
        with self.assertRaises(ValueError):
            server.refill_cal_finish(50)

    def test_normal_refill_during_run_cannot_corrupt_the_count(self):
        # /api/tank/refill nullt nur den Zyklus-Zaehler — der Kalibrier-
        # Snapshot haengt am kumulativen activationCount und bleibt korrekt
        server.refill_cal_start(250)
        self._fog(3)
        server.tank_refill(amount_ml=30)
        self._fog(3)
        self.assertEqual(server.refill_cal_state()["acts"], 6)

    def test_capacity_change_mid_run_uses_live_value(self):
        server.refill_cal_start(250)
        self._fog(10)
        server.tank_set_capacity(300)
        res = server.refill_cal_finish(90)
        self.assertEqual(res["level_ml"], 300.0)

    def test_start_without_capacity_keeps_stored_value(self):
        server.refill_cal_start(None)
        self.assertEqual(server.refill_cal_state()["capacity_ml"],
                         server.TANK_DEFAULT_CAPACITY)


if __name__ == "__main__":
    unittest.main()


class FogTimeTracking(unittest.TestCase):
    """ON-Zeit-Messung (2026-08-14): exakt zwischen on/off, gecappt."""

    def setUp(self):
        _fresh_tank()
        server.refill_cal_abort()
        server.config["fogSecondsTotal"] = 0.0
        server._fog_on_ts = None

    def test_booking_is_exact_between_on_and_off(self):
        server._fog_on_ts = 1000.0
        dur = server._book_fog_time(now=1007.5)
        self.assertAlmostEqual(dur, 7.5)
        self.assertAlmostEqual(server.config["fogSecondsTotal"], 7.5)
        self.assertIsNone(server._fog_on_ts)

    def test_booking_caps_a_forgotten_off(self):
        # Auto-Fog sendet nie "off"; die Maschine beendet den Burst selbst —
        # ohne Cap zaehlte eine vergessene ON-Phase endlos.
        server._fog_on_ts = 1000.0
        dur = server._book_fog_time(now=1000.0 + 999)
        self.assertAlmostEqual(dur, server.FOG_BURST_CAP_S)

    def test_pending_phase_counts_into_live_level(self):
        # mps kalibrieren, dann eine LAUFENDE ON-Phase: Level sinkt live
        server.refill_cal_start(250)
        server.config["activationCount"] += 4
        for _ in range(4):
            server.tank_bump_activation()
        server.config["fogSecondsTotal"] += 100.0
        server.tank_bump_seconds(100.0)
        server.refill_cal_finish(20)          # 0,2 ml/s
        server._fog_on_ts = time.time() - 10  # 10 s on, noch kein off
        s = server.tank_state()
        self.assertLess(s["level_ml"], 250.0)
        self.assertGreater(s["level_ml"], 245.0)
        server._fog_on_ts = None
