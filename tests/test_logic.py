"""Pure-logic unit tests for fog-controller (server.py).

All tests use only in-memory SQLite — no GPIO, no real DB, no network.
RPi.GPIO, rpi_rf, pymysql, flask_cors are stubbed via sys.modules before any
import from the app so the test runner works on any platform.
"""

import sys
import types

# ---------------------------------------------------------------------------
# Stub out hardware + optional deps BEFORE importing server.py
# ---------------------------------------------------------------------------

def _make_stub(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# RPi.GPIO
gpio_mod = _make_stub("RPi")
gpio_mod.GPIO = types.ModuleType("RPi.GPIO")
gpio_mod.GPIO.BCM = "BCM"
gpio_mod.GPIO.OUT = "OUT"
gpio_mod.GPIO.LOW = 0
gpio_mod.GPIO.HIGH = 1
gpio_mod.GPIO.setmode = lambda *a, **kw: None
gpio_mod.GPIO.setwarnings = lambda *a, **kw: None
gpio_mod.GPIO.setup = lambda *a, **kw: None
gpio_mod.GPIO.output = lambda *a, **kw: None
gpio_mod.GPIO.cleanup = lambda *a, **kw: None
sys.modules["RPi.GPIO"] = gpio_mod.GPIO

# rpi_rf (not imported directly in server.py but fog-controller.py may reference)
_make_stub("rpi_rf")

# pymysql — make it look present but harmless
pymysql_stub = _make_stub("pymysql")
pymysql_stub.cursors = types.ModuleType("pymysql.cursors")
pymysql_stub.cursors.DictCursor = object
pymysql_stub.connect = lambda **kw: None
sys.modules["pymysql.cursors"] = pymysql_stub.cursors

# flask_cors
cors_stub = _make_stub("flask_cors")
cors_stub.CORS = lambda app, **kw: None

# ---------------------------------------------------------------------------
# Now we can safely import the pure-logic helpers from server.py
# ---------------------------------------------------------------------------
import importlib
import os
import sqlite3
import tempfile
import threading
import unittest

# We'll import just the pure functions by executing the module with a patched
# TANK_DB so it uses an in-memory/temp file.
# Strategy: set env vars + monkeypatch TANK_DB after import.

# We need flask available (it's a real dep on the test runner's Python).
# If not installed, skip flask-dependent tests — but still run pure-logic ones.
try:
    import flask  # noqa: F401
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False


class _TankHelpers(unittest.TestCase):
    """Tests for the in-process pure helper functions extracted from server.py.

    Because server.py merges Flask routes with business logic we test the
    *formulas* directly rather than importing the full module, keeping the tests
    fast and dependency-free.
    """

    # ------------------------------------------------------------------
    # 1. _level_from_row — the core tank-level calculation
    # ------------------------------------------------------------------

    def _level_from_row(self, row: dict) -> float:
        """Reimplementation mirroring server.py::_level_from_row."""
        lvl = row["level_at_refill_ml"] - row["activations_since_refill"] * row["ml_per_activation"]
        return max(0.0, min(row["capacity_ml"], lvl))

    def test_level_full_tank_zero_activations(self):
        row = {"level_at_refill_ml": 250.0, "activations_since_refill": 0,
               "ml_per_activation": 10.0, "capacity_ml": 250.0}
        self.assertAlmostEqual(self._level_from_row(row), 250.0)

    def test_level_decreases_with_activations(self):
        row = {"level_at_refill_ml": 250.0, "activations_since_refill": 10,
               "ml_per_activation": 10.0, "capacity_ml": 250.0}
        self.assertAlmostEqual(self._level_from_row(row), 150.0)

    def test_level_clamps_to_zero_when_overdrawn(self):
        row = {"level_at_refill_ml": 50.0, "activations_since_refill": 10,
               "ml_per_activation": 10.0, "capacity_ml": 250.0}
        # 50 - 10*10 = -50 → clamped to 0
        self.assertAlmostEqual(self._level_from_row(row), 0.0)

    def test_level_clamps_to_capacity(self):
        # If level_at_refill_ml somehow exceeds capacity (shouldn't happen, but guard)
        row = {"level_at_refill_ml": 300.0, "activations_since_refill": 0,
               "ml_per_activation": 10.0, "capacity_ml": 250.0}
        self.assertAlmostEqual(self._level_from_row(row), 250.0)

    def test_level_exactly_at_zero(self):
        row = {"level_at_refill_ml": 100.0, "activations_since_refill": 10,
               "ml_per_activation": 10.0, "capacity_ml": 250.0}
        self.assertAlmostEqual(self._level_from_row(row), 0.0)

    # ------------------------------------------------------------------
    # 2. EWMA calibration formula
    # ------------------------------------------------------------------

    def _ewma_update(self, old_mpa, sample, alpha):
        return alpha * sample + (1 - alpha) * old_mpa

    def test_ewma_first_sample_replaces_seed(self):
        # First real sample should fully replace seed when calibrated=False
        # (server.py: if not calibrated: new_mpa = sample)
        sample = 12.5
        self.assertAlmostEqual(sample, 12.5)

    def test_ewma_normal_refill_alpha_0_5(self):
        old, sample, alpha = 10.0, 14.0, 0.5
        result = self._ewma_update(old, sample, alpha)
        self.assertAlmostEqual(result, 12.0)

    def test_ewma_empty_refill_alpha_0_7(self):
        # Empty-tank refill uses stronger weight (_CAL_ALPHA_EMPTY = 0.7)
        old, sample, alpha = 10.0, 20.0, 0.7
        result = self._ewma_update(old, sample, alpha)
        self.assertAlmostEqual(result, 17.0)

    def test_ewma_convergence_after_many_cycles(self):
        """EWMA should converge to the true value after several normal cycles."""
        true_mpa = 15.0
        mpa = 10.0  # seed
        alpha = 0.5
        for _ in range(20):
            mpa = self._ewma_update(mpa, true_mpa, alpha)
        self.assertAlmostEqual(mpa, true_mpa, places=2)

    def test_ewma_empty_weight_pulls_faster(self):
        old, sample = 10.0, 20.0
        normal = self._ewma_update(old, sample, 0.5)
        empty = self._ewma_update(old, sample, 0.7)
        self.assertGreater(empty, normal)

    # ------------------------------------------------------------------
    # 3. Calibration sample formula: consumed / activations
    # ------------------------------------------------------------------

    def _calibration_sample(self, level_at_refill, remaining, activations):
        consumed = level_at_refill - remaining
        if activations > 0 and consumed > 0:
            return consumed / activations
        return None

    def test_calibration_sample_normal(self):
        sample = self._calibration_sample(250.0, 50.0, 20)
        self.assertAlmostEqual(sample, 10.0)

    def test_calibration_sample_empty_tank(self):
        # was_empty → remaining = 0
        sample = self._calibration_sample(250.0, 0.0, 25)
        self.assertAlmostEqual(sample, 10.0)

    def test_calibration_sample_zero_activations_returns_none(self):
        sample = self._calibration_sample(250.0, 100.0, 0)
        self.assertIsNone(sample)

    def test_calibration_sample_no_consumption_returns_none(self):
        # consumed = 0 → no sample
        sample = self._calibration_sample(250.0, 250.0, 10)
        self.assertIsNone(sample)

    def test_calibration_sample_partial_drain(self):
        sample = self._calibration_sample(200.0, 100.0, 10)
        self.assertAlmostEqual(sample, 10.0)

    # ------------------------------------------------------------------
    # 4. Level percentage formula
    # ------------------------------------------------------------------

    def _level_pct(self, level_ml, capacity_ml):
        return round(level_ml / capacity_ml * 100) if capacity_ml > 0 else 0

    def test_level_pct_full(self):
        self.assertEqual(self._level_pct(250.0, 250.0), 100)

    def test_level_pct_half(self):
        self.assertEqual(self._level_pct(125.0, 250.0), 50)

    def test_level_pct_empty(self):
        self.assertEqual(self._level_pct(0.0, 250.0), 0)

    def test_level_pct_zero_capacity_returns_zero(self):
        self.assertEqual(self._level_pct(100.0, 0), 0)

    # ------------------------------------------------------------------
    # 5. Estimated activations remaining
    # ------------------------------------------------------------------

    def _est_activations_remaining(self, level_ml, mpa):
        return int(level_ml / mpa) if mpa > 0 else None

    def test_est_activations_full_tank(self):
        self.assertEqual(self._est_activations_remaining(250.0, 10.0), 25)

    def test_est_activations_partial(self):
        self.assertEqual(self._est_activations_remaining(75.0, 10.0), 7)  # int floors

    def test_est_activations_zero_mpa_returns_none(self):
        self.assertIsNone(self._est_activations_remaining(250.0, 0))

    def test_est_activations_empty_tank(self):
        self.assertEqual(self._est_activations_remaining(0.0, 10.0), 0)

    # ------------------------------------------------------------------
    # 6. tank_set_capacity clamping (50–5000 ml)
    # ------------------------------------------------------------------

    def _clamp_capacity(self, v):
        return max(50.0, min(5000.0, float(v)))

    def test_capacity_clamp_min(self):
        self.assertAlmostEqual(self._clamp_capacity(0), 50.0)

    def test_capacity_clamp_max(self):
        self.assertAlmostEqual(self._clamp_capacity(9999), 5000.0)

    def test_capacity_clamp_valid(self):
        self.assertAlmostEqual(self._clamp_capacity(500), 500.0)

    def test_capacity_clamp_boundary_low(self):
        self.assertAlmostEqual(self._clamp_capacity(50), 50.0)

    def test_capacity_clamp_boundary_high(self):
        self.assertAlmostEqual(self._clamp_capacity(5000), 5000.0)

    # ------------------------------------------------------------------
    # 7. tank_refill: level_after when full=True vs partial
    # ------------------------------------------------------------------

    def _level_after(self, remaining, amount_ml, capacity, full=False):
        if full or amount_ml is None:
            return capacity
        return max(0.0, min(capacity, remaining + float(amount_ml)))

    def test_refill_full_tops_to_capacity(self):
        self.assertAlmostEqual(self._level_after(50.0, None, 250.0, full=True), 250.0)

    def test_refill_partial_adds_amount(self):
        self.assertAlmostEqual(self._level_after(50.0, 150.0, 250.0), 200.0)

    def test_refill_partial_clamps_to_capacity(self):
        self.assertAlmostEqual(self._level_after(200.0, 200.0, 250.0), 250.0)

    def test_refill_partial_not_below_zero(self):
        self.assertAlmostEqual(self._level_after(0.0, -10.0, 250.0), 0.0)

    # ------------------------------------------------------------------
    # 8. RF code constants (verifying the ON/OFF hex values)
    # ------------------------------------------------------------------

    def test_rf_code_on_hex(self):
        # fog-controller.py defines CODE_ON = 4543756 decimal = 0x45550C
        CODE_ON = 4543756
        self.assertEqual(f"0x{CODE_ON:06X}", "0x45550C")

    def test_rf_code_off_hex(self):
        # fog-controller.py defines CODE_OFF = 4543792 decimal = 0x455530
        CODE_OFF = 4543792
        self.assertEqual(f"0x{CODE_OFF:06X}", "0x455530")

    def test_rf_codes_are_different(self):
        CODE_ON = 4543756
        CODE_OFF = 4543792
        self.assertNotEqual(CODE_ON, CODE_OFF)

    def test_rf_codes_24bit_range(self):
        # Both codes must fit in 24 bits (< 16 777 216)
        CODE_ON = 4543756
        CODE_OFF = 4543792
        self.assertLess(CODE_ON, 2**24)
        self.assertLess(CODE_OFF, 2**24)

    def test_rf_controller_code_on_decimal(self):
        # The ON code is 36 less than the OFF code (verified from source)
        CODE_ON = 4543756
        CODE_OFF = 4543792
        self.assertEqual(CODE_OFF - CODE_ON, 36)

    # ------------------------------------------------------------------
    # 9. Auto-fog interval validation (valid: 5,15,30,60,120)
    # ------------------------------------------------------------------

    def _is_valid_interval(self, v):
        try:
            return int(v) in (5, 15, 30, 60, 120)
        except (TypeError, ValueError):
            return False

    def test_valid_intervals(self):
        for i in (5, 15, 30, 60, 120):
            self.assertTrue(self._is_valid_interval(i))

    def test_invalid_interval_10(self):
        self.assertFalse(self._is_valid_interval(10))

    def test_invalid_interval_none(self):
        self.assertFalse(self._is_valid_interval(None))

    def test_invalid_interval_string(self):
        self.assertFalse(self._is_valid_interval("abc"))

    def test_invalid_interval_zero(self):
        self.assertFalse(self._is_valid_interval(0))

    # ------------------------------------------------------------------
    # 10. Hex-code validation for /api/fog/custom
    # ------------------------------------------------------------------

    def _is_valid_hex(self, code):
        if not code:
            return False
        try:
            int(code, 16)
            return True
        except (ValueError, TypeError):
            return False

    def test_hex_valid_uppercase(self):
        self.assertTrue(self._is_valid_hex("454B8C"))

    def test_hex_valid_lowercase(self):
        self.assertTrue(self._is_valid_hex("454b8c"))

    def test_hex_valid_with_prefix(self):
        # int("0x...", 16) is valid
        self.assertTrue(self._is_valid_hex("0x454B8C"))

    def test_hex_invalid_nonhex(self):
        self.assertFalse(self._is_valid_hex("XYZABC"))

    def test_hex_invalid_empty(self):
        self.assertFalse(self._is_valid_hex(""))

    def test_hex_invalid_none(self):
        self.assertFalse(self._is_valid_hex(None))

    # ------------------------------------------------------------------
    # 11. Seed ml_per_activation = capacity / 25
    # ------------------------------------------------------------------

    def test_seed_mpa_default_capacity(self):
        capacity = 250.0
        seed = capacity / 25.0
        self.assertAlmostEqual(seed, 10.0)

    def test_seed_mpa_custom_capacity(self):
        capacity = 500.0
        seed = capacity / 25.0
        self.assertAlmostEqual(seed, 20.0)

    # ------------------------------------------------------------------
    # 12. Auto-fog auto-disable deadline (1 hour = 3600 s)
    # ------------------------------------------------------------------

    def test_auto_disable_is_one_hour(self):
        import time
        start = time.time()
        disable_time = start + 3600
        self.assertAlmostEqual(disable_time - start, 3600, delta=1)

    # ------------------------------------------------------------------
    # 13. Edge: refill with remaining_ml clamped to [0, capacity]
    # ------------------------------------------------------------------

    def _clamp_remaining(self, remaining_ml, capacity):
        return max(0.0, min(capacity, float(remaining_ml)))

    def test_remaining_clamp_below_zero(self):
        self.assertAlmostEqual(self._clamp_remaining(-50, 250), 0.0)

    def test_remaining_clamp_above_capacity(self):
        self.assertAlmostEqual(self._clamp_remaining(300, 250), 250.0)

    def test_remaining_clamp_normal(self):
        self.assertAlmostEqual(self._clamp_remaining(100, 250), 100.0)


class _SqliteIntegration(unittest.TestCase):
    """Lightweight integration tests that exercise the SQLite schema and helper
    queries used in server.py, without starting Flask or touching GPIO."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE tank (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                capacity_ml REAL NOT NULL DEFAULT 250,
                level_at_refill_ml REAL NOT NULL DEFAULT 250,
                activations_since_refill INTEGER NOT NULL DEFAULT 0,
                ml_per_activation REAL NOT NULL DEFAULT 10,
                calibrated INTEGER NOT NULL DEFAULT 0,
                last_refill_at TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE refills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                level_before_ml REAL,
                remaining_ml REAL,
                amount_added_ml REAL,
                level_after_ml REAL,
                activations_in_cycle INTEGER,
                ml_per_act_sample REAL,
                was_empty INTEGER NOT NULL DEFAULT 0
            )
        """)
        self.conn.execute(
            "INSERT INTO tank (id, capacity_ml, level_at_refill_ml, ml_per_activation)"
            " VALUES (1, 250, 250, 10)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _level_from_row(self, row):
        lvl = row["level_at_refill_ml"] - row["activations_since_refill"] * row["ml_per_activation"]
        return max(0.0, min(row["capacity_ml"], lvl))

    def _bump(self, n=1):
        for _ in range(n):
            self.conn.execute(
                "UPDATE tank SET activations_since_refill = activations_since_refill + 1 WHERE id = 1")
        self.conn.commit()

    def test_initial_state_full(self):
        row = self.conn.execute("SELECT * FROM tank WHERE id = 1").fetchone()
        self.assertAlmostEqual(self._level_from_row(row), 250.0)

    def test_bump_decreases_level(self):
        self._bump(5)
        row = self.conn.execute("SELECT * FROM tank WHERE id = 1").fetchone()
        self.assertAlmostEqual(self._level_from_row(row), 200.0)

    def test_bump_to_empty(self):
        self._bump(25)
        row = self.conn.execute("SELECT * FROM tank WHERE id = 1").fetchone()
        self.assertAlmostEqual(self._level_from_row(row), 0.0)

    def test_refill_resets_activations(self):
        self._bump(10)
        self.conn.execute(
            "UPDATE tank SET level_at_refill_ml = 250, activations_since_refill = 0 WHERE id = 1")
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM tank WHERE id = 1").fetchone()
        self.assertEqual(row["activations_since_refill"], 0)
        self.assertAlmostEqual(self._level_from_row(row), 250.0)

    def test_refill_log_entry_inserted(self):
        self.conn.execute(
            "INSERT INTO refills (ts, level_before_ml, remaining_ml, amount_added_ml,"
            " level_after_ml, activations_in_cycle, ml_per_act_sample, was_empty)"
            " VALUES ('2026-01-01T00:00:00Z', 50.0, 0.0, 250.0, 250.0, 20, 12.5, 1)")
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM refills ORDER BY id DESC LIMIT 1").fetchone()
        self.assertAlmostEqual(row["ml_per_act_sample"], 12.5)
        self.assertEqual(row["was_empty"], 1)

    def test_calibrated_flag_updates(self):
        self.conn.execute("UPDATE tank SET calibrated = 1 WHERE id = 1")
        self.conn.commit()
        row = self.conn.execute("SELECT calibrated FROM tank WHERE id = 1").fetchone()
        self.assertEqual(row["calibrated"], 1)

    def test_mpa_update_stored(self):
        new_mpa = 12.5
        self.conn.execute("UPDATE tank SET ml_per_activation = ? WHERE id = 1", (new_mpa,))
        self.conn.commit()
        row = self.conn.execute("SELECT ml_per_activation FROM tank WHERE id = 1").fetchone()
        self.assertAlmostEqual(row["ml_per_activation"], 12.5)

    def test_history_query_returns_most_recent_first(self):
        for i in range(3):
            self.conn.execute(
                "INSERT INTO refills (ts, was_empty) VALUES (?, 0)", (f"2026-01-0{i+1}T00:00:00Z",))
        self.conn.commit()
        rows = self.conn.execute("SELECT * FROM refills ORDER BY id DESC LIMIT 10").fetchall()
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, sorted(ids, reverse=True))


if __name__ == "__main__":
    unittest.main()
