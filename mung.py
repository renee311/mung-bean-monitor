import os
import sqlite3
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

@app.route('/')
def dashboard():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT timestamp, temperature, humidity, soil_moisture, plant_height, pump_state, fan_state, light_state 
        FROM sensor_logs ORDER BY id DESC LIMIT 1
    ''')
    latest = cursor.fetchone()
    conn.close()

    if latest:
        ts, temp, hum, soil, height, pump, fan, light = latest
        pump_str = "ON" if pump == 1 else "OFF"
        fan_str = "ON" if fan == 1 else "OFF"
        light_str = "ON" if light == 1 else "OFF"
    else:
        ts, temp, hum, soil, height = "Waiting for ESP32...", "--", "--", "--", "--"
        pump_str, fan_str, light_str = "OFF", "OFF", "OFF"

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>🌱 Mung Bean Enclosure Monitor</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="5">
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; text-align: center; background-color: #121212; color: #ffffff; padding: 20px; margin: 0; }}
            h1 {{ color: #4CAF50; margin-top: 20px; }}
            .card {{ background: #1e1e1e; border-radius: 12px; padding: 18px; margin: 12px auto; max-width: 420px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); }}
            .value {{ font-size: 2.2em; font-weight: bold; color: #00E676; margin: 5px 0; }}
            .status-box {{ display: flex; justify-content: space-around; font-weight: bold; font-size: 1.1em; }}
            .on {{ color: #00E676; }}
            .off {{ color: #FF5252; }}
            .label {{ font-size: 0.85em; color: #aaa; letter-spacing: 1px; }}
        </style>
    </head>
    <body>
        <h1>🌱 Mung Bean Enclosure Live Server</h1>
        <p class="label">LAST UPDATED: {ts}</p>
        
        <div class="card">
            <div class="label">TEMPERATURE</div>
            <div class="value">{temp} °C</div>
        </div>
        
        <div class="card">
            <div class="label">HUMIDITY</div>
            <div class="value">{hum} %</div>
        </div>

        <div class="card">
            <div class="label">SOIL MOISTURE (RAW ADC)</div>
            <div class="value">{soil}</div>
        </div>

        <div class="card">
            <div class="label">PLANT HEIGHT</div>
            <div class="value">{height} cm</div>
        </div>

        <div class="card">
            <div class="label" style="margin-bottom: 10px;">RELAY ACTUATORS</div>
            <div class="status-box">
                <div>Pump: <span class="{'on' if pump == 1 else 'off'}">{pump_str}</span></div>
                <div>Fan: <span class="{'on' if fan == 1 else 'off'}">{fan_str}</span></div>
                <div>Light: <span class="{'on' if light == 1 else 'off'}">{light_str}</span></div>
            </div>
        </div>
    </body>
    </html>
    '''
    return render_template_string(html)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)