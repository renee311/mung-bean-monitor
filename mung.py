from flask import Flask, render_template, jsonify
import requests
import sqlite3
import threading
import time
import os
import sys
import traceback
import platform
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)


# ------------------------------------------------------------------
# Logging helper — flushes immediately so Render's log viewer shows
# output from daemon threads (plain print() often buffers and vanishes).
# ------------------------------------------------------------------
def log(*args):
    try:
        print(*args, flush=True)
        sys.stdout.flush()
    except Exception:
        pass


# ------------------------------------------------------------------
# Blynk configuration — read token from environment
# ------------------------------------------------------------------
BLYNK_AUTH_TOKEN = os.environ.get("BLYNK_AUTH_TOKEN")
if not BLYNK_AUTH_TOKEN:
    raise ValueError(
        "BLYNK_AUTH_TOKEN environment variable is required. "
        "Set it in your Render dashboard before starting the app."
    )

BLYNK_SERVER = "https://blynk.cloud/external/api"


# ------------------------------------------------------------------
# Database configuration
# ------------------------------------------------------------------
DB_NAME = os.environ.get("DB_PATH", "mung_bean_history.db")
_db_dir = os.path.dirname(DB_NAME)
if _db_dir and not os.path.exists(_db_dir):
    os.makedirs(_db_dir, exist_ok=True)

POLL_INTERVAL = 1.0     # poller reads Blynk every 1 s
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
    "temp": 0.0,
    "humidity": 0.0,
    "moisture": 0,
    "height": 0.0,
    "pump": 0,
    "fan": 0,
    "light": 0,
    "online": False,
    "cloud_ok": False,
    "updated": None,
    "status": "checking",
    "status_message": "Initializing...",
}
data_lock = threading.Lock()


# ------------------------------------------------------------------
# HTTP sessions — one per thread role so they never collide
# ------------------------------------------------------------------
def make_session():
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


_session        = make_session()   # used by Flask route handlers
_poller_session = make_session()   # used exclusively by the poller thread


# ------------------------------------------------------------------
# Database helpers
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
        log(f"[db] insertion error: {type(e).__name__}: {e}")


# ------------------------------------------------------------------
# Blynk helpers
# ------------------------------------------------------------------
def fetch_blynk_pins(pins, session=None):
    """Fetch all virtual pins in one Blynk call. Returns dict or None."""
    s = session or _session
    url = f"{BLYNK_SERVER}/get?token={BLYNK_AUTH_TOKEN}&" + "&".join(pins)
    try:
        r = s.get(url, timeout=(3, 6))
        if r.status_code != 200:
            log(f"[blynk] /get HTTP {r.status_code}: {r.text[:150]}")
            return None
        raw = r.json()
        if not isinstance(raw, dict):
            raw = {pins[0]: raw}
        return {p: float(raw.get(p, 0) or 0) for p in pins}
    except Exception as e:
        log(f"[blynk] /get exception: {type(e).__name__}: {e}")
        return None


def fetch_device_online(session=None):
    """
    Returns:
        True  -> hardware connected
        False -> hardware not connected (normal offline)
        None  -> cloud unreachable or unexpected error
    """
    s = session or _session
    url = f"{BLYNK_SERVER}/isHardwareConnected?token={BLYNK_AUTH_TOKEN}"
    try:
        r = s.get(url, timeout=(3, 6))
        if r.status_code == 200:
            return r.text.strip().lower() == "true"
        if r.status_code in (400, 404):
            return False
        log(f"[blynk] isHardwareConnected HTTP {r.status_code}: {r.text[:150]}")
        return None
    except Exception as e:
        log(f"[blynk] isHardwareConnected exception: {type(e).__name__}: {e}")
        return None


# ------------------------------------------------------------------
# Background poller — bulletproof
# ------------------------------------------------------------------
_poller_thread       = None
_poller_started_at   = None
_poller_cycles       = 0
_poller_last_error   = None
_poller_last_cycle_at = None


def poller_loop():
    """The actual polling loop. Cannot silently die."""
    global _poller_cycles, _poller_last_error, _poller_last_cycle_at

    log(f"[poller] thread started, pid={os.getpid()}")
    last_log = 0.0

    while True:
        _poller_cycles += 1
        try:
            started = time.time()

            # Fetch using the poller's OWN session.
            values = fetch_blynk_pins(
                list(VIRTUAL_PINS.values()), session=_poller_session
            )
            device_online = fetch_device_online(session=_poller_session)

            with data_lock:
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
                    latest_data.update({
                        "online": False,
                        "cloud_ok": False,
                        "status": "cloud_error",
                        "status_message": "Cannot reach Blynk cloud",
                    })

                snapshot = dict(latest_data)

            _poller_last_cycle_at = snapshot["updated"]

            if snapshot["cloud_ok"] and time.time() - last_log >= LOG_INTERVAL:
                log_to_db(snapshot)
                last_log = time.time()

            # Heartbeat every 30 cycles
            if _poller_cycles % 30 == 0:
                log(
                    f"[poller] cycles={_poller_cycles} "
                    f"online={snapshot['online']} "
                    f"temp={snapshot['temp']:.2f} "
                    f"hum={snapshot['humidity']:.1f} "
                    f"moist={snapshot['moisture']} "
                    f"updated={snapshot['updated']}"
                )

            elapsed = time.time() - started
            time.sleep(max(0.0, POLL_INTERVAL - elapsed))

        except BaseException as e:
            # Catch EVERYTHING so the thread physically cannot terminate.
            _poller_last_error = f"{type(e).__name__}: {e}"
            log(f"[poller] EXCEPTION: {_poller_last_error}")
            log(traceback.format_exc())
            time.sleep(2.0)


def start_poller():
    """Start the poller if it isn't already alive."""
    global _poller_thread, _poller_started_at
    if _poller_thread is not None and _poller_thread.is_alive():
        return
    _poller_thread = threading.Thread(
        target=poller_loop, daemon=True, name="blynk-poller"
    )
    _poller_thread.start()
    _poller_started_at = datetime.now().isoformat()
    log(f"[poller] launched at {_poller_started_at}")


def ensure_poller_alive():
    """Watchdog — called from every route so the poller can never stay dead."""
    if _poller_thread is None or not _poller_thread.is_alive():
        log("[watchdog] poller not alive — restarting")
        start_poller()


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def get_data():
    ensure_poller_alive()
    with data_lock:
        return jsonify(dict(latest_data))


@app.route("/api/history")
def get_history():
    ensure_poller_alive()
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
    ensure_poller_alive()
    info = {
        "python":             platform.python_version(),
        "token_set":          bool(BLYNK_AUTH_TOKEN),
        "token_prefix":       BLYNK_AUTH_TOKEN[:6] + "..." if BLYNK_AUTH_TOKEN else None,
        "blynk_server":       BLYNK_SERVER,
        "db_path":            DB_NAME,
        "poll_interval":      POLL_INTERVAL,
        "log_interval":       LOG_INTERVAL,
        # --- Poller health ---
        "poller_alive":       _poller_thread.is_alive() if _poller_thread else False,
        "poller_cycles":      _poller_cycles,
        "poller_started_at":  _poller_started_at,
        "poller_last_cycle":  _poller_last_cycle_at,
        "poller_last_error":  _poller_last_error,
        "poller_pid":         os.getpid(),
    }

    # Direct, non-poller Blynk sanity check (runs in the Flask request thread)
    url = f"{BLYNK_SERVER}/isHardwareConnected?token={BLYNK_AUTH_TOKEN}"
    try:
        r = requests.get(url, timeout=10)
        info["blynk_status_code"] = r.status_code
        info["blynk_body"] = r.text[:200]
    except Exception as e:
        info["blynk_error"] = f"{type(e).__name__}: {e}"

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
    ensure_poller_alive()
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# No-cache headers — prevents the browser from serving stale JSON/HTML
# ------------------------------------------------------------------
@app.after_request
def add_no_cache_headers(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ------------------------------------------------------------------
# Startup
# ------------------------------------------------------------------
log("[startup] mung.py loaded, __name__ =", __name__)
init_db()
log("[startup] database ready at", DB_NAME)
start_poller()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)