"""Private MAX reminders to the planner owner; never send to a client automatically."""
from datetime import timedelta
import requests
import app


def process_one():
    with app.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("SELECT * FROM app_clients WHERE reminder_status='scheduled' AND reminder_at<=? ORDER BY reminder_at LIMIT 1",(app.iso(app.utcnow()),)).fetchone()
        if not row: return
        if not app.has_pro(db,row['owner_id']):
            db.execute("UPDATE app_clients SET reminder_status='paused',reminder_error='Подписка PRO не активна' WHERE id=?",(row['id'],)); db.commit(); return
        db.execute("UPDATE app_clients SET reminder_status='processing',reminder_attempts=reminder_attempts+1 WHERE id=?",(row['id'],)); db.commit()
    message='Напоминание: '+row['type']+'\n'+row['name']+'\n'+app.parse_time(row['schedule_time']).astimezone(app.ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')
    if row['phone']: message+='\nТелефон: '+row['phone'][:100]
    if row['text']: message+='\nЗаметка: '+row['text'][:1000]
    if row['prepared_message']: message+='\n\nПодготовленное сообщение клиенту (скопируйте и отправьте вручную):\n'+row['prepared_message'][:2000]
    status='uncertain'; error='Ответ MAX не получен. Проверьте личный чат с ботом.'
    retry_at=None
    try:
        response=app.max_api('POST','/messages',params={'user_id':row['owner_id']},json={'text':message})
        if response.status_code==429 and row['reminder_attempts']<2:
            status='scheduled'; error='MAX временно ограничил отправку'; retry_at=app.iso(app.utcnow()+timedelta(minutes=1))
        elif response.ok:
            data=response.json()
            if data.get('message',{}).get('body',{}).get('mid'): status='sent'; error=None
        elif response.status_code<500:
            status='failed'; error=f'MAX отклонил напоминание (HTTP {response.status_code}). Откройте личный чат с ботом и нажмите «Начать».'
    except (requests.RequestException,ValueError,TypeError,AttributeError): pass
    with app.db() as db:
        db.execute('UPDATE app_clients SET reminder_status=?,reminder_error=?,reminder_at=COALESCE(?,reminder_at) WHERE id=?',(status,error,retry_at,row['id'])); db.commit()
