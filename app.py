import os
import requests
import time
import urllib3
from flask import Flask, request, jsonify
from flask_cors import CORS
from threading import Thread

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- НАСТРОЙКИ ---
TOKEN = "f9LHodD0cOLMdTsqq6Aayvaoutp2PBcYlCKXPS7dEg83rYK5B88u3b-KlPiCuCsRPUmJ3Vl6ruCUgOSCqFig"
BASE_URL = "https://platform-api2.max.ru"
MY_CHAT_ID = 505757849 

# База данных чатов в памяти
KNOWN_CHATS = []

headers = {"Authorization": TOKEN, "Content-Type": "application/json"}
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# --- 1. ОТПРАВКА В MAX ---
def send_max_message(chat_id, text):
    try:
        response = requests.post(f"{BASE_URL}/messages", headers=headers, params={"chat_id": chat_id}, json={"text": text}, verify=False)
        print(f"-> Отправлено в MAX (чат {chat_id}). Статус: {response.status_code}", flush=True)
        return response.status_code == 200
    except Exception as e:
        print("-> Ошибка MAX:", e, flush=True)
        return False

# --- 2. МАРШРУТЫ ДЛЯ САЙТА (LAYERO) ---

@app.route('/')
def home():
    return "Сервер НЕПРОСТОПОСТ работает!"

# Возвращаем список сохраненных чатов
@app.route('/get_chats', methods=['GET'])
def get_chats():
    print(f"Запрос чатов. Текущий список: {KNOWN_CHATS}", flush=True)
    return jsonify({"chats": KNOWN_CHATS})

# Принимаем данные для рассылки
@app.route('/submit_ad', methods=['POST', 'OPTIONS'])
def submit_ad():
    if request.method == 'OPTIONS': return '', 204
    try:
        data = request.get_json(silent=True)
        print(f"\n[ДАННЫЕ ОТ САЙТА]: {data}", flush=True)
        
        chat_id = data.get('chat_id', MY_CHAT_ID) 
        text = data.get('text', 'Пусто')
        
        success = send_max_message(chat_id, f"✅ Рассылка запущена:\n\n{text}")
        return jsonify({"status": "ok" if success else "error"}), (200 if success else 500)
    except Exception as e:
        print("Ошибка в submit_ad:", e, flush=True)
        return jsonify({"status": "error"}), 500

# --- 3. ЛОГИКА БОТА ---
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
                        update_type = update.get("update_type")
                        
                        # Пытаемся достать chat_id из любого события
                        chat_id = update.get("chat_id")
                        if not chat_id and update.get("message"):
                            chat_id = update["message"].get("recipient", {}).get("chat_id")

                        if chat_id:
                            # Проверяем, не личка ли это (нам нужны только группы)
                            is_dialog = False
                            if update.get("message") and update["message"].get("recipient", {}).get("chat", {}).get("type") == "dialog":
                                is_dialog = True
                            elif update.get("chat") and update["chat"].get("type") == "dialog":
                                is_dialog = True

                            if not is_dialog:
                                # Если группы еще нет в списке — добавляем
                                if chat_id not in [c['id'] for c in KNOWN_CHATS]:
                                    title = f"Группа {chat_id}"
                                    if update.get("chat") and update["chat"].get("title"):
                                        title = update["chat"]["title"]
                                    elif update.get("message") and update["message"].get("recipient", {}).get("chat", {}).get("title"):
                                        title = update["message"]["recipient"]["chat"]["title"]
                                    
                                    KNOWN_CHATS.append({"id": chat_id, "title": title})
                                    print(f"✅ НОВЫЙ ЧАТ ДОБАВЛЕН: {title} ({chat_id})", flush=True)
                                    send_max_message(chat_id, "Привет! Я бот НЕПРОСТОПОСТ. Теперь я буду публиковать здесь ваши объявления.")
                                    
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
