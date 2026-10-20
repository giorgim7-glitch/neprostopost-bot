import os
import requests
import time
import urllib3
from flask import Flask, request, jsonify
from flask_cors import CORS
from threading import Thread

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOKEN = "f9LHodD0cOLMdTsqq6Aayvaoutp2PBcYlCKXPS7dEg83rYK5B88u3b-KlPiCuCsRPUmJ3Vl6ruCUgOSCqFig"
BASE_URL = "https://platform-api2.max.ru"
MY_CHAT_ID = 505757849 

# Имитация базы данных (пока в памяти)
KNOWN_CHATS = [] 

headers = {"Authorization": TOKEN, "Content-Type": "application/json"}
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

def send_max_message(chat_id, text):
    try:
        response = requests.post(f"{BASE_URL}/messages", headers=headers, params={"chat_id": chat_id}, json={"text": text}, verify=False)
        print(f"-> Отправлено в MAX (чат {chat_id}). Статус: {response.status_code}", flush=True)
        return response.status_code == 200
    except Exception as e:
        print("-> Ошибка MAX:", e, flush=True)
        return False

@app.route('/')
def home():
    return "Сервер работает!"

# Эндпоинт для получения списка чатов (для мини-аппа)
@app.route('/get_chats', methods=['GET'])
def get_chats():
    return jsonify({"chats": KNOWN_CHATS})

# Эндпоинт для запуска рассылки
@app.route('/submit_ad', methods=['POST', 'OPTIONS'])
def submit_ad():
    if request.method == 'OPTIONS': return '', 204
    try:
        data = request.get_json(silent=True)
        print(f"\n[ДАННЫЕ ОТ САЙТА]: {data}", flush=True)
        
        chat_id = data.get('chat_id', MY_CHAT_ID) 
        text = data.get('text', 'Пусто')
        
        # В реальности здесь мы бы брали chat_id из списка выбранных групп
        success = send_max_message(chat_id, f"✅ Рассылка запущена:\n\n{text}")
        return jsonify({"status": "ok" if success else "error"}), (200 if success else 500)
    except Exception as e:
        return jsonify({"status": "error"}), 500

def run_bot():
    print("🚀 Бот запущен и слушает MAX...", flush=True)
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
                        # Если бота добавили в чат/группу
                        if update.get("update_type") == "bot_added":
                            chat_id = update.get("chat_id")
                            title = update.get("chat", {}).get("title", "Без названия")
                            if chat_id not in [c['id'] for c in KNOWN_CHATS]:
                                KNOWN_CHATS.append({"id": chat_id, "title": title})
                                print(f"✅ Бот добавлен в новый чат: {title} ({chat_id})", flush=True)
                                send_max_message(chat_id, f"Привет! Я бот НЕПРОСТОПОСТ. Теперь я буду публиковать здесь ваши объявления по расписанию.")
                                
                        # Если написали /start
                        elif update.get("update_type") == "bot_started":
                            send_max_message(update.get("chat_id"), "Привет! Добавь меня в свою группу и сделай админом, чтобы я мог рассылать объявления.")
        except Exception as e:
            print("Ошибка цикла:", e, flush=True)
            time.sleep(5)
        time.sleep(1)

if __name__ == '__main__':
    t = Thread(target=run_bot)
    t.daemon = True
    t.start()
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
