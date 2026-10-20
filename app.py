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
    c.execute('''CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, title TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS catalog (id TEXT PRIMARY KEY, title TEXT, desc TEXT, price TEXT, img_paths TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, chat_id TEXT, text TEXT, time TEXT, is_daily INTEGER, img_paths TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, admin_chat_id TEXT, name TEXT, text TEXT, schedule_time TEXT, status TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users (chat_id TEXT PRIMARY KEY, trial_end TEXT)''')
    conn.commit()
    conn.close()

init_db()

MOSCOW_TZ = pytz.timezone('Europe/Moscow')
scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
scheduler.start()

headers = {"Authorization": TOKEN}
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ... (функции upload_file_to_max и send_max_message оставляем как были) ...
def upload_file_to_max(filepath, file_type='image'):
    try:
        upload_url_api = f"{BASE_URL}/uploads"
        res = requests.post(upload_url_api, headers=headers, params={"type": file_type}, verify=False)
        if res.status_code != 200: return None
        upload_url = res.json().get('url')
        if not upload_url: return None
        with open(filepath, 'rb') as f:
            upload_res = requests.post(upload_url, files={'file': f}, verify=False)
            if upload_res.status_code == 200:
                data = upload_res.json()
                token = data.get('photos', {}).get('original', {}).get('token') if file_type == 'image' else data.get('token')
                if not token and 'photos' in data:
                    token = next(iter(data['photos'].values())).get('token')
                if token: return {"type": file_type, "payload": {"token": token}}
    except Exception as e: print(f"Ошибка загрузки: {e}", flush=True)
    return None

def send_max_message(chat_id, text, media_paths=None):
    try:
        attachments = []
        if media_paths:
            for path in media_paths:
                if os.path.exists(path):
                    att = upload_file_to_max(path, 'image')
                    if att: attachments.append(att)
        payload = {"text": text}
        if attachments: payload["attachments"] = attachments
        res = requests.post(f"{BASE_URL}/messages", headers=headers, params={"chat_id": chat_id}, json=payload, verify=False)
        return res.status_code == 200
    except Exception: return False

# --- ЗАДАЧИ ---
def scheduled_job(chat_id, text, time_str, is_daily, img_paths, job_id):
    # Добавляем задержку, чтобы не было конфликта при одновременном запуске
    time.sleep(2) 
    print(f"⏰ Запуск рассылки {job_id}", flush=True)
    media = [p.strip() for p in img_paths.split(',')] if img_paths else []
    send_max_message(chat_id, text, media)
    if is_daily == 0:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
        conn.close()

def send_client_reminder(admin_chat_id, client_name, text, client_id):
    time.sleep(4) # Задержка, чтобы не пересекаться с рассылкой
    print(f"⏰ Отправка напоминания менеджеру о клиенте {client_name}", flush=True)
    reminder_text = f"🔔 Напоминание по клиенту:\n\n👤 {client_name}\n\n📝 {text}\n\nПора написать!"
    success = send_max_message(admin_chat_id, reminder_text)
    conn = sqlite3.connect(DB_PATH)
    status = 'Отправлено' if success else 'Ошибка'
    conn.execute("UPDATE clients SET status=? WHERE id=?", (status, client_id))
    conn.commit()
    conn.close()

# ... (маршруты check_trial, get_chats, get_jobs, delete_job, get_catalog, add_catalog, edit_catalog, submit_ad оставляем как были) ...
# ВСТАВЬ ИХ СЮДА ИЗ ПРЕДЫДУЩЕГО КОДА

# ПЛАНЕР (Исправленный)
@app.route('/add_client', methods=['POST', 'OPTIONS'])
def add_client():
    if request.method == 'OPTIONS': return '', 204
    try:
        name, text = request.form.get('name'), request.form.get('text')
        time_str, date_str = request.form.get('time'), request.form.get('date')
        admin_chat_id = request.form.get('admin_chat_id')
        client_id = str(int(time.time()))
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO clients (id, admin_chat_id, name, text, schedule_time, status) VALUES (?, ?, ?, ?, ?, ?)", (client_id, admin_chat_id, name, text, f"{date_str} {time_str}", "Запланировано"))
        conn.commit()
        conn.close()

        # Явно преобразуем строку в datetime объект
        year, month, day = map(int, date_str.split('-'))
        hour, minute = map(int, time_str.split(':'))
        run_date = datetime(year, month, day, hour, minute, tzinfo=MOSCOW_TZ)
        
        scheduler.add_job(send_client_reminder, 'date', run_date=run_date, args=[admin_chat_id, name, text, client_id], id=f"client_{client_id}")
        return jsonify({"status": "ok"}), 200
    except Exception as e: 
        print(f"Ошибка клиента: {e}", flush=True)
        return jsonify({"status": "error"}), 500

# ... (run_bot оставляем как был) ...
def run_bot():
    # ... код бота ...
    pass

if __name__ == '__main__':
    t = Thread(target=run_bot)
    t.daemon = True
    t.start()
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
