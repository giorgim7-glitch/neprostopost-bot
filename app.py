import os
import requests
import time
import urllib3
from flask import Flask, request, jsonify
from flask_cors import CORS
from threading import Thread
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename
import pytz
import sqlite3
from datetime import datetime, timedelta

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOKEN = "f9LHodD0cOLMdTsqq6Aayvaoutp2PBcYlCKXPS7dEg83rYK5B88u3b-KlPiCuCsRPUmJ3Vl6ruCUgOSCqFig"
BASE_URL = "https://platform-api2.max.ru"
MY_CHAT_ID = 505757849 

DB_PATH = '/app/data.db'
UPLOAD_FOLDER = '/app/uploads'
if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, title TEXT, members_count INTEGER DEFAULT 0, status TEXT DEFAULT 'Активна')''')
    c.execute('''CREATE TABLE IF NOT EXISTS catalog (id TEXT PRIMARY KEY, title TEXT, desc TEXT, price TEXT, img_paths TEXT, tag TEXT, address TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, chat_id TEXT, text TEXT, time TEXT, is_daily INTEGER, img_paths TEXT)''')
    # ДОБАВИЛИ phone
    c.execute('''CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, admin_chat_id TEXT, name TEXT, phone TEXT, text TEXT, schedule_time TEXT, status TEXT, type TEXT DEFAULT 'Звонок')''')
    c.execute('''CREATE TABLE IF NOT EXISTS users (chat_id TEXT PRIMARY KEY, trial_end TEXT, is_paid INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

init_db()

MOSCOW_TZ = pytz.timezone('Europe/Moscow')
scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
scheduler.start()

headers = {"Authorization": TOKEN}
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ... (функции upload_file_to_max, send_max_message, scheduled_job, send_client_reminder оставляем без изменений) ...
def upload_file_to_max(filepath, file_type='image'):
    # ... код ...
    pass

def send_max_message(chat_id, text, media_paths=None):
    # ... код ...
    pass

def scheduled_job(chat_id, text, time_str, is_daily, img_paths, job_id):
    # ... код ...
    pass

def send_client_reminder(admin_chat_id, client_name, text, client_id):
    # ... код ...
    pass

# --- НОВЫЙ МАРШРУТ ДЛЯ ДОБАВЛЕНИЯ КЛИЕНТА (С ТЕЛЕФОНОМ) ---
@app.route('/add_client', methods=['POST', 'OPTIONS'])
def add_client():
    if request.method == 'OPTIONS': return '', 204
    try:
        name = request.form.get('name')
        phone = request.form.get('phone', '') # Получаем телефон
        text = request.form.get('text')
        time_str = request.form.get('time')
        date_str = request.form.get('date')
        admin_chat_id = request.form.get('admin_chat_id')
        client_id = str(int(time.time()))
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO clients (id, admin_chat_id, name, phone, text, schedule_time, status) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                     (client_id, admin_chat_id, name, phone, text, f"{date_str} {time_str}", "Запланировано"))
        conn.commit()
        conn.close()

        year, month, day = map(int, date_str.split('-'))
        hour, minute = map(int, time_str.split(':'))
        run_date = datetime(year, month, day, hour, minute, tzinfo=MOSCOW_TZ)
        
        scheduler.add_job(send_client_reminder, 'date', run_date=run_date, args=[admin_chat_id, name, text, client_id], id=f"client_{client_id}")
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"status": "error"}), 500

# --- МАРШРУТ ПОЛУЧЕНИЯ КЛИЕНТОВ (С ТЕЛЕФОНОМ) ---
@app.route('/get_clients', methods=['GET'])
def get_clients():
    conn = sqlite3.connect(DB_PATH)
    # Добавили phone в выборку
    clients = [{"id": r[0], "name": r[1], "phone": r[2], "text": r[3], "schedule_time": r[4], "status": r[5]} for r in conn.execute("SELECT id, name, phone, text, schedule_time, status FROM clients").fetchall()]
    conn.close()
    return jsonify({"clients": clients})

# ... (остальные маршруты get_chats, get_jobs, get_stats, delete_job, get_catalog, add_catalog, edit_catalog, submit_ad, check_trial оставляем как были) ...

def run_bot():
    # ... код бота ...
    pass

if __name__ == '__main__':
    t = Thread(target=run_bot)
    t.daemon = True
    t.start()
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
