from flask import Flask, render_template, jsonify
import requests
import sqlite3
import threading
import time
import os
import sys
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

# ------------------------------------------------------------------
# Logging helper — forces flush so Render's log viewer shows output
# from daemon threads. Without flush=True, print() in a thread often
# buffers and never appears in the Render log stream.
# ------------------------------------------------------------------
def log(*args):
    try:
        print(*args, flush=True)
        sys.stdout.flush()
    except Exception:
        pass


# ------------------------------------------------------------------
# Blynk Configuration (SECURE — read from environment)
# ------------------------------------------------------------------
BLYNK_AUTH_TOKEN = os.environ.get("BLYNK_AUTH_TOKEN")

if not BLYNK_AUTH_TOKEN:
    raise ValueError(
        "BLYNK_AUTH_TOKEN environment variable is required. "
        "Set it in your Render dashboard (or local shell) before starting the app."
    )

BLYNK_SERVER = "https://blynk.cloud/external/api"

# ------------------------------------------------------------------
# Database path
# ------------------------------------------------------------------
DB_NAME = os.environ.get("DB_PATH", "mung_bean_history.db")

_db_dir = os.path.dirname(DB_NAME)
if _db_dir and not os.path.exists(_db_dir):
    os.makedirs(_db_dir, exist_ok=True)

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
        log(f"[db] insertion error: {e}")


# ------------------------------------------------------------------
# Blynk helpers
# ------------------------------------------------------------------
def fetch_blynk_pins(pins):
    """
    Fetch multiple virtual pins in one call.
    Blynk returns JSON like {"V0": 24.6, "V1": 67.2, ...}.
    Returns a dict {pin: float} or None on hard failure.
    """
    url = f"{BLYNK_SERVER}/get?token={BLYNK_AUTH_TOKEN}&" + "&".join(pins)
    try:
        r = _session.get(url, timeout=(3, 6))
        if r.status_code != 200:
            log(f"[blynk] /get HTTP {r.status_code}: {r.text[:150]}")
            return None
        raw = r.json()
        # Normalise non-dict responses (single pin returns a scalar)
        if not isinstance(raw, dict):
            raw = {pins[0]: raw}
        return {p: float(raw.get(p, 0) or 0) for p in pins}
    except Exception as e:
        log(f"[blynk] /get exception: {type(e).__name__}: {e}")
        return None


def fetch_device_online():
    """
    Returns:
        True  -> hardware is connected
        False -> hardware is NOT connected (normal offline state)
        None  -> cloud unreachable / unexpected error
    """
    url = f"{BLYNK_SERVER}/isHardwareConnected?token={BLYNK_AUTH_TOKEN}"
    try:
        r = _session.get(url, timeout=(3, 6))
        if r.status_code == 200:
            return r.text.strip().lower() == "true"
        # Blynk returns 400/404 when the device has never been connected.
        # That is NOT a cloud error — treat it as "device offline".
        if r.status_code in (400, 404):
            return False
        log(f"[blynk] isHardwareConnected HTTP {r.status_code}: {r.text[:150]}")
        return None
    except Exception as e:
        log(f"[blynk] isHardwareConnected exception: {type(e).__name__}: {e}")
        return None


# ------------------------------------------------------------------
# Background poller
# ------------------------------------------------------------------
def poller():
    log("[poller] thread started")
    last_log = 0.0
    cycle = 0

    while True:
        cycle += 1
        try:
            started = time.time()

            # Always try to fetch values — Blynk keeps the last known
            # values even when the hardware is offline, which is what
            # we want to display alongside the offline badge.
            values = fetch_blynk_pins(list(VIRTUAL_PINS.values()))
            device_online = fetch_device_online()

            with data_lock:
                # 1) Update numeric values from Blynk (if we got them)
                if values is not None:
                    latest_data.update({
                        "temp":     values[VIRTUAL_PINS["temp"]],
                        "humidity": values[VIRTUAL_PINS["humidity"]],
                        "moisture": int(values[VIRTUAL_PINS["moisture"]]),
                        "height":   values[VIRTUAL_PINS["height"]],
                        "pump":     int(values[VIRTUAL_PINS["pump"]]),
                        "fan":      int(values[VIRTUAL_PINS["fan"]]),
                        "light":    int(values[VIRTUAL_PINS["light"]]),
                        "updated":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "cloud_ok": True,
                    })

                # 2) Update device status flags
                if device_online is True:
                    latest_data.update({
                        "online": True,
                        "status": "online",
                        "status_message": "All systems normal",
                    })
                elif device_online is False:
                    latest_data.update({
                        "online": False,
                        "status": "offline",
                        "status_message": "Device not connected",
                    })
                else:
                    # None -> could not reach Blynk at all
                    latest_data.update({
                        "online": False,
                        "cloud_ok": False,
                        "status": "cloud_error",
                        "status_message": "Cannot reach Blynk cloud",
                    })

                snapshot = dict(latest_data)

            # 3) Persist to DB at a slower cadence
            if snapshot["cloud_ok"] and time.time() - last_log >= LOG_INTERVAL:
                log_to_db(snapshot)
                last_log = time.time()

            # 4) Periodic diagnostic line so we can see the loop is alive
            if cycle % 30 == 0:
                log(
                    f"[poller] cycle={cycle} "
                    f"online={snapshot['online']} "
                    f"cloud_ok={snapshot['cloud_ok']} "
                    f"temp={snapshot['temp']} "
                    f"hum={snapshot['humidity']} "
                    f"moist={snapshot['moisture']}"
                )

            elapsed = time.time() - started
            time.sleep(max(0.0, POLL_INTERVAL - elapsed))

        except Exception as e:
            log(f"[poller] unexpected error: {type(e).__name__}: {e}")
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
    """Diagnostic endpoint — visit this URL when things look frozen."""
    import platform
    info = {
        "python": platform.python_version(),
        "token_set": bool(BLYNK_AUTH_TOKEN),
        "token_prefix": BLYNK_AUTH_TOKEN[:6] + "..." if BLYNK_AUTH_TOKEN else None,
        "blynk_server": BLYNK_SERVER,
        "db_path": DB_NAME,
        "poll_interval": POLL_INTERVAL,
        "log_interval": LOG_INTERVAL,
    }

    # Direct, un-cached Blynk call
    url = f"{BLYNK_SERVER}/isHardwareConnected?token={BLYNK_AUTH_TOKEN}"
    try:
        r = requests.get(url, timeout=10)
        info["blynk_status_code"] = r.status_code
        info["blynk_body"] = r.text[:200]
    except Exception as e:
        info["blynk_error"] = f"{type(e).__name__}: {e}"

    # Direct pin fetch too
    try:
        pins = list(VIRTUAL_PINS.values())
        purl = f"{BLYNK_SERVER}/get?token={BLYNK_AUTH_TOKEN}&" + "&".join(pins)
        pr = requests.get(purl, timeout=10)
        info["pins_status_code"] = pr.status_code
        info["pins_body"] = pr.text[:300]
    except Exception as e:
        info["pins_error"] = f"{type(e).__name__}: {e}"

    with data_lock:
        info["latest_data"] = dict(latest_data)

    return jsonify(info)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# Startup — runs both locally AND under gunicorn
# ------------------------------------------------------------------
log("[startup] mung.py loaded, __name__ =", __name__)
init_db()
log("[startup] database ready at", DB_NAME)

# Start the poller exactly once. Run gunicorn with --workers 1
# so this guard is sufficient (Render does this by default).
if os.environ.get("START_POLLER", "1") == "1":
    threading.Thread(target=poller, daemon=True, name="blynk-poller").start()
    os.environ["START_POLLER"] = "0"
    log("[startup] poller thread launched")
else:
    log("[startup] poller already running in another worker, skipping")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)