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

TOKEN = "f9LHodD0cOLMdTsqq6Aayvaoutp2PBcYlCKXPS7dEg83rYK5B88u3b-KlPiCuCsRPUmJ3Vl6ruCUgOSCqFig"
BASE_URL = "https://platform-api2.max.ru"
MY_CHAT_ID = 505757849 

KNOWN_CHATS = []
SCHEDULED_JOBS = []
CATALOG = [] # <-- НОВАЯ БАЗА ОБЪЕКТОВ

MOSCOW_TZ = pytz.timezone('Europe/Moscow')
scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
scheduler.start()

headers = {"Authorization": TOKEN}
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ... (функции upload_file_to_max и send_max_message оставляем без изменений) ...
# (они у тебя уже написаны и работают)

def upload_file_to_max(filepath, file_type='image'):
    # ... (код без изменений) ...
    pass

def send_max_message(chat_id, text, media_paths=None):
    # ... (код без изменений) ...
    pass

def scheduled_job(chat_id, text, media_paths, job_id, is_daily):
    # ... (код без изменений) ...
    pass

# --- МАРШРУТЫ ---
@app.route('/')
def home(): return "Сервер работает!"

@app.route('/get_chats', methods=['GET'])
def get_chats(): return jsonify({"chats": KNOWN_CHATS})

@app.route('/get_jobs', methods=['GET'])
def get_jobs(): return jsonify({"jobs": SCHEDULED_JOBS})

# НОВЫЙ МАРШРУТ: Получить каталог
@app.route('/get_catalog', methods=['GET'])
def get_catalog(): return jsonify({"catalog": CATALOG})

# НОВЫЙ МАРШРУТ: Добавить объект в каталог
@app.route('/add_catalog', methods=['POST', 'OPTIONS'])
def add_catalog():
    if request.method == 'OPTIONS': return '', 204
    try:
        title = request.form.get('title', 'Без названия')
        description = request.form.get('description', '')
        price = request.form.get('price', '')
        
        # Пока сохраняем только текст. Фото для каталога можно добавить позже.
        new_item = {"id": str(int(time.time())), "title": title, "description": description, "price": price}
        CATALOG.append(new_item)
        print(f"✅ В каталог добавлен объект: {title}", flush=True)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"status": "error"}), 500

# Обновленный submit_ad (теперь принимает параметр source - из каталога или свой текст)
@app.route('/submit_ad', methods=['POST', 'OPTIONS'])
def submit_ad():
    if request.method == 'OPTIONS': return '', 204
    try:
        chat_id = request.form.get('chat_id', MY_CHAT_ID)
        source = request.form.get('source', 'manual') # 'manual' или 'catalog'
        time_str = request.form.get('time', '10:00')
        date_str = request.form.get('date', '')
        is_daily_str = request.form.get('daily', 'false')
        is_daily = True if is_daily_str in ['true', 'True', 'on', '1'] else False

        text = ""
        media_paths = []

        if source == 'catalog':
            catalog_id = request.form.get('catalog_id')
            item = next((i for i in CATALOG if i['id'] == catalog_id), None)
            if item:
                text = f"🏠 {item['title']}\n\n{item['description']}\n\n💰 Цена: {item['price']}"
            else:
                return jsonify({"status": "error", "message": "Объект не найден"}), 404
        else:
            # Ручной ввод
            text = request.form.get('text', 'Пусто')
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

# ... (run_bot оставляем без изменений) ...
