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

MOSCOW_TZ = pytz.timezone('Europe/Moscow')
scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
scheduler.start()

headers = {"Authorization": TOKEN}
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# --- ЗАГРУЗКА ФАЙЛА (ИСПРАВЛЕНА) ---
def upload_file_to_max(filepath, file_type='image'):
    try:
        upload_url_api = f"{BASE_URL}/uploads"
        params = {"type": file_type}
        res = requests.post(upload_url_api, headers=headers, params=params, verify=False)
        
        if res.status_code != 200:
            print(f"❌ Ошибка получения URL: {res.text}", flush=True)
            return None
            
        upload_url = res.json().get('url')
        if not upload_url: return None

        with open(filepath, 'rb') as f:
            files = {'data': f} # ВАЖНО: поле называется 'data'
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
                    print(f"✅ Файл загружен. Токен: {token[:20]}...", flush=True)
                    return {"type": file_type, "payload": {"token": token}}
                else:
                    print(f"❌ Токен не найден: {response_data}", flush=True)
            else:
                print(f"❌ Ошибка загрузки: {upload_res.text}", flush=True)
    except Exception as e:
        print(f"❌ Исключение: {e}", flush=True)
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
        print(f"-> Отправлено в MAX. Статус: {response.status_code}", flush=True)
        if response.status_code != 200:
            print(f"-> Ошибка MAX: {response.text}", flush=True)
        return response.status_code == 200
    except Exception as e:
        print("-> Ошибка отправки:", e, flush=True)
        return False

def scheduled_job(chat_id, text, media_paths, job_id, is_daily):
    print(f"⏰ СРАБОТАЛ ПЛАНИРОВЩИК (ID: {job_id})", flush=True)
    send_max_message(chat_id, text, media_paths)
    
    # УДАЛЯЕМ ЗАДАЧУ, ТОЛЬКО ЕСЛИ ОНА НЕ ЕЖЕДНЕВНАЯ
    if is_daily is False:
        global SCHEDULED_JOBS
        SCHEDULED_JOBS = [j for j in SCHEDULED_JOBS if j['id'] != job_id]
        print(f"🗑️ Разовая задача {job_id} удалена.", flush=True)
    else:
        print(f"🔄 Ежедневная задача {job_id} остается в списке.", flush=True)

@app.route('/')
def home(): return "Сервер работает!"

@app.route('/get_chats', methods=['GET'])
def get_chats(): return jsonify({"chats": KNOWN_CHATS})

@app.route('/get_jobs', methods=['GET'])
def get_jobs(): return jsonify({"jobs": SCHEDULED_JOBS})

@app.route('/submit_ad', methods=['POST', 'OPTIONS'])
def submit_ad():
    if request.method == 'OPTIONS': return '', 204
    try:
        chat_id = request.form.get('chat_id', MY_CHAT_ID)
        text = request.form.get('text', 'Пусто')
        time_str = request.form.get('time', '10:00')
        date_str = request.form.get('date', '')
        
        # ЖЕСТКО ПРИВОДИМ К БУЛЕВУ ЗНАЧЕНИЮ
        is_daily_str = request.form.get('daily', 'false')
        is_daily = True if is_daily_str in ['true', 'True', 'on', '1'] else False

        media_paths = []
        if 'images' in request.files:
            for file in request.files.getlist('images'):
                if file.filename:
                    path = os.path.join('/tmp', secure_filename(file.filename))
                    file.save(path)
                    media_paths.append((path, 'image'))
        if 'video' in request.files and request.files['video'].filename:
            path = os.path.join('/tmp', secure_filename(request.files['video'].filename))
            request.files['video'].save(path)
            media_paths.append((path, 'video'))

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
        print(f"✅ Задача {job_id} запланирована: {job_info['schedule']}", flush=True)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"!! Ошибка: {e}", flush=True)
        return jsonify({"status": "error"}), 500

# ... run_bot() вставь из предыдущего сообщения ...
