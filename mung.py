import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)
DB_FILE = 'mungbean.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sensor_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
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
    conn.close()

init_db()

# ==========================================
# ESP32 TELEMETRY RECEIVER ENDPOINT
# ==========================================
@app.route('/api/data', methods=['POST'])
def receive_data():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "Missing JSON payload"}), 400

        temp = data.get('temperature', 0.0)
        hum = data.get('humidity', 0.0)
        soil = data.get('soil_moisture', 0)
        height = data.get('plant_height', 0.0)
        pump = data.get('pump_state', 0)
        fan = data.get('fan_state', 0)
        light = data.get('light_state', 0)

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO sensor_logs 
            (temperature, humidity, soil_moisture, plant_height, pump_state, fan_state, light_state) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (temp, hum, soil, height, pump, fan, light))
        conn.commit()
        conn.close()

        return jsonify({"status": "success", "message": "Telemetry logged"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# DASHBOARD REAL-TIME DATA API (GET)
# ==========================================
@app.route('/api/data', methods=['GET'])
def get_latest_data():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT timestamp, temperature, humidity, soil_moisture, plant_height, pump_state, fan_state, light_state 
        FROM sensor_logs ORDER BY id DESC LIMIT 1
    ''')
    latest = cursor.fetchone()
    conn.close()

    if latest:
        ts_str, temp, hum, soil, height, pump, fan, light = latest
        
        # Determine online status if telemetry arrived within 30 seconds
        try:
            last_dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
            is_online = (datetime.utcnow() - last_dt) < timedelta(seconds=30)
        except Exception:
            is_online = True

        return jsonify({
            "online": is_online,
            "status": "online" if is_online else "offline",
            "status_message": "IoT Device Online" if is_online else "IoT Device Offline",
            "updated": ts_str,
            "temp": temp,
            "humidity": hum,
            "moisture": soil,
            "height": height,
            "pump": pump,
            "fan": fan,
            "light": light
        })
    else:
        return jsonify({
            "online": False,
            "status": "no_data",
            "status_message": "Awaiting ESP32 Telemetry",
            "updated": "--",
            "temp": 0.0,
            "humidity": 0.0,
            "moisture": 0,
            "height": 0.0,
            "pump": 0,
            "fan": 0,
            "light": 0
        })

# ==========================================
# CHART HISTORICAL TELEMETRY API
# ==========================================
@app.route('/api/history', methods=['GET'])
def get_history():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT timestamp, temperature, humidity, soil_moisture, plant_height 
        FROM sensor_logs ORDER BY id DESC LIMIT 50
    ''')
    rows = cursor.fetchall()
    conn.close()

    history = []
    for r in reversed(rows):
        history.append({
            "timestamp": r[0],
            "temp": r[1],
            "humidity": r[2],
            "moisture": r[3],
            "height": r[4]
        })
    return jsonify(history)

# ==========================================
# HTML DASHBOARD ROUTE (WITH ABSOLUTE PATH)
# ==========================================
@app.route('/')
def dashboard():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(base_dir, 'index.html')
    
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        return render_template_string(html_content)
    except FileNotFoundError:
        return "<h2>index.html missing! Please ensure index.html is committed to your GitHub repository.</h2>", 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)