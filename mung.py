from flask import Flask, render_template, jsonify
import requests
import sqlite3
import threading
import time
import os
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

# ------------------------------------------------------------------
# Blynk Configuration  (reads from environment variable on Render,
# falls back to the hardcoded value for local testing)
# ------------------------------------------------------------------
BLYNK_AUTH_TOKEN = os.environ.get(
    "BLYNK_AUTH_TOKEN",
    "auWNqgLcgtwQ6lQCbhQzCJbeoANRG3KF"   # local fallback only
)
BLYNK_SERVER = "https://blynk.cloud/external/api"
DB_NAME = "mung_bean_history.db"

POLL_INTERVAL = 1.0     # backend reads Blynk every 1 s
LOG_INTERVAL  = 10.0    # SQLite insert every 10 s

VIRTUAL_PINS = {
    "temp":     "V0",
    "humidity": "V1",
    "moisture": "V2",
    "height":   "V3",
    "pump":     "V4",
    "fan":      "V5",
    "light":    "V6",
}

latest_data = {
    "temp": 0.0, "humidity": 0.0, "moisture": 0, "height": 0.0,
    "pump": 0, "fan": 0, "light": 0,
    "online": False,
    "cloud_ok": False,
    "updated": None,
    "status": "checking",
    "status_message": "Initializing...",
}
data_lock = threading.Lock()

# Retrying HTTP session
_session = requests.Session()
_retry = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["GET"],
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))


# ------------------------------------------------------------------
# Database
# ------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sensor_logs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     TEXT    NOT NULL,
            temperature   REAL,
            humidity      REAL,
            moisture      INTEGER,
            plant_height  REAL,
            pump_state    INTEGER,
            fan_state     INTEGER,
            light_state   INTEGER
        )
    ''')
    conn.commit()
    conn.close()


def log_to_db(data):
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.execute('''
            INSERT INTO sensor_logs
            (timestamp, temperature, humidity, moisture, plant_height,
             pump_state, fan_state, light_state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            data["temp"], data["humidity"], data["moisture"], data["height"],
            data["pump"], data["fan"], data["light"],
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Database insertion error: {e}")


# ------------------------------------------------------------------
# Blynk helpers
# ------------------------------------------------------------------
def fetch_blynk_pins(pins):
    url = f"{BLYNK_SERVER}/get?token={BLYNK_AUTH_TOKEN}&" + "&".join(pins)
    try:
        r = _session.get(url, timeout=(3, 6))
        if r.status_code != 200:
            print(f"Blynk returned HTTP {r.status_code}")
            return None
        raw = r.json()
        if not isinstance(raw, dict):
            raw = {pins[0]: raw}
        return {p: float(raw.get(p, 0)) for p in pins}
    except Exception as e:
        print(f"Error fetching pins: {e}")
        return None


def fetch_device_online():
    url = f"{BLYNK_SERVER}/isHardwareConnected?token={BLYNK_AUTH_TOKEN}"
    try:
        r = _session.get(url, timeout=(3, 6))
        if r.status_code != 200:
            return None
        return r.text.strip().lower() == "true"
    except Exception as e:
        print(f"Error checking device status: {e}")
        return None


# ------------------------------------------------------------------
# Background poller
# ------------------------------------------------------------------
def poller():
    last_log = 0.0
    while True:
        started = time.time()
        device_online = fetch_device_online()
        values = fetch_blynk_pins(list(VIRTUAL_PINS.values())) if device_online else None

        with data_lock:
            if device_online is None or (device_online and values is None):
                latest_data["cloud_ok"] = False
                latest_data["online"] = False
                latest_data["status"] = "cloud_error"
                latest_data["status_message"] = "Cannot reach Blynk cloud"
            elif not device_online:
                latest_data.update({
                    "pump": 0, "fan": 0, "light": 0,
                    "online": False,
                    "cloud_ok": True,
                    "status": "offline",
                    "status_message": "Device not connected",
                })
            else:
                latest_data.update({
                    "temp":     values[VIRTUAL_PINS["temp"]],
                    "humidity": values[VIRTUAL_PINS["humidity"]],
                    "moisture": int(values[VIRTUAL_PINS["moisture"]]),
                    "height":   values[VIRTUAL_PINS["height"]],
                    "pump":     int(values[VIRTUAL_PINS["pump"]]),
                    "fan":      int(values[VIRTUAL_PINS["fan"]]),
                    "light":    int(values[VIRTUAL_PINS["light"]]),
                    "online": True,
                    "cloud_ok": True,
                    "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "online",
                    "status_message": "All systems normal",
                })
            snapshot = dict(latest_data)

        if snapshot["online"] and time.time() - last_log >= LOG_INTERVAL:
            log_to_db(snapshot)
            last_log = time.time()

        elapsed = time.time() - started
        time.sleep(max(0.0, POLL_INTERVAL - elapsed))


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def get_data():
    with data_lock:
        return jsonify(dict(latest_data))


@app.route("/api/history")
def get_history():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT timestamp, temperature, humidity, plant_height
        FROM sensor_logs
        ORDER BY id DESC LIMIT 50
    ''')
    rows = cursor.fetchall()
    conn.close()
    return jsonify([
        {"timestamp": r[0], "temp": r[1], "humidity": r[2], "height": r[3]}
        for r in reversed(rows)
    ])


# ------------------------------------------------------------------
# Entry point — works both locally (python mung.py) and on Render
# (gunicorn mung:app). The poller is started once, guarded by an
# environment flag so it doesn't spawn multiple threads under
# multi-worker servers.
# ------------------------------------------------------------------
init_db()

if os.environ.get("START_POLLER", "1") == "1":
    threading.Thread(target=poller, daemon=True).start()
    os.environ["START_POLLER"] = "0"

# ------------------------------------------------------------------
# Startup — runs on import (works under gunicorn) AND locally
# ------------------------------------------------------------------
init_db()

# Start the background poller exactly once.
# gunicorn imports this module; __name__ is "mung", not "__main__".
# The START_POLLER env guard prevents duplicate threads if gunicorn
# spawns multiple workers.
if os.environ.get("START_POLLER", "1") == "1":
    threading.Thread(target=poller, daemon=True).start()
    os.environ["START_POLLER"] = "0"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)