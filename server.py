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
                last_refill_at TEXT
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
                was_empty INTEGER NOT NULL DEFAULT 0
            )
        """)
        if conn.execute("SELECT id FROM tank WHERE id = 1").fetchone() is None:
            conn.execute(
                "INSERT INTO tank (id, capacity_ml, level_at_refill_ml, "
                "ml_per_activation) VALUES (1, ?, ?, ?)",
                (TANK_DEFAULT_CAPACITY, TANK_DEFAULT_CAPACITY, TANK_SEED_ML_PER_ACT))
        conn.commit()
    print("💧 Tank SQLite DB ready")


def _level_from_row(row):
    lvl = row["level_at_refill_ml"] - row["activations_since_refill"] * row["ml_per_activation"]
    return max(0.0, min(row["capacity_ml"], lvl))


def tank_state():
    with _tank_lock:
        row = _tank_conn().execute("SELECT * FROM tank WHERE id = 1").fetchone()
    cap = row["capacity_ml"]
    level = _level_from_row(row)
    mpa = row["ml_per_activation"]
    return {
        "capacity_ml": round(cap, 1),
        "level_ml": round(level, 1),
        "level_pct": round(level / cap * 100) if cap > 0 else 0,
        "ml_per_activation": round(mpa, 2),
        "activations_since_refill": row["activations_since_refill"],
        "est_activations_remaining": int(level / mpa) if mpa > 0 else None,
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
    if command == "on":
        config["fogActive"] = True
        config["lastActivated"] = datetime.datetime.now(
            datetime.timezone.utc).isoformat()
        config["activationCount"] += 1
        log_activation(kind)
        tank_bump_activation()
    else:
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
