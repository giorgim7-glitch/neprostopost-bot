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
                        
                        # Ловим любые события, где есть chat_id (будь то добавление или сообщение)
                        chat_id = update.get("chat_id")
                        if not chat_id and update.get("message"):
                            chat_id = update["message"].get("recipient", {}).get("chat_id")

                        if chat_id:
                            # Пытаемся узнать название чата (если есть)
                            chat_title = "Группа " + str(chat_id)
                            if update.get("chat") and update["chat"].get("title"):
                                chat_title = update["chat"]["title"]
                            elif update.get("message") and update["message"].get("recipient", {}).get("chat", {}).get("title"):
                                chat_title = update["message"]["recipient"]["chat"]["title"]

                            # Если этого чата еще нет в списке, добавляем
                            if chat_id not in [c['id'] for c in KNOWN_CHATS]:
                                # Игнорируем личные сообщения (проверяем, что это не личка)
                                is_private = update.get("message", {}).get("recipient", {}).get("chat", {}).get("type") == "dialog"
                                if not is_private:
                                    KNOWN_CHATS.append({"id": chat_id, "title": chat_title})
                                    print(f"✅ Новый чат добавлен в базу: {chat_title} ({chat_id})", flush=True)
                                    send_max_message(chat_id, f"Привет! Я бот НЕПРОСТОПОСТ. Теперь я буду публиковать здесь объявления.")
                                    
                        # Отвечаем на /start в личке
                        if update_type == "bot_started":
                            send_max_message(update.get("chat_id"), "Привет! Добавь меня в свою группу и сделай админом, чтобы я мог рассылать объявления.")
        except Exception as e:
            print("Ошибка цикла:", e, flush=True)
            time.sleep(5)
        time.sleep(1)
