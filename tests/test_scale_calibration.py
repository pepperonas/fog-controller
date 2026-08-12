"""Scale-calibration tests (2026-08-13): weigh -> burn -> weigh -> refill -> weigh.

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


class ScaleCalCompute(unittest.TestCase):
    def test_happy_path(self):
        consumed, sample, added = server.scale_cal_compute(2000, 1940, 2050, 5)
        self.assertEqual(consumed, 60)
        self.assertEqual(sample, 12)
        self.assertEqual(added, 110)

    def test_no_consumption_rejected(self):
        with self.assertRaises(ValueError):
            server.scale_cal_compute(2000, 2000, 2050, 5)
        with self.assertRaises(ValueError):
            server.scale_cal_compute(2000, 2010, 2050, 5)

    def test_implausible_sample_rejected(self):
        with self.assertRaises(ValueError):
            server.scale_cal_compute(2000, 1999, 2050, 5)     # 0.2 ml/burst
        with self.assertRaises(ValueError):
            server.scale_cal_compute(4000, 1000, 4100, 5)     # 600 ml/burst

    def test_negative_refill_rejected(self):
        with self.assertRaises(ValueError):
            server.scale_cal_compute(2000, 1940, 1900, 5)

    def test_zero_bursts_rejected(self):
        with self.assertRaises(ValueError):
            server.scale_cal_compute(2000, 1940, 2050, 0)


class ScaleCalFlow(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_tank()
        self.fired = []
        self._orig_exec = server.execute_command
        # No RF/MariaDB in tests — but the burn MUST count activations like
        # the real path does (finish() resets them; the model stays coherent).
        server.execute_command = lambda cmd, kind="manual": (
            self.fired.append((cmd, kind)), server.tank_bump_activation())
        server.scale_cal_abort()

    def tearDown(self):
        server.execute_command = self._orig_exec
        server.scale_cal_abort()
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def test_full_cycle_calibrates_and_books_refill(self):
        level0 = server.tank_state()["level_ml"]         # 250 (fresh full)
        st = server.scale_cal_start(2000.0, bursts=1, gap_s=5)
        # one burst finishes instantly (first burst has no gap wait)
        for _ in range(100):
            if server.scale_cal_state()["phase"] == "weigh":
                break
            time.sleep(0.02)
        self.assertEqual(server.scale_cal_state()["phase"], "weigh")
        self.assertEqual(self.fired, [("on", "calib")])
        st = server.scale_cal_weigh(1988.0)              # 12 g consumed
        self.assertEqual(st["phase"], "refill")
        self.assertEqual(st["consumed_ml"], 12.0)
        self.assertEqual(st["sample_ml"], 12.0)
        res = server.scale_cal_finish(2038.0)            # +50 g refilled
        self.assertEqual(res["ml_per_activation"], 12.0)
        t = server.tank_state()
        self.assertTrue(t["calibrated"])
        self.assertEqual(t["ml_per_activation"], 12.0)
        self.assertEqual(t["activations_since_refill"], 0)
        # level = start - consumed + added, clamped to capacity
        self.assertAlmostEqual(t["level_ml"],
                               min(t["capacity_ml"], level0 - 12 + 50), delta=0.1)
        # booked as a refill row (shows up in the history/chart)
        hist = server.tank_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["activations_in_cycle"], 1)
        self.assertAlmostEqual(hist[0]["ml_per_act_sample"], 12.0, places=2)
        self.assertEqual(server.scale_cal_state()["phase"], "idle")

    def test_weigh_rejects_no_consumption_and_keeps_phase(self):
        server.scale_cal_start(2000.0, bursts=1, gap_s=5)
        for _ in range(100):
            if server.scale_cal_state()["phase"] == "weigh":
                break
            time.sleep(0.02)
        with self.assertRaises(ValueError):
            server.scale_cal_weigh(2000.0)
        self.assertEqual(server.scale_cal_state()["phase"], "weigh",
                         "abgelehnter Messwert darf den Schritt nicht kippen")

    def test_abort_mid_burn_writes_nothing(self):
        mpa0 = server.tank_state()["ml_per_activation"]
        server.scale_cal_start(2000.0, bursts=3, gap_s=5)
        time.sleep(0.1)                                   # first burst fired
        st = server.scale_cal_state()
        self.assertEqual(st["phase"], "burning")
        self.assertGreaterEqual(st["done"], 1)
        server.scale_cal_abort()
        self.assertEqual(server.scale_cal_state()["phase"], "idle")
        t = server.tank_state()
        self.assertEqual(t["ml_per_activation"], mpa0)
        self.assertFalse(t["calibrated"])
        self.assertEqual(server.tank_history(), [])
        # the fired burst still counts as a normal activation (old model absorbs it)
        self.assertEqual(t["activations_since_refill"], 1)

    def test_double_start_rejected(self):
        server.scale_cal_start(2000.0, bursts=3, gap_s=5)
        with self.assertRaises(ValueError):
            server.scale_cal_start(2000.0, bursts=3, gap_s=5)
        server.scale_cal_abort()

    def test_finish_out_of_phase_rejected(self):
        with self.assertRaises(ValueError):
            server.scale_cal_finish(2100.0)


if __name__ == "__main__":
    unittest.main()
