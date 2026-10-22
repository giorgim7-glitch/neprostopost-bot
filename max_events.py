"""Remember groups observed in supported MAX events; never persist message text."""
import re
import requests
import app as backend


def state(key, default=None):
    with backend.db() as db:
        row = db.execute('SELECT value FROM app_bot_state WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def set_error(message):
    with backend.db() as db:
        db.execute('INSERT OR REPLACE INTO app_bot_state VALUES(?,?)', ('updates_error', message))
        db.commit()


def remember(db, event):
    if not isinstance(event, dict):
        return
    kind = event.get('update_type')
    title = None
    active = 1
    if kind == 'message_created':
        message = event.get('message')
        recipient = message.get('recipient') if isinstance(message, dict) else None
        if not isinstance(recipient, dict) or recipient.get('chat_type') not in ('chat', 'channel'):
            return
        chat_id = recipient.get('chat_id')
    elif kind in ('bot_added', 'bot_removed', 'chat_title_changed'):
        chat_id = event.get('chat_id')
        active = int(kind != 'bot_removed')
        if kind == 'chat_title_changed' and isinstance(event.get('title'), str):
            title = event['title']
    else:
        return
    chat_id = str(chat_id)
    if not re.fullmatch(r'-?\d+', chat_id):
        return
    db.execute('''INSERT INTO app_known_chats(chat_id,title,active,updated_at) VALUES(?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET title=COALESCE(excluded.title,app_known_chats.title),
        active=excluded.active,updated_at=excluded.updated_at''',
        (chat_id, title, active, backend.iso(backend.utcnow())))
    if not active:
        db.execute("UPDATE app_jobs SET status='cancelled',last_error='Бот удалён из группы' WHERE chat_id=? AND status='scheduled'", (chat_id,))


def poll_updates():
    params = {'limit': 100, 'timeout': 0, 'types': 'bot_added,bot_removed,message_created,chat_title_changed'}
    marker = state('updates_marker')
    if marker is not None:
        params['marker'] = marker
    try:
        response = backend.max_api('GET', '/updates', params=params, timeout=8)
        if not response.ok:
            descriptions = {401: 'MAX отклонил токен бота', 403: 'MAX запретил получение событий',
                            409: 'MAX отклонил polling: проверьте webhook или другой процесс бота'}
            set_error(descriptions.get(response.status_code, 'MAX не отдал события') + f' (HTTP {response.status_code}).')
            return False
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('updates'), list):
            raise ValueError('Invalid update response')
        marker = payload.get('marker')
        if marker is None or not re.fullmatch(r'\d{1,30}', str(marker)):
            raise ValueError('Invalid marker')
        with backend.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for event in payload['updates']:
                remember(db, event)
            # Commit the cursor and events together: restarting cannot lose an observed group.
            db.execute('INSERT OR REPLACE INTO app_bot_state VALUES(?,?)', ('updates_marker', str(marker)))
            db.execute('INSERT OR REPLACE INTO app_bot_state VALUES(?,?)', ('updates_error', ''))
            db.execute('INSERT OR REPLACE INTO app_bot_state VALUES(?,?)', ('updates_checked_at', backend.iso(backend.utcnow())))
            db.commit()
        return True
    except requests.Timeout:
        set_error('Ботхост не дождался ответа MAX при получении событий.')
    except requests.ConnectionError:
        set_error('Ботхост не смог подключиться к MAX для получения событий.')
    except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError):
        set_error('Не удалось обработать ответ MAX с событиями бота.')
    return False
