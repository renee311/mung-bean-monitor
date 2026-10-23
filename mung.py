import os
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# Database configuration
DATABASE_URL = os.environ.get("DATABASE_URL")

# Vercel/Heroku gives postgres:// but psycopg2 requires postgresql://
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

def get_conn():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing!")
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if not DATABASE_URL:
        print("DATABASE_URL is missing!")
        return
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sensor_logs (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                temperature REAL,
                humidity REAL,
                soil_moisture INTEGER,
                pump_state INTEGER,
                fan_state INTEGER,
                light_state INTEGER
            )
        ''')
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"DB Init Error: {e}")
    finally:
        if conn:
            conn.close()

# Initialize DB structure on startup
init_db()

# Receive Telemetry from ESP32
@app.route('/api/data', methods=['POST'])
def receive_data():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "Missing JSON payload"}), 400

        temp = data.get('temperature')
        hum = data.get('humidity')
        moisture = data.get('soil_moisture')
        pump = data.get('pump_state', 0)
        fan = data.get('fan_state', 0)
        light = data.get('light_state', 0)

        conn = get_conn()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO sensor_logs 
            (temperature, humidity, soil_moisture, pump_state, fan_state, light_state) 
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (temp, hum, moisture, pump, fan, light))
        
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"POST Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Get Latest Reading for Dashboard
@app.route('/api/data', methods=['GET'])
def get_latest_data():
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('''
            SELECT timestamp, temperature, humidity, soil_moisture,
                   pump_state, fan_state, light_state 
            FROM sensor_logs ORDER BY id DESC LIMIT 1
        ''')
        latest = cur.fetchone()
        cur.close()

        if latest:
            ts = latest['timestamp']
            if isinstance(ts, datetime):
                last_dt = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
            else:
                last_dt = datetime.strptime(str(ts), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

            # Mark online if telemetry was received within the last 30 seconds
            now_utc = datetime.now(timezone.utc)
            is_online = (now_utc - last_dt) < timedelta(seconds=30)

            return jsonify({
                "online": is_online,
                "status": "online" if is_online else "offline",
                "status_message": "IoT Device Online" if is_online else "IoT Device Offline",
                "updated": last_dt.strftime('%Y-%m-%d %H:%M:%S UTC'),
                "temp": latest['temperature'],
                "humidity": latest['humidity'],
                "moisture": latest['soil_moisture'],
                "pump": latest['pump_state'],
                "fan": latest['fan_state'],
                "light": latest['light_state']
            })
    except Exception as e:
        print(f"GET Error: {e}")
    finally:
        if conn:
            conn.close()

    # Fallback response when database is empty
    return jsonify({
        "online": False, "status": "no_data", "status_message": "Awaiting Telemetry",
        "updated": "--", "temp": 0.0, "humidity": 0.0, "moisture": 0,
        "pump": 0, "fan": 0, "light": 0
    })

# Fetch History for Dashboard Charts
@app.route('/api/history', methods=['GET'])
def get_history():
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('''
            SELECT timestamp, temperature, humidity, soil_moisture 
            FROM sensor_logs ORDER BY id DESC LIMIT 50
        ''')
        rows = cur.fetchall()
        cur.close()

        history = []
        for r in reversed(rows):
            ts = r['timestamp']
            ts_str = ts.strftime('%Y-%m-%d %H:%M:%S') if isinstance(ts, datetime) else str(ts)
            history.append({
                "timestamp": ts_str, 
                "temp": r['temperature'], 
                "humidity": r['humidity'], 
                "moisture": r['soil_moisture']
            })
        return jsonify(history)
    except Exception as e:
        print(f"History Error: {e}")
        return jsonify([])
    finally:
        if conn:
            conn.close()

# Dashboard Page Route
@app.route('/')
def dashboard():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(base_dir, 'index.html'), 'r', encoding='utf-8') as f:
            return render_template_string(f.read())
    except FileNotFoundError:
        return "<h2>index.html missing! Please ensure it is located in the root folder.</h2>", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
