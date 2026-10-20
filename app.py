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
    
    # НОВАЯ ТАБЛИЦА ДЛЯ ПЛАНЕРА КАСАНИЙ
    c.execute('''CREATE TABLE IF NOT EXISTS clients (
        id TEXT PRIMARY KEY, 
        name TEXT, 
        client_chat_id TEXT, 
        text TEXT, 
        schedule_time TEXT, 
        status TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

MOSCOW_TZ = pytz.timezone('Europe/Moscow')
scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
scheduler.start()

headers = {"Authorization": TOKEN}
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ... (функции upload_file_to_max и send_max_message оставляем без изменений) ...
def upload_file_to_max(filepath, file_type='image'):
    # ... код ...
    pass # (вставь код из предыдущего сообщения)

def send_max_message(chat_id, text, media_paths=None):
    # ... код ...
    pass # (вставь код из предыдущего сообщения)

# ... (scheduled_job оставляем без изменений) ...
def scheduled_job(chat_id, text, time_str, is_daily, img_paths, job_id):
    # ... код ...
    pass

# --- ПЛАНИРОВЩИК ДЛЯ КЛИЕНТОВ (ПЛАНЕР) ---
def send_client_message(client_chat_id, text, client_id):
    print(f"⏰ Отправка напоминания клиенту {client_id}", flush=True)
    success = send_max_message(client_chat_id, text)
    
    # После отправки обновляем статус в базе
    conn = sqlite3.connect(DB_PATH)
    if success:
        conn.execute("UPDATE clients SET status='Отправлено' WHERE id=?", (client_id,))
    else:
        conn.execute("UPDATE clients SET status='Ошибка' WHERE id=?", (client_id,))
    conn.commit()
    conn.close()

# --- МАРШРУТЫ ---
@app.route('/')
def home(): return "Сервер работает!"

@app.route('/get_chats', methods=['GET'])
def get_chats():
    conn = sqlite3.connect(DB_PATH)
    chats = [{"id": r[0], "title": r[1]} for r in conn.execute("SELECT * FROM chats").fetchall()]
    conn.close()
    return jsonify({"chats": chats})

@app.route('/get_jobs', methods=['GET'])
def get_jobs():
    conn = sqlite3.connect(DB_PATH)
    jobs = []
    for r in conn.execute("SELECT id, time, text, is_daily FROM jobs").fetchall():
        jobs.append({"id": r[0], "time": r[1], "text": r[2][:20] + "...", "schedule": f"Каждый день в {r[1]}" if r[3] else f"Разово в {r[1]}"})
    conn.close()
    return jsonify({"jobs": jobs})

@app.route('/delete_job/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    try: scheduler.remove_job(job_id)
    except: pass
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

# ... (маршруты get_catalog, add_catalog, submit_ad оставляем без изменений) ...
@app.route('/get_catalog', methods=['GET'])
def get_catalog():
    # ... код ...
    pass

@app.route('/add_catalog', methods=['POST', 'OPTIONS'])
def add_catalog():
    # ... код ...
    pass

@app.route('/submit_ad', methods=['POST', 'OPTIONS'])
def submit_ad():
    # ... код ...
    pass

# --- НОВЫЕ МАРШРУТЫ ДЛЯ ПЛАНЕРА КАСАНИЙ ---
@app.route('/get_clients', methods=['GET'])
def get_clients():
    conn = sqlite3.connect(DB_PATH)
    clients = []
    for r in conn.execute("SELECT id, name, client_chat_id, text, schedule_time, status FROM clients").fetchall():
        clients.append({"id": r[0], "name": r[1], "client_chat_id": r[2], "text": r[3], "schedule_time": r[4], "status": r[5]})
    conn.close()
    return jsonify({"clients": clients})

@app.route('/add_client', methods=['POST', 'OPTIONS'])
def add_client():
    if request.method == 'OPTIONS': return '', 204
    try:
        name = request.form.get('name')
        text = request.form.get('text')
        time_str = request.form.get('time') # Формат HH:MM
        date_str = request.form.get('date') # Формат YYYY-MM-DD

        client_id = str(int(time.time()))
        
        # Сохраняем в базу (пока без chat_id клиента, его мы получим, когда он нажмет Start)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO clients (id, name, client_chat_id, text, schedule_time, status) VALUES (?, ?, ?, ?, ?, ?)", 
                     (client_id, name, '', text, f"{date_str} {time_str}", "Ожидает привязки"))
        conn.commit()
        conn.close()

        return jsonify({"status": "ok", "client_id": client_id}), 200
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

# --- ЦИКЛ БОТА ---
def run_bot():
    print("🚀 Бот запущен...", flush=True)
    last_marker = None
    while True:
        try:
            res = requests.get(f"{BASE_URL}/updates", headers=headers, params={"timeout": 30, "types": "message_created,bot_started"}, verify=False)
            if res.status_code == 200:
                data = res.json()
                if "marker" in data: last_marker = data["marker"]
                
                for u in data.get("updates", []):
                    update_type = u.get("update_type")
                    chat_id = u.get("chat_id") or u.get("message", {}).get("recipient", {}).get("chat_id")
                    
                    # 1. Ловим группы
                    if chat_id:
                        is_dialog = u.get("message", {}).get("recipient", {}).get("chat", {}).get("type") == "dialog"
                        if not is_dialog:
                            conn = sqlite3.connect(DB_PATH)
                            exists = conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone()
                            if not exists:
                                title = u.get("chat", {}).get("title") or f"Группа {chat_id}"
                                conn.execute("INSERT INTO chats (id, title) VALUES (?, ?)", (chat_id, title))
                                conn.commit()
                                send_max_message(chat_id, "Привет! Я бот НЕПРОСТОПОСТ.")
                            conn.close()

                    # 2. Ловим старт в личке (для планера)
                    if update_type == "bot_started":
                        # Получаем payload (параметр из ссылки)
                        payload = u.get("payload") 
                        if payload:
                            # Пользователь перешел по ссылке с ID клиента
                            client_id = payload
                            conn = sqlite3.connect(DB_PATH)
                            client = conn.execute("SELECT text, schedule_time FROM clients WHERE id=?", (client_id,)).fetchone()
                            if client:
                                text, schedule_time = client
                                # Привязываем chat_id клиента
                                conn.execute("UPDATE clients SET client_chat_id=?, status='Запланировано' WHERE id=?", (chat_id, client_id))
                                conn.commit()
                                
                                # Ставим задачу в планировщик
                                date_part, time_part = schedule_time.split(' ')
                                hour, minute = map(int, time_part.split(':'))
                                year, month, day = map(int, date_part.split('-'))
                                
                                scheduler.add_job(
                                    send_client_message, 
                                    'date', 
                                    run_date=f"{year}-{month}-{day} {hour}:{minute}:00", 
                                    args=[chat_id, text, client_id], 
                                    id=f"client_{client_id}"
                                )
                                send_max_message(chat_id, f"Отлично! Я напомню вам о встрече {schedule_time}.")
                            conn.close()
                        else:
                            send_max_message(chat_id, "Привет! Я бот-помощник НЕПРОСТОПОСТ.")
        except Exception as e: 
            print(f"Ошибка цикла: {e}", flush=True)
            time.sleep(5)
        time.sleep(1)

if __name__ == '__main__':
    t = Thread(target=run_bot)
    t.daemon = True
    t.start()
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
