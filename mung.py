import os
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# ==========================================
# DATABASE CONFIGURATION
# ==========================================
DATABASE_URL = os.environ.get("DATABASE_URL")

# Handle Vercel/Heroku postgres:// prefix quirk
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if not DATABASE_URL:
        print("DATABASE_URL is missing!")
        return
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sensor_logs (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                temperature REAL,
                humidity REAL,
                soil_moisture INTEGER,
                plant_height REAL,
                pump_state INTEGER,
                fan_state INTEGER,
                light_state INTEGER
            )
        ''')
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB Init Error: {e}")

# Run on cold start
init_db()

# ==========================================
# ESP32 TELEMETRY RECEIVER (POST)
# ==========================================
@app.route('/api/data', methods=['POST'])
def receive_data():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "Missing JSON"}), 400

        conn = get_conn()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO sensor_logs 
            (temperature, humidity, soil_moisture, plant_height, pump_state, fan_state, light_state) 
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (
            data.get('temperature', 0.0), data.get('humidity', 0.0), 
            data.get('soil_moisture', 0), data.get('plant_height', 0.0), 
            data.get('pump_state', 0), data.get('fan_state', 0), data.get('light_state', 0)
        ))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# DASHBOARD REAL-TIME DATA (GET)
# ==========================================
@app.route('/api/data', methods=['GET'])
def get_latest_data():
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('''
            SELECT timestamp, temperature, humidity, soil_moisture, 
                   plant_height, pump_state, fan_state, light_state 
            FROM sensor_logs ORDER BY id DESC LIMIT 1
        ''')
        latest = cur.fetchone()
        cur.close()
        conn.close()

        if latest:
            ts = latest['timestamp']
            if isinstance(ts, datetime):
                last_dt = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
            else:
                last_dt = datetime.strptime(str(ts), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

            is_online = (datetime.now(timezone.utc) - last_dt) < timedelta(seconds=30)
            
            return jsonify({
                "online": is_online,
                "status": "online" if is_online else "offline",
                "status_message": "IoT Device Online" if is_online else "IoT Device Offline",
                "updated": last_dt.strftime('%Y-%m-%d %H:%M:%S'),
                "temp": latest['temperature'],
                "humidity": latest['humidity'],
                "moisture": latest['soil_moisture'],
                "height": latest['plant_height'],
                "pump": latest['pump_state'],
                "fan": latest['fan_state'],
                "light": latest['light_state']
            })
    except Exception as e:
        print(f"Fetch Error: {e}")

    return jsonify({
        "online": False, "status": "no_data", "status_message": "Awaiting Telemetry",
        "updated": "--", "temp": 0.0, "humidity": 0.0, "moisture": 0, 
        "height": 0.0, "pump": 0, "fan": 0, "light": 0
    })

# ==========================================
# CHART HISTORICAL DATA (GET)
# ==========================================
@app.route('/api/history', methods=['GET'])
def get_history():
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('''
            SELECT timestamp, temperature, humidity, soil_moisture, plant_height 
            FROM sensor_logs ORDER BY id DESC LIMIT 50
        ''')
        rows = cur.fetchall()
        cur.close()
        conn.close()

        history = []
        for r in reversed(rows):
            ts = r['timestamp']
            ts_str = ts.strftime('%Y-%m-%d %H:%M:%S') if isinstance(ts, datetime) else str(ts)
            history.append({
                "timestamp": ts_str, "temp": r['temperature'], 
                "humidity": r['humidity'], "moisture": r['soil_moisture'], "height": r['plant_height']
            })
        return jsonify(history)
    except Exception:
        return jsonify([])

# ==========================================
# HTML DASHBOARD ROUTE
# ==========================================
@app.route('/')
def dashboard():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(base_dir, 'index.html'), 'r', encoding='utf-8') as f:
            return render_template_string(f.read())
    except FileNotFoundError:
        return "<h2>index.html missing! Please ensure it is in the root folder.</h2>", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
