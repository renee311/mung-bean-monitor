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
# Blynk Configuration
# ------------------------------------------------------------------
BLYNK_AUTH_TOKEN = os.environ.get("BLYNK_AUTH_TOKEN", "")
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

# Retrying HTTP session for Blynk
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
            print(f"Blynk returned HTTP {r.status_code}: {r.text[:120]}")
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
            print(f"Blynk (isHardwareConnected) HTTP {r.status_code}: {r.text[:120]}")
            return None
        return r.text.strip().lower() == "true"
    except Exception as e:
        print(f"Error checking device status: {e}")
        return None


# ------------------------------------------------------------------
# Background poller
# ------------------------------------------------------------------
def poller():
    print("[poller] thread started")
    last_log = 0.0
    while True:
        try:
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

        except Exception as e:
            # Never let the poller thread die — keep it alive no matter what
            print(f"[poller] unexpected error: {e}")
            time.sleep(1.0)


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


@app.route("/api/debug")
def debug_info():
    """Temporary endpoint for diagnosing connectivity."""
    import platform
    info = {
        "python": platform.python_version(),
        "token_set": bool(BLYNK_AUTH_TOKEN),
        "token_prefix": BLYNK_AUTH_TOKEN[:6] + "..." if BLYNK_AUTH_TOKEN else None,
        "blynk_server": BLYNK_SERVER,
    }
    # Try a direct Blynk call
    url = f"{BLYNK_SERVER}/isHardwareConnected?token={BLYNK_AUTH_TOKEN}"
    try:
        r = requests.get(url, timeout=10)
        info["blynk_status_code"] = r.status_code
        info["blynk_body"] = r.text[:200]
    except Exception as e:
        info["blynk_error"] = str(e)

    with data_lock:
        info["latest_data"] = dict(latest_data)
    return jsonify(info)


# ------------------------------------------------------------------
# Startup — runs both locally AND under gunicorn
# ------------------------------------------------------------------
print("[startup] mung.py loaded, __name__ =", __name__)
init_db()
print("[startup] database ready")

# Start the poller exactly once, whether running via `python mung.py`
# or `gunicorn mung:app`. The START_POLLER env guard prevents
# duplicate threads if gunicorn spawns multiple workers.
if os.environ.get("START_POLLER", "1") == "1":
    threading.Thread(target=poller, daemon=True, name="blynk-poller").start()
    os.environ["START_POLLER"] = "0"
    print("[startup] poller thread launched")
else:
    print("[startup] poller already running in another worker, skipping")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)