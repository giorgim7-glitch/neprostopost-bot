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
from datetime import datetime

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
    c.execute('''CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, name TEXT, client_chat_id TEXT, text TEXT, schedule_time TEXT, status TEXT)''')
    conn.commit()
    conn.close()

init_db()

MOSCOW_TZ = pytz.timezone('Europe/Moscow')
scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
scheduler.start()

headers = {"Authorization": TOKEN}
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

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
    except Exception as e: print(f"Ошибка: {e}", flush=True)
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

def scheduled_job(chat_id, text, time_str, is_daily, img_paths, job_id):
    print(f"⏰ Запуск рассылки {job_id}", flush=True)
    media = [p.strip() for p in img_paths.split(',')] if img_paths else []
    send_max_message(chat_id, text, media)
    if is_daily == 0:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
        conn.close()

# --- ПЛАНИРОВЩИК ДЛЯ КЛИЕНТОВ ---
def send_client_message(client_chat_id, text, client_id):
    print(f"⏰ Отправка напоминания клиенту {client_id}", flush=True)
    success = send_max_message(client_chat_id, text)
    conn = sqlite3.connect(DB_PATH)
    status = 'Отправлено' if success else 'Ошибка'
    conn.execute("UPDATE clients SET status=? WHERE id=?", (status, client_id))
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

@app.route('/get_catalog', methods=['GET'])
def get_catalog():
    conn = sqlite3.connect(DB_PATH)
    cat = [{"id": r[0], "title": r[1], "description": r[2], "price": r[3]} for r in conn.execute("SELECT id, title, desc, price FROM catalog").fetchall()]
    conn.close()
    return jsonify({"catalog": cat})

# СОХРАНЕНИЕ В КАТАЛОГ
@app.route('/add_catalog', methods=['POST', 'OPTIONS'])
def add_catalog():
    if request.method == 'OPTIONS': return '', 204
    try:
        title = request.form.get('title')
        desc = request.form.get('description')
        price = request.form.get('price')
        item_id = str(int(time.time()))
        
        saved_paths = []
        if 'images' in request.files:
            for file in request.files.getlist('images'):
                if file.filename:
                    filename = secure_filename(file.filename)
                    path = os.path.join(UPLOAD_FOLDER, filename)
                    file.save(path)
                    saved_paths.append(path)

        img_paths_str = ",".join(saved_paths)

        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO catalog (id, title, desc, price, img_paths) VALUES (?, ?, ?, ?, ?)", (item_id, title, desc, price, img_paths_str))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"}), 200
    except Exception as e: 
        print(f"Ошибка каталога: {e}", flush=True)
        return jsonify({"status": "error", "msg": str(e)}), 500

# РЕДАКТИРОВАНИЕ КАТАЛОГА
@app.route('/edit_catalog/<item_id>', methods=['POST', 'OPTIONS'])
def edit_catalog(item_id):
    if request.method == 'OPTIONS': return '', 204
    try:
        title = request.form.get('title')
        desc = request.form.get('description')
        price = request.form.get('price')
        
        conn = sqlite3.connect(DB_PATH)
        # Если загружены новые фото, обновляем и их
        if 'images' in request.files and request.files['images'].filename:
            saved_paths = []
            for file in request.files.getlist('images'):
                if file.filename:
                    filename = secure_filename(file.filename)
                    path = os.path.join(UPLOAD_FOLDER, filename)
                    file.save(path)
                    saved_paths.append(path)
            img_paths_str = ",".join(saved_paths)
            conn.execute("UPDATE catalog SET title=?, desc=?, price=?, img_paths=? WHERE id=?", (title, desc, price, img_paths_str, item_id))
        else:
            conn.execute("UPDATE catalog SET title=?, desc=?, price=? WHERE id=?", (title, desc, price, item_id))
            
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"status": "error"}), 500

@app.route('/submit_ad', methods=['POST', 'OPTIONS'])
def submit_ad():
    if request.method == 'OPTIONS': return '', 204
    try:
        chat_id = request.form.get('chat_id')
        source = request.form.get('source', 'manual')
        time_str = request.form.get('time', '10:00')
        is_daily = 1 if request.form.get('daily') in ['true', 'True', 'on', '1'] else 0

        text, img_paths_str = "", ""
        if source == 'catalog':
            cat_id = request.form.get('catalog_id')
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("SELECT title, desc, price, img_paths FROM catalog WHERE id=?", (cat_id,)).fetchone()
            conn.close()
            if row:
                text = f"🏠 {row[0]}\n\n{row[1]}\n\n💰 Цена: {row[2]}"
                img_paths_str = row[3] or ""
        else:
            text = request.form.get('text', 'Пусто')
            saved_paths = []
            if 'images' in request.files:
                for file in request.files.getlist('images'):
                    if file.filename:
                        filename = secure_filename(file.filename)
                        path = os.path.join(UPLOAD_FOLDER, filename)
                        file.save(path)
                        saved_paths.append(path)
            img_paths_str = ",".join(saved_paths)

        hour, minute = map(int, time_str.split(':'))
        job_id = f"job_{int(time.time())}"
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO jobs (id, chat_id, text, time, is_daily, img_paths) VALUES (?, ?, ?, ?, ?, ?)", 
                     (job_id, chat_id, text, time_str, is_daily, img_paths_str))
        conn.commit()
        conn.close()

        if is_daily:
            scheduler.add_job(scheduled_job, 'cron', hour=hour, minute=minute, args=[chat_id, text, time_str, is_daily, img_paths_str, job_id], id=job_id)
        else:
            # Если разовая, ставим на сегодня/завтра
            scheduler.add_job(scheduled_job, 'cron', hour=hour, minute=minute, args=[chat_id, text, time_str, is_daily, img_paths_str, job_id], id=job_id)

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"Ошибка submit_ad: {e}", flush=True)
        return jsonify({"status": "error"}), 500

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
        time_str = request.form.get('time')
        date_str = request.form.get('date')

        client_id = str(int(time.time()))
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO clients (id, name, client_chat_id, text, schedule_time, status) VALUES (?, ?, ?, ?, ?, ?)", 
                     (client_id, name, '', text, f"{date_str} {time_str}", "Ожидает привязки"))
        conn.commit()
        conn.close()

        return jsonify({"status": "ok", "client_id": client_id}), 200
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

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

                    if update_type == "bot_started":
                        payload = u.get("payload") 
                        if payload:
                            client_id = payload
                            conn = sqlite3.connect(DB_PATH)
                            client = conn.execute("SELECT text, schedule_time FROM clients WHERE id=?", (client_id,)).fetchone()
                            if client:
                                text, schedule_time = client
                                conn.execute("UPDATE clients SET client_chat_id=?, status='Запланировано' WHERE id=?", (chat_id, client_id))
                                conn.commit()
                                
                                # Парсим дату и время
                                date_part, time_part = schedule_time.split(' ')
                                hour, minute = map(int, time_part.split(':'))
                                year, month, day = map(int, date_part.split('-'))
                                
                                # Ставим задачу на конкретную дату
                                scheduler.add_job(
                                    send_client_message, 
                                    'date', 
                                    run_date=datetime(year, month, day, hour, minute), 
                                    args=[chat_id, text, client_id], 
                                    id=f"client_{client_id}"
                                )
                                send_max_message(chat_id, f"Отлично! Я напомню вам {date_part} в {time_part}.")
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
