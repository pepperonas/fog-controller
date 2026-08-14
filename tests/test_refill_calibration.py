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
    def test_happy_path(self):
        # 80 ml ueber 10 Aktivierungen = 8 ml je Stoss
        self.assertAlmostEqual(server.refill_cal_compute(80, 10, 250), 8.0)

    def test_too_few_activations_has_plain_language(self):
        with self.assertRaises(ValueError) as cm:
            server.refill_cal_compute(50, 2, 250)
        self.assertIn("mindestens", str(cm.exception))

    def test_zero_or_negative_ml_rejected(self):
        for ml in (0, -5):
            with self.assertRaises(ValueError):
                server.refill_cal_compute(ml, 5, 250)

    def test_more_than_capacity_rejected(self):
        with self.assertRaises(ValueError) as cm:
            server.refill_cal_compute(300, 5, 250)
        self.assertIn("Tankgröße", str(cm.exception))

    def test_implausible_sample_names_the_rf_remote(self):
        # 1 ml ueber 10 Stoesse = 0.1 ml/Stoss -> unter der Untergrenze;
        # der Klartext nennt die wahrscheinlichste Ursache (Funk-Fernbedienung)
        with self.assertRaises(ValueError) as cm:
            server.refill_cal_compute(1, 10, 250)
        self.assertIn("Funk-Fernbedienung", str(cm.exception))


class RefillCalFlow(unittest.TestCase):
    def setUp(self):
        _fresh_tank()
        server.refill_cal_abort()
        server.config["activationCount"] = 100   # kumulativer Snapshot-Zaehler

    def _fog(self, n):
        server.config["activationCount"] += n
        for _ in range(n):
            server.tank_bump_activation()

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
        self._fog(10)
        res = server.refill_cal_finish(80)
        self.assertAlmostEqual(res["ml_per_activation"], 8.0)
        self.assertEqual(res["acts"], 10)
        s = server.tank_state()
        # randvoll = Kapazitaet, Zaehler genullt, kalibriert
        self.assertEqual(s["level_ml"], 250.0)
        self.assertEqual(s["activations_since_refill"], 0)
        self.assertTrue(s["calibrated"])
        self.assertAlmostEqual(s["ml_per_activation"], 8.0)
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

    def test_abort_writes_nothing(self):
        before = server.tank_state()
        server.refill_cal_start(250)
        self._fog(5)
        server.refill_cal_abort()
        after = server.tank_state()
        self.assertEqual(server.refill_cal_state()["phase"], "idle")
        self.assertEqual(before["ml_per_activation"], after["ml_per_activation"])
        self.assertEqual(before["calibrated"], after["calibrated"])
        # die 5 Bursts wurden normal gebucht (altes Modell absorbiert sie)
        self.assertEqual(after["activations_since_refill"], 5)

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
