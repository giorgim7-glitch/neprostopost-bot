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

# Указываем часовой пояс (Москва)
MOSCOW_TZ = pytz.timezone('Europe/Moscow')
scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
scheduler.start()

headers = {"Authorization": TOKEN}
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# --- 1. ФУНКЦИЯ ЗАГРУЗКИ ФАЙЛА В MAX (ИСПРАВЛЕНА) ---
def upload_file_to_max(filepath, file_type='image'):
    try:
        upload_url = f"{BASE_URL}/uploads"
        # MAX требует передавать тип файла в параметрах запроса
        params = {"type": file_type} 
        
        with open(filepath, 'rb') as f:
            files = {'file': f}
            res = requests.post(upload_url, headers=headers, params=params, files=files, verify=False)
            
            if res.status_code == 200:
                return {"type": file_type, "payload": {"token": res.json().get('token')}}
            else:
                print(f"Ошибка загрузки файла: {res.text}", flush=True)
    except Exception as e:
        print(f"Исключение при загрузке: {e}", flush=True)
    return None

# --- 2. ОТПРАВКА СООБЩЕНИЯ ---
def send_max_message(chat_id, text, media_paths=None):
    try:
        attachments = []
        if media_paths:
            for path, ftype in media_paths:
                att = upload_file_to_max(path, ftype)
                if att: attachments.append(att)
        
        payload = {"text": text}
        if attachments:
            payload["attachments"] = attachments

        response = requests.post(f"{BASE_URL}/messages", headers=headers, params={"chat_id": chat_id}, json=payload, verify=False)
        print(f"-> Отправлено в MAX (чат {chat_id}). Статус: {response.status_code}", flush=True)
        return response.status_code == 200
    except Exception as e:
        print("-> Ошибка отправки в MAX:", e, flush=True)
        return False

# --- 3. ЗАДАЧА ДЛЯ ПЛАНИРОВЩИКА ---
def scheduled_job(chat_id, text, media_paths, job_id):
    print(f"⏰ СРАБОТАЛ ПЛАНИРОВЩИК для чата {chat_id} (ID: {job_id})", flush=True)
    send_max_message(chat_id, text, media_paths)
    
    # Удаляем задачу из списка активных после отправки
    global SCHEDULED_JOBS
    SCHEDULED_JOBS = [j for j in SCHEDULED_JOBS if j['id'] != job_id]

# --- 4. МАРШРУТЫ ---
@app.route('/')
def home(): return "Сервер НЕПРОСТОПОСТ работает!"

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
        is_daily = request.form.get('daily', 'false') == 'true'

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
        job_info = {"id": job_id, "time": time_str, "text": text[:20] + "..."}

        if is_daily:
            scheduler.add_job(scheduled_job, 'cron', hour=hour, minute=minute, args=[chat_id, text, media_paths, job_id], id=job_id)
            job_info["schedule"] = f"Каждый день в {time_str}"
        else:
            if date_str:
                year, month, day = map(int, date_str.split('-'))
                scheduler.add_job(scheduled_job, 'date', run_date=f"{year}-{month}-{day} {hour}:{minute}:00", args=[chat_id, text, media_paths, job_id], id=job_id)
                job_info["schedule"] = f"{date_str} в {time_str}"
            else:
                scheduler.add_job(scheduled_job, 'cron', hour=hour, minute=minute, args=[chat_id, text, media_paths, job_id], id=job_id)
                job_info["schedule"] = f"Сегодня в {time_str}"

        SCHEDULED_JOBS.append(job_info)
        print(f"✅ Задача {job_id} запланирована: {job_info['schedule']} (МСК)", flush=True)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"!! Ошибка: {e}", flush=True)
        return jsonify({"status": "error"}), 500

# --- 5. ЦИКЛ БОТА (СЛУШАЕТ MAX) ---
def run_bot():
    print("🚀 Бот НЕПРОСТОПОСТ запущен...", flush=True)
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
                        update_type = update.get("update_type")
                        
                        chat_id = update.get("chat_id")
                        if not chat_id and update.get("message"):
                            chat_id = update["message"].get("recipient", {}).get("chat_id")

                        if chat_id:
                            is_dialog = False
                            if update.get("message") and update["message"].get("recipient", {}).get("chat", {}).get("type") == "dialog":
                                is_dialog = True
                            elif update.get("chat") and update["chat"].get("type") == "dialog":
                                is_dialog = True

                            if not is_dialog:
                                if chat_id not in [c['id'] for c in KNOWN_CHATS]:
                                    title = f"Группа {chat_id}"
                                    if update.get("chat") and update["chat"].get("title"):
                                        title = update["chat"]["title"]
                                    elif update.get("message") and update["message"].get("recipient", {}).get("chat", {}).get("title"):
                                        title = update["message"]["recipient"]["chat"]["title"]
                                    
                                    KNOWN_CHATS.append({"id": chat_id, "title": title})
                                    print(f"✅ НОВЫЙ ЧАТ ДОБАВЛЕН: {title} ({chat_id})", flush=True)
                                    send_max_message(chat_id, "Привет! Я бот НЕПРОСТОПОСТ. Теперь я буду публиковать здесь ваши объявления по расписанию.")
                                    
                        if update_type == "bot_started":
                            send_max_message(update.get("chat_id"), "Привет! Добавь меня в группу и сделай админом, чтобы я мог рассылать объявления.")
        except Exception as e:
            print("Ошибка цикла бота:", e, flush=True)
            time.sleep(5)
        time.sleep(1)

if __name__ == '__main__':
    t = Thread(target=run_bot)
    t.daemon = True
    t.start()
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
