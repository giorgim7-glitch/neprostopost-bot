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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- НАСТРОЙКИ ---
TOKEN = "f9LHodD0cOLMdTsqq6Aayvaoutp2PBcYlCKXPS7dEg83rYK5B88u3b-KlPiCuCsRPUmJ3Vl6ruCUgOSCqFig"
BASE_URL = "https://platform-api2.max.ru"
MY_CHAT_ID = 505757849 

KNOWN_CHATS = []
SCHEDULED_JOBS = []
CATALOG = [] # База объектов

MOSCOW_TZ = pytz.timezone('Europe/Moscow')
scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
scheduler.start()

headers = {"Authorization": TOKEN}
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# --- ФУНКЦИИ ---
def upload_file_to_max(filepath, file_type='image'):
    try:
        upload_url_api = f"{BASE_URL}/uploads"
        params = {"type": file_type}
        res = requests.post(upload_url_api, headers=headers, params=params, verify=False)
        if res.status_code != 200: return None
        upload_url = res.json().get('url')
        if not upload_url: return None

        with open(filepath, 'rb') as f:
            files = {'file': f}
            upload_res = requests.post(upload_url, files=files, verify=False)
            if upload_res.status_code == 200:
                response_data = upload_res.json()
                token = None
                if file_type == 'image':
                    photos = response_data.get('photos', {})
                    if photos:
                        first_photo = next(iter(photos.values()))
                        token = first_photo.get('token')
                else:
                    token = response_data.get('token')
                if token:
                    return {"type": file_type, "payload": {"token": token}}
    except Exception as e:
        print(f"❌ Ошибка загрузки: {e}", flush=True)
    return None

def send_max_message(chat_id, text, media_paths=None):
    try:
        attachments = []
        if media_paths:
            for path, ftype in media_paths:
                att = upload_file_to_max(path, ftype)
                if att: attachments.append(att)
        
        payload = {"text": text}
        if attachments: payload["attachments"] = attachments

        response = requests.post(f"{BASE_URL}/messages", headers=headers, params={"chat_id": chat_id}, json=payload, verify=False)
        return response.status_code == 200
    except Exception as e:
        return False

def scheduled_job(chat_id, text, media_paths, job_id, is_daily):
    print(f"⏰ Сработал планировщик для {chat_id}", flush=True)
    send_max_message(chat_id, text, media_paths)
    if is_daily is False:
        global SCHEDULED_JOBS
        SCHEDULED_JOBS = [j for j in SCHEDULED_JOBS if j['id'] != job_id]

# --- МАРШРУТЫ ---
@app.route('/')
def home(): return "Сервер работает!"

@app.route('/get_chats', methods=['GET'])
def get_chats(): return jsonify({"chats": KNOWN_CHATS})

@app.route('/get_jobs', methods=['GET'])
def get_jobs(): return jsonify({"jobs": SCHEDULED_JOBS})

@app.route('/get_catalog', methods=['GET'])
def get_catalog(): return jsonify({"catalog": CATALOG})

@app.route('/add_catalog', methods=['POST', 'OPTIONS'])
def add_catalog():
    if request.method == 'OPTIONS': return '', 204
    try:
        new_item = {
            "id": str(int(time.time())), 
            "title": request.form.get('title', 'Без названия'), 
            "description": request.form.get('description', ''), 
            "price": request.form.get('price', '')
        }
        CATALOG.append(new_item)
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"status": "error"}), 500

@app.route('/submit_ad', methods=['POST', 'OPTIONS'])
def submit_ad():
    if request.method == 'OPTIONS': return '', 204
    try:
        chat_id = request.form.get('chat_id', MY_CHAT_ID)
        source = request.form.get('source', 'manual')
        time_str = request.form.get('time', '10:00')
        date_str = request.form.get('date', '')
        is_daily = request.form.get('daily') in ['true', 'True', 'on', '1']

        text = ""
        media_paths = []

        if source == 'catalog':
            catalog_id = request.form.get('catalog_id')
            item = next((i for i in CATALOG if i['id'] == catalog_id), None)
            if item:
                text = f"🏠 {item['title']}\n\n{item['description']}\n\n💰 Цена: {item['price']}"
            else: return jsonify({"status": "error"}), 404
        else:
            text = request.form.get('text', 'Пусто')
            if 'images' in request.files:
                for file in request.files.getlist('images'):
                    if file.filename:
                        path = os.path.join('/tmp', secure_filename(file.filename))
                        file.save(path)
                        media_paths.append((path, 'image'))

        hour, minute = map(int, time_str.split(':'))
        job_id = f"job_{int(time.time())}"
        job_info = {"id": job_id, "time": time_str, "text": text[:20] + "...", "is_daily": is_daily}

        if is_daily:
            scheduler.add_job(scheduled_job, 'cron', hour=hour, minute=minute, args=[chat_id, text, media_paths, job_id, is_daily], id=job_id)
            job_info["schedule"] = f"Каждый день в {time_str}"
        else:
            if date_str:
                year, month, day = map(int, date_str.split('-'))
                scheduler.add_job(scheduled_job, 'date', run_date=f"{year}-{month}-{day} {hour}:{minute}:00", args=[chat_id, text, media_paths, job_id, is_daily], id=job_id)
                job_info["schedule"] = f"{date_str} в {time_str}"
            else:
                scheduler.add_job(scheduled_job, 'cron', hour=hour, minute=minute, args=[chat_id, text, media_paths, job_id, is_daily], id=job_id)
                job_info["schedule"] = f"Сегодня в {time_str}"

        SCHEDULED_JOBS.append(job_info)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"!! Ошибка: {e}", flush=True)
        return jsonify({"status": "error"}), 500

# --- БОТ ---
def run_bot():
    print("🚀 Бот запущен...", flush=True)
    last_marker = None
    while True:
        try:
            params = {"timeout": 30, "types": "message_created,bot_started"}
            if last_marker: params["marker"] = last_marker
            response = requests.get(f"{BASE_URL}/updates", headers=headers, params=params, verify=False)
            if response.status_code == 200:
                data = response.json()
                if "marker" in data: last_marker = data["marker"]
                if "updates" in data:
                    for update in data["updates"]:
                        chat_id = update.get("chat_id") or update.get("message", {}).get("recipient", {}).get("chat_id")
                        if chat_id:
                            is_dialog = update.get("message", {}).get("recipient", {}).get("chat", {}).get("type") == "dialog"
                            if not is_dialog and chat_id not in [c['id'] for c in KNOWN_CHATS]:
                                title = update.get("chat", {}).get("title") or f"Группа {chat_id}"
                                KNOWN_CHATS.append({"id": chat_id, "title": title})
                                print(f"✅ НОВЫЙ ЧАТ: {title}", flush=True)
                                send_max_message(chat_id, "Привет! Я бот НЕПРОСТОПОСТ.")
                        if update.get("update_type") == "bot_started":
                            send_max_message(update.get("chat_id"), "Привет! Добавь меня в группу.")
        except Exception as e:
            time.sleep(5)
        time.sleep(1)

if __name__ == '__main__':
    t = Thread(target=run_bot)
    t.daemon = True
    t.start()
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
