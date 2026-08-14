#!/usr/bin/env python3
"""Fog machine controller — Flask + RPi.GPIO (replaces the Node/Express+PM2 app
that shelled out sudo python3 per click).

The proven RF433 bit-bang logic is reused verbatim from fog-controller.py
(imported via importlib; RPi.GPIO works as the pi user via the gpio group, so no
sudo/subprocess). DB logging via PyMySQL, auto-fog via a background thread.
API is byte-for-byte compatible with the old server.js (nginx /proxy/fog/ keeps
working). SAFETY: fog is never auto-started at boot; auto-fog is opt-in and
self-disables after one hour, exactly as before.
"""

import datetime
import importlib.util
import json
import os
import sqlite3
import threading
import time

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    pymysql = None

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "5003"))
CONFIG_FILE = os.path.join(HERE, "fog-config.json")

DB_CFG = dict(
    host=os.environ.get("DB_HOST", "127.0.0.1"),
    user=os.environ.get("DB_USER", "fog_user"),
    password=os.environ.get("DB_PASSWORD", "fog_password"),
    database=os.environ.get("DB_NAME", "fog_controller"),
)

# ---- RF433 (reuse proven module; hyphenated filename → importlib) ------------
_spec = importlib.util.spec_from_file_location(
    "fog_rf", os.path.join(HERE, "fog-controller.py"))
fog_rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fog_rf)

_rf_lock = threading.Lock()
_rf = None


def _get_rf():
    global _rf
    if _rf is None:
        _rf = fog_rf.RF433Controller(gpio_pin=17)
    return _rf


def _rf_send(command, code=None):
    global _rf
    with _rf_lock:
        try:
            ctrl = _get_rf()
            if command == "on":
                ctrl.turn_on()
            elif command == "off":
                ctrl.turn_off()
            elif command == "custom":
                ctrl.send_custom_code(code)
        except Exception:
            try:
                if _rf:
                    _rf.cleanup()
            except Exception:
                pass
            _rf = None
            raise


# ---- config -----------------------------------------------------------------
DEFAULT_CONFIG = {
    "pythonScriptPath": os.path.join(HERE, "fog-controller.py"),
    "lastCommand": None,
    "fogActive": False,
    "lastActivated": None,
    "activationCount": 0,
    "fogSecondsTotal": 0.0,
}
_cfg_lock = threading.Lock()


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULT_CONFIG)


config = load_config()


def save_config():
    with _cfg_lock:
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
        except OSError:
            pass


# ---- database (PyMySQL, lazy + reconnect) -----------------------------------
_db_lock = threading.Lock()
_db = None
_db_ok = pymysql is not None


def _db_conn():
    global _db
    if pymysql is None:
        return None
    if _db is None:
        _db = pymysql.connect(autocommit=True,
                              cursorclass=pymysql.cursors.DictCursor, **DB_CFG)
    else:
        _db.ping(reconnect=True)
    return _db


def init_db():
    global _db_ok
    if pymysql is None:
        print("⚠️  PyMySQL missing — running without database logging")
        _db_ok = False
        return
    try:
        with _db_lock:
            conn = _db_conn()
            with conn.cursor() as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS fog_activations (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        type ENUM('manual', 'auto') DEFAULT 'manual',
                        duration INT DEFAULT 0,
                        INDEX idx_timestamp (timestamp)
                    )
                """)
        print("📊 MySQL database connected successfully")
        _db_ok = True
    except Exception as e:
        print(f"❌ MySQL connection failed: {e} — running without DB logging")
        _db_ok = False


def log_activation(kind="manual"):
    if not _db_ok:
        return
    try:
        with _db_lock:
            conn = _db_conn()
            with conn.cursor() as c:
                c.execute("INSERT INTO fog_activations (type) VALUES (%s)", (kind,))
    except Exception as e:
        print(f"❌ Failed to log activation: {e}")


def usage_analytics():
    if not _db_ok:
        return {"hourlyData": [], "peakHour": None}
    try:
        with _db_lock:
            conn = _db_conn()
            with conn.cursor() as c:
                c.execute("""
                    SELECT HOUR(timestamp) AS hour, COUNT(*) AS count
                    FROM fog_activations
                    WHERE timestamp >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                    GROUP BY HOUR(timestamp) ORDER BY hour
                """)
                rows = {r["hour"]: r["count"] for r in c.fetchall()}
                hourly = [{"hour": i, "count": rows.get(i, 0)} for i in range(24)]
                c.execute("""
                    SELECT HOUR(timestamp) AS hour, COUNT(*) AS count
                    FROM fog_activations
                    WHERE timestamp >= DATE_SUB(NOW(), INTERVAL 7 DAY)
                    GROUP BY HOUR(timestamp) ORDER BY count DESC LIMIT 1
                """)
                peak = c.fetchone()
        return {"hourlyData": hourly,
                "peakHour": f"{peak['hour']}:00" if peak else None}
    except Exception as e:
        print(f"❌ Failed to get analytics: {e}")
        return {"hourlyData": [], "peakHour": None}


# ---- tank / fluid tracking (SQLite, self-calibrating) -----------------------
# Each fog "on" is one discrete burst (~fixed fluid). We don't know the exact
# ml-per-burst up front, so we learn it: every activation since the last refill
# is counted; when the user refills and tells us how empty the tank was, the
# finished cycle yields a sample (consumed / activations) that updates the
# per-activation estimate via an EWMA. Live level is derived, never polled.
TANK_DB = os.path.join(HERE, "fog-tank.db")
TANK_DEFAULT_CAPACITY = 250.0          # ml — Katomi 500W class; editable in GUI
TANK_SEED_ML_PER_ACT = TANK_DEFAULT_CAPACITY / 25.0   # ~10 ml until calibrated
_CAL_ALPHA = 0.5                       # EWMA weight for a normal refill sample
_CAL_ALPHA_EMPTY = 0.7                 # stronger weight when the tank ran empty
_tank_lock = threading.Lock()
_tank = None


def _tank_conn():
    global _tank
    if _tank is None:
        _tank = sqlite3.connect(TANK_DB, check_same_thread=False)
        _tank.row_factory = sqlite3.Row
    return _tank


def init_tank_db():
    with _tank_lock:
        conn = _tank_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tank (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                capacity_ml REAL NOT NULL DEFAULT 250,
                level_at_refill_ml REAL NOT NULL DEFAULT 250,
                activations_since_refill INTEGER NOT NULL DEFAULT 0,
                ml_per_activation REAL NOT NULL DEFAULT 10,
                calibrated INTEGER NOT NULL DEFAULT 0,
                last_refill_at TEXT,
                ml_per_second REAL,
                seconds_since_refill REAL NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS refills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                level_before_ml REAL,
                remaining_ml REAL,
                amount_added_ml REAL,
                level_after_ml REAL,
                activations_in_cycle INTEGER,
                ml_per_act_sample REAL,
                was_empty INTEGER NOT NULL DEFAULT 0,
                fog_seconds_in_cycle REAL,
                ml_per_s_sample REAL
            )
        """)
        # Lazy-Migration fuer Bestands-DBs (2026-08-14: Zeit-Modell)
        for ddl in (
            "ALTER TABLE tank ADD COLUMN ml_per_second REAL",
            "ALTER TABLE tank ADD COLUMN seconds_since_refill REAL NOT NULL DEFAULT 0",
            "ALTER TABLE refills ADD COLUMN fog_seconds_in_cycle REAL",
            "ALTER TABLE refills ADD COLUMN ml_per_s_sample REAL",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass   # Spalte existiert schon
        if conn.execute("SELECT id FROM tank WHERE id = 1").fetchone() is None:
            conn.execute(
                "INSERT INTO tank (id, capacity_ml, level_at_refill_ml, "
                "ml_per_activation) VALUES (1, ?, ?, ?)",
                (TANK_DEFAULT_CAPACITY, TANK_DEFAULT_CAPACITY, TANK_SEED_ML_PER_ACT))
        conn.commit()
    print("💧 Tank SQLite DB ready")


def _level_from_row(row, pending_s=0.0):
    """Restpegel. Seit 2026-08-14 ZEIT-basiert, sobald ml_per_second
    kalibriert ist (Nachgiessen mit on/off-Messung) — die Aktivierungs-
    Schaetzung bleibt der Fallback fuer unkalibrierte Bestaende.
    pending_s = laufende, noch ungebuchte ON-Phase (Live-Anzeige)."""
    mps = row["ml_per_second"] if "ml_per_second" in row.keys() else None
    if mps:
        used = (row["seconds_since_refill"] + pending_s) * mps
    else:
        used = row["activations_since_refill"] * row["ml_per_activation"]
    lvl = row["level_at_refill_ml"] - used
    return max(0.0, min(row["capacity_ml"], lvl))


def tank_state():
    with _tank_lock:
        row = _tank_conn().execute("SELECT * FROM tank WHERE id = 1").fetchone()
    cap = row["capacity_ml"]
    pending = _fog_pending_s()
    level = _level_from_row(row, pending)
    mpa = row["ml_per_activation"]
    mps = row["ml_per_second"] if "ml_per_second" in row.keys() else None
    secs = (row["seconds_since_refill"]
            if "seconds_since_refill" in row.keys() else 0.0) or 0.0
    return {
        "capacity_ml": round(cap, 1),
        "level_ml": round(level, 1),
        "level_pct": round(level / cap * 100) if cap > 0 else 0,
        "ml_per_activation": round(mpa, 2),
        "ml_per_second": round(mps, 3) if mps else None,
        "activations_since_refill": row["activations_since_refill"],
        "fog_seconds_since_refill": round(secs + pending, 1),
        "est_activations_remaining": int(level / mpa) if mpa > 0 else None,
        "est_seconds_remaining": int(level / mps) if mps else None,
        "calibrated": bool(row["calibrated"]),
        "last_refill_at": row["last_refill_at"],
    }


def tank_bump_activation():
    """+1 burst on the current cycle (independent of the resettable config count)."""
    try:
        with _tank_lock:
            conn = _tank_conn()
            conn.execute("UPDATE tank SET activations_since_refill = "
                         "activations_since_refill + 1 WHERE id = 1")
            conn.commit()
    except Exception as e:
        print(f"❌ Tank activation bump failed: {e}")


def tank_bump_seconds(s):
    """Gemessene ON-Sekunden auf den laufenden Zyklus buchen."""
    if s <= 0:
        return
    with _tank_lock:
        conn = _tank_conn()
        conn.execute("UPDATE tank SET seconds_since_refill = "
                     "seconds_since_refill + ? WHERE id = 1", (float(s),))
        conn.commit()


# ---- ON-Zeit-Messung (2026-08-14) -------------------------------------------
# Exakte Zeit zwischen "on" und "off" — der Nutzerfrage geschuldet ("nimmst
# du die zeit genau?"): vorher zaehlte nur die Aktivierung, ein 3-s- und ein
# 30-s-Burst galten gleich. Gecappt bei FOG_BURST_CAP_S, weil die Maschine
# einen Burst nach ~20 s SELBST beendet (Handbuch: "ca. 20 Sekunden") und
# Auto-Fog nie "off" sendet — ohne Cap zaehlte ein vergessenes Off endlos.
FOG_BURST_CAP_S = float(os.environ.get("FOG_BURST_CAP_S", "20"))
_fog_on_ts = None


def _fog_pending_s(now=None):
    """Laufende, noch ungebuchte ON-Phase (fuer Live-Anzeige/Level)."""
    if _fog_on_ts is None:
        return 0.0
    return min(max(0.0, (now or time.time()) - _fog_on_ts), FOG_BURST_CAP_S)


def _book_fog_time(now=None):
    """ON-Phase abschliessen: Sekunden kumulativ (config) + Zyklus (tank)."""
    global _fog_on_ts
    if _fog_on_ts is None:
        return 0.0
    dur = _fog_pending_s(now)
    _fog_on_ts = None
    config["fogSecondsTotal"] = float(config.get("fogSecondsTotal", 0.0)) + dur
    tank_bump_seconds(dur)
    return dur


def tank_refill(full=False, amount_ml=None, remaining_ml=None, was_empty=False):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _tank_lock:
        conn = _tank_conn()
        row = conn.execute("SELECT * FROM tank WHERE id = 1").fetchone()
        cap = row["capacity_ml"]
        level_at_refill = row["level_at_refill_ml"]
        acts = row["activations_since_refill"]
        old_mpa = row["ml_per_activation"]
        calibrated = row["calibrated"]
        level_before = _level_from_row(row)

        # Ground-truth remaining fluid before topping up (user feedback).
        if was_empty:
            remaining = 0.0
        elif remaining_ml is not None:
            remaining = max(0.0, min(cap, float(remaining_ml)))
        else:
            remaining = level_before          # no info → trust the estimate

        # Calibration sample from the cycle that just ended.
        consumed = level_at_refill - remaining
        sample = consumed / acts if acts > 0 and consumed > 0 else None
        if sample is not None:
            if not calibrated:
                new_mpa = sample
            else:
                alpha = _CAL_ALPHA_EMPTY if was_empty else _CAL_ALPHA
                new_mpa = alpha * sample + (1 - alpha) * old_mpa
            new_cal = 1
        else:
            new_mpa, new_cal = old_mpa, calibrated

        # New level after topping up.
        if full or amount_ml is None:
            level_after = cap
        else:
            level_after = max(0.0, min(cap, remaining + float(amount_ml)))
        added = level_after - remaining

        conn.execute(
            "UPDATE tank SET level_at_refill_ml = ?, activations_since_refill = 0, "
            "seconds_since_refill = 0, "
            "ml_per_activation = ?, calibrated = ?, last_refill_at = ? WHERE id = 1",
            (level_after, new_mpa, new_cal, now))
        conn.execute(
            "INSERT INTO refills (ts, level_before_ml, remaining_ml, amount_added_ml, "
            "level_after_ml, activations_in_cycle, ml_per_act_sample, was_empty) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now, round(level_before, 1), round(remaining, 1), round(added, 1),
             round(level_after, 1), acts,
             round(sample, 3) if sample is not None else None, 1 if was_empty else 0))
        conn.commit()
    return tank_state()


def tank_set_capacity(new_cap):
    new_cap = max(50.0, min(5000.0, float(new_cap)))
    with _tank_lock:
        conn = _tank_conn()
        row = conn.execute("SELECT * FROM tank WHERE id = 1").fetchone()
        level = min(_level_from_row(row), new_cap)
        conn.execute("UPDATE tank SET capacity_ml = ?, level_at_refill_ml = ?, "
                     "activations_since_refill = 0 WHERE id = 1", (new_cap, level))
        conn.commit()
    return tank_state()


def tank_history(limit=10):
    with _tank_lock:
        rows = _tank_conn().execute(
            "SELECT * FROM refills ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---- core command -----------------------------------------------------------
def execute_command(command, kind="manual"):
    if command not in ("on", "off"):
        raise ValueError('Invalid command. Must be "on" or "off"')
    _rf_send(command)
    config["lastCommand"] = command
    global _fog_on_ts
    if command == "on":
        _book_fog_time()               # Auto-Wiederholung: alten Burst buchen
        _fog_on_ts = time.time()
        config["fogActive"] = True
        config["lastActivated"] = datetime.datetime.now(
            datetime.timezone.utc).isoformat()
        config["activationCount"] += 1
        log_activation(kind)
        tank_bump_activation()
    else:
        _book_fog_time()
        config["fogActive"] = False
    save_config()


# ---- auto-fog (background thread; self-disables after 1 h) -------------------
auto = {"active": False, "interval": 5, "startTime": None,
        "autoDisableTime": None, "_stop": None}


def start_auto_fog(interval):
    if auto["active"]:
        return
    auto["active"] = True
    auto["interval"] = interval
    auto["startTime"] = int(time.time() * 1000)
    auto["autoDisableTime"] = int((time.time() + 3600) * 1000)
    stop = threading.Event()
    auto["_stop"] = stop

    def loop():
        deadline = time.time() + 3600          # auto-disable after 1 hour
        while auto["active"] and not stop.wait(interval * 60):
            if time.time() >= deadline:
                break
            try:
                execute_command("on", "auto")
            except Exception as e:
                print(f"❌ Auto-Fog execution failed: {e}")
        stop_auto_fog()

    threading.Thread(target=loop, daemon=True).start()


def stop_auto_fog():
    auto["active"] = False
    auto["startTime"] = None
    auto["autoDisableTime"] = None
    if auto.get("_stop"):
        auto["_stop"].set()


# ---- refill calibration (Nachgiess-Kalibrierung, 2026-08-14) -----------------
# Replaces the scale flow (git history has it): fill the tank to the BRIM,
# fog normally through the app/auto for a while (the controller counts its
# own activations), then refill to the brim again and enter the refilled ml.
# sample = ml / activations REPLACES the EWMA estimate outright (a measured
# top-up is ground truth, not feedback to be blended), and the level is set
# to the capacity — brim-full is the whole point of the method: the SAME
# reproducible reference at both ends, no kitchen scale, the machine stays
# in place (220 V, hot, cabled — lifting it onto a scale was the old flow's
# weak spot).  ⚠️ The machine's own RF remote is INVISIBLE to the Pi — fog
# triggered with it is consumed but not counted, so the GUI warns and the
# plausibility check calls it out.  Starting BOOKS the brim-full level at
# once (it is a fact, not part of the measurement); an abort discards only
# the measurement state (in-memory), never that level.  The cumulative config["activationCount"] is
# the counter snapshot — no extra hook in the activation path needed, and a
# normal /api/tank/refill during a run cannot corrupt it (that only resets
# the per-cycle counter).
_CAL_MIN_SAMPLE = 0.5                  # ml/activation — below: implausible
_CAL_MAX_SAMPLE = 200.0
_CAL_MIN_ACTS = 3                      # fewer bursts = no trustworthy sample
_CAL_MIN_FOG_S = 10.0                  # ab hier traegt das ZEIT-Sample (ml/s)
_CAL_MIN_MPS = 0.01                    # ml/s-Plausibilitaet (500-W-Klasse
_CAL_MAX_MPS = 2.0                     # liegt bei ~0,08; grosszuegig geklemmt)
_cal_lock = threading.Lock()
_cal = {"phase": "idle"}               # idle|fogging (+ fields)


def refill_cal_compute(added_ml, acts, secs, capacity_ml):
    """Pure math + validation: (ml_je_aktivierung | None, ml_je_sekunde | None).

    Zeit-Sample (ml/s aus der EXAKT gemessenen on/off-Zeit) ist das primaere
    Ergebnis; das Aktivierungs-Sample bleibt als Fallback/Zusatzinfo. Raises
    ValueError mit deutschem Klartext — die GUI zeigt ihn woertlich."""
    added_ml = float(added_ml)
    acts = int(acts)
    secs = float(secs)
    capacity_ml = float(capacity_ml)
    if added_ml <= 0:
        raise ValueError("Nachgefüllte Menge muss größer als 0 ml sein")
    if added_ml > capacity_ml:
        raise ValueError(
            f"{added_ml:.0f} ml nachgefüllt, aber der Tank fasst nur "
            f"{capacity_ml:.0f} ml — Tankgröße prüfen oder Eingabe "
            "korrigieren")
    if secs < _CAL_MIN_FOG_S and acts < _CAL_MIN_ACTS:
        raise ValueError(
            f"Zu wenig genebelt für eine belastbare Zahl (nur {acts} "
            f"Aktivierung(en), {secs:.0f} s Nebelzeit) — mindestens "
            f"{_CAL_MIN_ACTS} Aktivierungen oder {_CAL_MIN_FOG_S:.0f} s "
            "über App/Auto nebeln, dann abschließen.")
    mps = None
    if secs >= _CAL_MIN_FOG_S:
        mps = added_ml / secs
        if not (_CAL_MIN_MPS <= mps <= _CAL_MAX_MPS):
            raise ValueError(
                f"Unplausibler Verbrauch ({mps:.2f} ml je Sekunde; die "
                "500-W-Klasse liegt bei ~0,05–0,3) — wurde zwischendurch "
                "mit der Funk-Fernbedienung der Maschine genebelt? Die "
                "sieht der Pi nicht; nur App-/Auto-Nebel zählt.")
    sample = added_ml / acts if acts >= 1 else None
    if mps is None and sample is not None \
            and not (_CAL_MIN_SAMPLE <= sample <= _CAL_MAX_SAMPLE):
        raise ValueError(
            f"Unplausibler Verbrauch ({sample:.1f} ml je Aktivierung) — "
            "wurde zwischendurch mit der Funk-Fernbedienung der Maschine "
            "genebelt? Die sieht der Pi nicht; nur App-/Auto-Nebel zählt.")
    return sample, mps


def refill_cal_state():
    with _cal_lock:
        st = dict(_cal)
    if st.get("phase") == "fogging":
        st["acts"] = max(0, config["activationCount"] - st.pop("acts0"))
        st["fog_s"] = round(max(0.0, float(config.get("fogSecondsTotal", 0.0))
                                 + _fog_pending_s() - st.pop("secs0")), 1)
        st["elapsed_s"] = max(0, int(time.time() - st.pop("t0")))
    return st


def refill_cal_start(capacity_ml=None):
    """User confirms the tank is BRIM-FULL right now; the optional capacity
    value updates the reference for "randvoll" in the same step (the size is
    what unlocks the mechanism — without it "brim-full" means nothing)."""
    with _cal_lock:
        if _cal.get("phase") not in (None, "idle"):
            raise ValueError("Kalibrierung läuft bereits")
    if capacity_ml is not None and str(capacity_ml).strip() != "":
        tank_set_capacity(float(capacity_ml))   # clamps 50..5000 + persists
    cap = tank_state()["capacity_ml"]
    if not cap or cap <= 0:
        raise ValueError("Erst die Tankgröße angeben — sie ist die Referenz "
                         "für „randvoll“")
    _book_fog_time()   # offene ON-Phase gehoert noch zum ALTEN Zyklus
    # Der Start-Schritt bestaetigt einen FAKT ("Tank ist randvoll") — der
    # wird sofort gebucht, sonst zeigte die Anzeige bis zum Abschluss den
    # alten (falschen) Stand. Ein Abort verwirft nur die MESSUNG; der
    # Randvoll-Stand bleibt zu Recht stehen. Die Historien-Zeile kommt
    # erst beim Finish (mit den gemessenen Samples).
    with _tank_lock:
        conn = _tank_conn()
        conn.execute("UPDATE tank SET level_at_refill_ml = capacity_ml, "
                     "activations_since_refill = 0, seconds_since_refill = 0 "
                     "WHERE id = 1")
        conn.commit()
    with _cal_lock:
        _cal.clear()
        _cal.update({"phase": "fogging", "acts0": config["activationCount"],
                     "secs0": float(config.get("fogSecondsTotal", 0.0)),
                     "t0": time.time(), "capacity_ml": cap})
    return refill_cal_state()


def refill_cal_finish(added_ml):
    """Refilled to the brim: commit calibration + refill atomically."""
    with _cal_lock:
        if _cal.get("phase") != "fogging":
            raise ValueError("Keine laufende Kalibrierung")
        st = dict(_cal)
    _book_fog_time()   # laufende ON-Phase noch in DIESEN Lauf buchen
    acts = max(0, config["activationCount"] - st["acts0"])
    secs = max(0.0, float(config.get("fogSecondsTotal", 0.0)) - st["secs0"])
    cap = tank_state()["capacity_ml"]  # live — Größe kann im Lauf geändert sein
    sample, mps = refill_cal_compute(added_ml, acts, secs, cap)
    added = float(added_ml)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _tank_lock:
        conn = _tank_conn()
        row = conn.execute("SELECT * FROM tank WHERE id = 1").fetchone()
        cap = row["capacity_ml"]
        old_mpa = row["ml_per_activation"]
        # Brim-full is the absolute reference: level = capacity, counters = 0.
        # ml_per_second wird auch auf None gesetzt, wenn der Lauf zu kurz fuer
        # ein Zeit-Sample war — Kalibrierung ERSETZT, sie mischt nicht.
        conn.execute(
            "UPDATE tank SET level_at_refill_ml = ?, activations_since_refill = 0, "
            "seconds_since_refill = 0, ml_per_activation = ?, ml_per_second = ?, "
            "calibrated = 1, last_refill_at = ? WHERE id = 1",
            (cap, sample if sample is not None else old_mpa, mps, now))
        conn.execute(
            "INSERT INTO refills (ts, level_before_ml, remaining_ml, amount_added_ml, "
            "level_after_ml, activations_in_cycle, ml_per_act_sample, was_empty, "
            "fog_seconds_in_cycle, ml_per_s_sample) "
            "VALUES (?,?,?,?,?,?,?,0,?,?)",
            (now, round(cap, 1), round(max(0.0, cap - added), 1),
             round(added, 1), round(cap, 1), acts,
             round(sample, 3) if sample is not None else None,
             round(secs, 1), round(mps, 4) if mps is not None else None))
        conn.commit()
    with _cal_lock:
        _cal.clear()
        _cal.update({"phase": "idle"})
    return {"acts": acts, "fog_seconds": round(secs, 1),
            "added_ml": round(added, 1),
            "ml_per_activation_old": round(old_mpa, 2),
            "ml_per_activation": round(sample, 2) if sample is not None else None,
            "ml_per_second": round(mps, 3) if mps is not None else None,
            **tank_state()}


def refill_cal_abort():
    with _cal_lock:
        _cal.clear()
        _cal.update({"phase": "idle"})
    return refill_cal_state()


# ---- Flask app --------------------------------------------------------------
app = Flask(__name__, static_folder=None)
CORS(app)


@app.route("/")
def index():
    return send_from_directory(os.path.join(HERE, "public"), "index.html")


@app.route("/api/health")
def health():
    return jsonify(status="ok", service="Fog Controller", port=PORT,
                   timestamp=datetime.datetime.now(
                       datetime.timezone.utc).isoformat())


@app.route("/api/status")
def status():
    a = usage_analytics()
    return jsonify(fogActive=config["fogActive"], lastCommand=config["lastCommand"],
                   lastActivated=config["lastActivated"],
                   activationCount=config["activationCount"],
                   peakHour=a["peakHour"],
                   timestamp=datetime.datetime.now(
                       datetime.timezone.utc).isoformat())


@app.route("/api/fog/on", methods=["POST"])
def fog_on():
    try:
        execute_command("on")
        return jsonify(success=True, message="Fog machine turned ON", status="on")
    except Exception as e:
        return jsonify(success=False, error="Failed to turn on fog machine",
                       details=str(e)), 500


@app.route("/api/fog/off", methods=["POST"])
def fog_off():
    try:
        execute_command("off")
        return jsonify(success=True, message="Fog machine turned OFF", status="off")
    except Exception as e:
        return jsonify(success=False, error="Failed to turn off fog machine",
                       details=str(e)), 500


@app.route("/api/fog/toggle", methods=["POST"])
def fog_toggle():
    try:
        new_state = "off" if config["fogActive"] else "on"
        execute_command(new_state)
        return jsonify(success=True,
                       message=f"Fog machine toggled to {new_state.upper()}",
                       status=new_state)
    except Exception as e:
        return jsonify(success=False, error="Failed to toggle fog machine",
                       details=str(e)), 500


@app.route("/api/stats/reset", methods=["POST"])
def stats_reset():
    config["activationCount"] = 0
    config["lastActivated"] = None
    save_config()
    return jsonify(success=True, message="Statistics reset successfully")


@app.route("/api/fog/custom", methods=["POST"])
def fog_custom():
    data = request.get_json(silent=True) or {}
    code = data.get("code")
    if not code:
        return jsonify(success=False, error="Code parameter required"), 400
    try:
        int(code, 16)                          # hex-only validation
    except (ValueError, TypeError):
        return jsonify(success=False,
                       error="Invalid code format. Only hexadecimal characters allowed"), 400
    try:
        _rf_send("custom", int(code, 16))
        return jsonify(success=True, message="Custom code sent successfully", code=code)
    except Exception as e:
        return jsonify(success=False, error="Failed to send custom code",
                       details=str(e)), 500


@app.route("/api/auto-fog/status")
def auto_status():
    return jsonify(active=auto["active"], interval=auto["interval"],
                   startTime=auto["startTime"], autoDisableTime=auto["autoDisableTime"],
                   timestamp=datetime.datetime.now(
                       datetime.timezone.utc).isoformat())


@app.route("/api/auto-fog/enable", methods=["POST"])
def auto_enable():
    data = request.get_json(silent=True) or {}
    try:
        interval = int(data.get("interval"))
    except (TypeError, ValueError):
        interval = None
    if interval not in (5, 15, 30, 60, 120):
        return jsonify(success=False,
                       error="Invalid interval. Must be 5, 15, 30, 60, or 120 minutes"), 400
    try:
        start_auto_fog(interval)
        return jsonify(success=True,
                       message=f"Auto-Fog enabled with {interval} minute interval",
                       interval=interval)
    except Exception as e:
        return jsonify(success=False, error="Failed to enable Auto-Fog",
                       details=str(e)), 500


@app.route("/api/auto-fog/disable", methods=["POST"])
def auto_disable():
    try:
        stop_auto_fog()
        return jsonify(success=True, message="Auto-Fog disabled")
    except Exception as e:
        return jsonify(success=False, error="Failed to disable Auto-Fog",
                       details=str(e)), 500


@app.route("/api/analytics/usage")
def analytics_usage():
    try:
        return jsonify(success=True, **usage_analytics())
    except Exception as e:
        return jsonify(success=False, error="Failed to get analytics",
                       details=str(e)), 500


@app.route("/api/tank")
def tank_get():
    try:
        return jsonify(success=True, **tank_state())
    except Exception as e:
        return jsonify(success=False, error="Failed to read tank state",
                       details=str(e)), 500


@app.route("/api/tank/refill", methods=["POST"])
def tank_refill_route():
    data = request.get_json(silent=True) or {}

    def _num(key):
        v = data.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    try:
        state = tank_refill(full=bool(data.get("full")),
                            amount_ml=_num("amount_ml"),
                            remaining_ml=_num("remaining_ml"),
                            was_empty=bool(data.get("was_empty")))
        return jsonify(success=True, message="Refill recorded", **state)
    except Exception as e:
        return jsonify(success=False, error="Failed to record refill",
                       details=str(e)), 500


@app.route("/api/tank/config", methods=["POST"])
def tank_config_route():
    data = request.get_json(silent=True) or {}
    try:
        cap = float(data.get("capacity_ml"))
    except (TypeError, ValueError):
        return jsonify(success=False, error="capacity_ml (number) required"), 400
    if not (50 <= cap <= 5000):
        return jsonify(success=False,
                       error="capacity_ml must be between 50 and 5000 ml"), 400
    try:
        return jsonify(success=True, message="Capacity updated",
                       **tank_set_capacity(cap))
    except Exception as e:
        return jsonify(success=False, error="Failed to set capacity",
                       details=str(e)), 500


@app.route("/api/tank/calibrate", methods=["GET"])
def refill_cal_state_route():
    return jsonify(success=True, **refill_cal_state())


@app.route("/api/tank/calibrate/start", methods=["POST"])
def refill_cal_start_route():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(success=True,
                       **refill_cal_start(data.get("capacity_ml")))
    except (ValueError, TypeError) as e:
        return jsonify(success=False, error=str(e) or "Ungültige Eingabe"), 400
    except Exception as e:
        return jsonify(success=False, error="Start fehlgeschlagen",
                       details=str(e)), 500


@app.route("/api/tank/calibrate/finish", methods=["POST"])
def refill_cal_finish_route():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(success=True, **refill_cal_finish(data.get("added_ml")))
    except (ValueError, TypeError) as e:
        return jsonify(success=False, error=str(e) or "Ungültige Eingabe"), 400
    except Exception as e:
        return jsonify(success=False, error="Abschluss fehlgeschlagen",
                       details=str(e)), 500


@app.route("/api/tank/calibrate/abort", methods=["POST"])
def refill_cal_abort_route():
    return jsonify(success=True, **refill_cal_abort())


@app.route("/api/tank/history")
def tank_history_route():
    try:
        return jsonify(success=True, refills=tank_history())
    except Exception as e:
        return jsonify(success=False, error="Failed to read history",
                       details=str(e)), 500


@app.route("/<path:p>")
def static_files(p):
    return send_from_directory(os.path.join(HERE, "public"), p)


init_db()
init_tank_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
