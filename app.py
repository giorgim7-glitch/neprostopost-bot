import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from threading import Thread

# --- НАСТРОЙКИ ---
TOKEN = "f9LHodD0cOLMdTsqq6Aayvaoutp2PBcYlCKXPS7dEg83rYK5B88u3b-KlPiCuCsRPUmJ3Vl6ruCUgOSCqFig"
BASE_URL = "https://platform-api2.max.ru"
MY_CHAT_ID = 505757849 

headers = {
    "Authorization": TOKEN,
    "Content-Type": "application/json"
}

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# --- 1. ОТПРАВКА В MAX ---
def send_max_message(chat_id, text):
    send_url = f"{BASE_URL}/messages"
    send_params = {"chat_id": chat_id}
    payload = {"text": text}
    
    try:
        response = requests.post(send_url, headers=headers, params=send_params, json=payload)
        print(f"-> Отправлено в MAX. Статус: {response.status_code}", flush=True)
        return response.status_code == 200
    except Exception as e:
        print("-> Ошибка при отправке в MAX:", e, flush=True)
        return False

# --- 2. ПРИЕМ ДАННЫХ ОТ САЙТА ---
@app.route('/')
def home():
    return "Сервер НЕПРОСТОПОСТ работает!"

@app.route('/submit_ad', methods=['POST', 'OPTIONS'])
def submit_ad():
    if request.method == 'OPTIONS':
        return '', 204
        
    try:
        data = request.get_json(silent=True)
        print(f"\n[ДАННЫЕ ОТ САЙТА]: {data}", flush=True)
        
        if not data:
            return jsonify({"status": "error", "message": "Пустой запрос"}), 400
            
        chat_id = data.get('chat_id', MY_CHAT_ID) 
        text = data.get('text', 'Объявление пустое')
        
        success = send_max_message(chat_id, f"✅ Я получил твое объявление:\n\n{text}")
        
        if success:
            return jsonify({"status": "ok"}), 200
        else:
            return jsonify({"status": "error", "message": "MAX не принял сообщение"}), 500
            
    except Exception as e:
        print(f"!! КРИТИЧЕСКАЯ ОШИБКА: {e}", flush=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 3. БОТ (СЛУШАЕТ MAX) ---
def run_bot():
    print("🚀 Бот НЕПРОСТОПОСТ запущен...", flush=True)
    last_marker = None
    while True:
        try:
            params = {"timeout": 30, "types": "message_created,bot_started"}
            if last_marker:
                params["marker"] = last_marker
                
            response = requests.get(f"{BASE_URL}/updates", headers=headers, params=params)
            
            if response.status_code == 200:
                data = response.json()
                if "marker" in data:
                    last_marker = data["marker"]

                if "updates" in data and len(data["updates"]) > 0:
                    for update in data["updates"]:
                        if update.get("update_type") == "bot_started":
                            chat_id = update.get("chat_id")
                            send_max_message(chat_id, "Добро пожаловать в НЕПРОСТОПОСТ!")
        except Exception as e:
            print("Ошибка в цикле бота:", e, flush=True)
            time.sleep(5)
        time.sleep(1)

if __name__ == '__main__':
    bot_thread = Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)