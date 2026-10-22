"""Single delivery worker. Start separately from Gunicorn: python worker.py."""
import fcntl
import json
import logging
import mimetypes
import time
from datetime import timedelta
from urllib.parse import urlsplit

import requests
import app as backend

LOG = logging.getLogger('neprostopost.worker')


def finish(job_id, status, error=None, message_id=None):
    with backend.db() as db:
        db.execute('UPDATE app_jobs SET status=?,last_error=?,message_id=? WHERE id=?',
                   (status, error, message_id, job_id))
        db.commit()


def retry(job, error):
    if job['attempts'] >= 3:
        finish(job['id'], 'failed', error)
        return
    when = backend.iso(backend.utcnow() + timedelta(seconds=30 * 2 ** (job['attempts'] - 1)))
    with backend.db() as db:
        db.execute("UPDATE app_jobs SET status='scheduled',last_error=?,run_at=? WHERE id=?", (error, when, job['id']))
        db.commit()


def claim():
    with backend.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM app_jobs WHERE status='scheduled' AND run_at<=? ORDER BY run_at LIMIT 1",
                         (backend.iso(backend.utcnow()),)).fetchone()
        if not row:
            return None
        db.execute("UPDATE app_jobs SET status='processing',attempts=attempts+1 WHERE id=?", (row['id'],))
        db.commit()
        return dict(row, attempts=row['attempts'] + 1)


def upload_images(names):
    attachments = []
    for name in names:
        response = backend.max_api('POST', '/uploads', params={'type': 'image'})
        response.raise_for_status()
        url = response.json()['url']
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Invalid upload URL')
        with (backend.UPLOAD / name).open('rb') as image:
            uploaded = requests.post(url, files={'data': (name, image, mimetypes.guess_type(name)[0])},
                                     timeout=60, allow_redirects=False)
        uploaded.raise_for_status()
        photos = uploaded.json()['photos']
        if not isinstance(photos, dict) or not photos:
            raise ValueError('Invalid upload result')
        attachments.append({'type': 'image', 'payload': {'photos': photos}})
    return attachments


def deliver(job):
    with backend.db() as db:
        connected = db.execute('SELECT 1 FROM app_groups WHERE owner_id=? AND chat_id=?',
                               (job['owner_id'], job['chat_id'])).fetchone()
    if not connected:
        finish(job['id'], 'cancelled', 'Группа отключена')
        return
    permission = backend.get_admin_group(job['chat_id'], job['owner_id'])
    if permission is None:
        retry(job, 'MAX временно не подтвердил права администратора')
        return
    if not permission:
        finish(job['id'], 'failed', 'У пользователя больше нет прав администратора группы')
        return
    try:
        attachments = upload_images(backend.image_names(job['images']))
    except (requests.RequestException, ValueError, KeyError, OSError, RuntimeError):
        retry(job, 'Не удалось загрузить вложения в MAX')
        return
    body = {'text': job['text']}
    if attachments:
        body['attachments'] = attachments
    try:
        response = backend.max_api('POST', '/messages', params={'chat_id': job['chat_id']}, json=body)
    except requests.RequestException:
        # MAX could already have accepted the POST. Never resend ambiguous deliveries.
        finish(job['id'], 'uncertain', 'Ответ MAX не получен. Проверьте доставку в группе перед повторной отправкой.')
        return
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code == 429 or data.get('code') == 'attachment.not.ready':
        retry(job, 'MAX просит повторить отправку позднее')
    elif response.status_code >= 500:
        finish(job['id'], 'uncertain', 'Сбой MAX. Проверьте доставку в группе перед повторной отправкой.')
    elif not response.ok:
        finish(job['id'], 'failed', f'MAX отклонил отправку (HTTP {response.status_code})')
    else:
        message_id = data.get('message', {}).get('body', {}).get('mid')
        if message_id:
            finish(job['id'], 'sent', message_id=message_id)
        else:
            finish(job['id'], 'uncertain', 'MAX не вернул ID сообщения. Проверьте доставку в группе.')


def run():
    if not backend.TOKEN:
        raise SystemExit('MAX_BOT_TOKEN не задан: worker не запущен')
    with backend.DB.with_suffix(backend.DB.suffix + '.worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Worker для этой базы данных уже запущен')
        with backend.db() as db:
            db.execute("UPDATE app_jobs SET status='uncertain',last_error='Worker прервался. Проверьте доставку в MAX.' WHERE status='processing'")
            db.commit()
        LOG.info('Worker started')
        while True:
            job = claim()
            if not job:
                time.sleep(2)
                continue
            try:
                deliver(job)
            except Exception:
                # Do not log response bodies, upload URLs or credentials.
                LOG.error('Unexpected delivery failure for job %s', job['id'])
                finish(job['id'], 'uncertain', 'Ошибка worker. Проверьте доставку перед повторной отправкой.')


if __name__ == '__main__':
    run()
