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


@app.route("/<path:p>")
def static_files(p):
    return send_from_directory(os.path.join(HERE, "public"), p)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
