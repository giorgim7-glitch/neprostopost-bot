import os, json, uuid, hmac, hashlib, sqlite3, logging, re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import parse_qsl
import requests
from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

TOKEN=os.getenv('MAX_BOT_TOKEN','').strip()
BASE='https://platform-api2.max.ru'
DATA=Path(os.getenv('DATA_DIR','/app/data'))
DATA.mkdir(parents=True,exist_ok=True)
DB=Path(os.getenv('DB_PATH',str(DATA/'data.db')))
DB.parent.mkdir(parents=True,exist_ok=True)
UPLOAD=Path(os.getenv('UPLOAD_FOLDER',str(DATA/'uploads')))
UPLOAD.mkdir(parents=True,exist_ok=True)
LOG=logging.getLogger('neprostopost')
logging.basicConfig(level=logging.INFO)
app=Flask(__name__)
app.config['MAX_CONTENT_LENGTH']=20*1024*1024
# Authorization uses custom header, no cross-origin cookie credentials.
CORS(app,resources={r'/api/*':{'origins':os.getenv('FRONTEND_ORIGIN','*').split(','),'allow_headers':['Authorization','Content-Type']}})

def db():
    c=sqlite3.connect(str(DB),timeout=30)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=30000')
    return c

def init_db():
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS app_users (id TEXT PRIMARY KEY, first_name TEXT, created_at TEXT NOT NULL, trial_end TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS app_sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS app_catalog (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT, price TEXT, tag TEXT, address TEXT, category TEXT, images TEXT, created_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS app_clients (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL, phone TEXT, text TEXT, type TEXT, schedule_time TEXT NOT NULL, status TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS app_groups (owner_id TEXT NOT NULL, chat_id TEXT NOT NULL, title TEXT NOT NULL, PRIMARY KEY(owner_id,chat_id))')
        c.execute('CREATE TABLE IF NOT EXISTS app_jobs (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, chat_id TEXT NOT NULL, text TEXT NOT NULL, run_at TEXT NOT NULL, repeat_daily INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT "scheduled", images TEXT NOT NULL DEFAULT "[]")')
        c.execute('CREATE INDEX IF NOT EXISTS idx_app_jobs_due ON app_jobs(status,run_at)')
        c.commit()
init_db()

def utcnow(): return datetime.now(timezone.utc)
def iso(d): return d.astimezone(timezone.utc).isoformat()
def parse_time(s):
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('Не указан часовой пояс')
    return d.astimezone(timezone.utc)

def verify_max(init_data):
    if not TOKEN or not isinstance(init_data,str) or len(init_data)>20000: return None
    try:
        pairs=parse_qsl(init_data,keep_blank_values=True,strict_parsing=True)
        keys=[k for k,v in pairs]
        if len(set(keys))!=len(keys) or keys.count('hash')!=1: return None
        data=dict(pairs)
        expected=data.pop('hash')
        content='\n'.join(f'{k}={v}' for k,v in sorted(data.items()))
        secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
        actual=hmac.new(secret,content.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual,expected): return None
        issued=int(data['auth_date'])
        now=int(utcnow().timestamp())
        if issued>now+60 or now-issued>3600: return None
        user=json.loads(data['user'])
        if not isinstance(user,dict) or not isinstance(user.get('id'),int): return None
        return user
    except (ValueError,KeyError,TypeError,json.JSONDecodeError): return None

def require_auth(fn):
    @wraps(fn)
    def wrapper(*a,**kw):
        auth=request.headers.get('Authorization','')
        if not auth.startswith('Bearer '): return jsonify(message='Нужна авторизация через MAX'),401
        digest=hashlib.sha256(auth[7:].encode()).hexdigest()
        with db() as c:
            row=c.execute('SELECT user_id,expires_at FROM app_sessions WHERE token_hash=?',(digest,)).fetchone()
        if not row or parse_time(row['expires_at'])<=utcnow(): return jsonify(message='Сессия истекла. Откройте приложение заново.'),401
        g.user_id=row['user_id']
        return fn(*a,**kw)
    return wrapper

def max_api(method,path,**kw):
    if not TOKEN: raise RuntimeError('MAX_BOT_TOKEN не задан')
    return requests.request(method,BASE+path,headers={'Authorization':TOKEN},timeout=25,**kw)

def get_admin_group(chat_id,user_id):
    if not re.fullmatch(r'-?\d+',str(chat_id)): return None
    try:
        response=max_api('GET',f'/chats/{chat_id}/members/admins')
        response.raise_for_status()
        data=response.json()
        for admin in data.get('members',[]):
            u=admin.get('user') if isinstance(admin.get('user'),dict) else admin
            if str(u.get('user_id',u.get('id','')))==str(user_id):
                return True
        return False
    except (requests.RequestException,ValueError,TypeError,KeyError):
        LOG.exception('Не удалось проверить права администратора MAX')
        return None

def owned_group(chat_id):
    with db() as c:
        return c.execute('SELECT 1 FROM app_groups WHERE owner_id=? AND chat_id=?',(g.user_id,str(chat_id))).fetchone() is not None

def image_names(value):
    try: names=json.loads(value or '[]')
    except (ValueError,TypeError): return []
    return [n for n in names if isinstance(n,str) and re.fullmatch(r'[a-f0-9]{32}\.(jpg|jpeg|png|webp|gif)',n)]

def save_images():
    names=[]
    for f in request.files.getlist('images'):
        if not f.filename: continue
        ext=Path(secure_filename(f.filename)).suffix.lower()
        if ext not in ('.jpg','.jpeg','.png','.webp','.gif'): raise ValueError('Недопустимый формат изображения')
        # Check actual magic bytes rather than trusting extensions.
        head=f.stream.read(16)
        f.stream.seek(0)
        valid=(head.startswith(b'\xff\xd8\xff') and ext in ('.jpg','.jpeg')) or (head.startswith(b'\x89PNG\r\n\x1a\n') and ext=='.png') or (head.startswith((b'GIF87a',b'GIF89a')) and ext=='.gif') or (head[:4]==b'RIFF' and head[8:12]==b'WEBP' and ext=='.webp')
        if not valid: raise ValueError('Файл не является изображением выбранного формата')
        name=uuid.uuid4().hex+ext
        f.save(UPLOAD/name)
        names.append(name)
    return names

@app.get('/')
def home(): return jsonify(status='ok',service='Непростопост API v1')

@app.post('/api/auth/max')
def auth_max():
    user=verify_max((request.get_json(silent=True) or {}).get('init_data'))
    if not user: return jsonify(message='Не удалось подтвердить запуск через MAX. Откройте Mini App из бота.'),401
    uid=str(user['id']); now=utcnow(); trial=iso(now+timedelta(days=3))
    with db() as c:
        c.execute('INSERT OR IGNORE INTO app_users(id,first_name,created_at,trial_end) VALUES(?,?,?,?)',(uid,user.get('first_name',''),iso(now),trial))
        token=uuid.uuid4().hex+uuid.uuid4().hex
        c.execute('INSERT INTO app_sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),uid,iso(now+timedelta(hours=12))))
        c.commit()
    return jsonify(token=token,user={'id':uid,'first_name':user.get('first_name','')})

@app.get('/api/me')
@require_auth
def me():
    with db() as c: u=c.execute('SELECT * FROM app_users WHERE id=?',(g.user_id,)).fetchone()
    return jsonify(user={'id':g.user_id,'first_name':u['first_name']},trial_end=u['trial_end'],subscription='trial' if parse_time(u['trial_end'])>utcnow() else 'expired')

@app.get('/api/get_chats')
@require_auth
def chats():
    with db() as c: rows=c.execute('SELECT chat_id,title FROM app_groups WHERE owner_id=? ORDER BY title',(g.user_id,)).fetchall()
    return jsonify(chats=[{'id':r['chat_id'],'title':r['title'],'members_count':None} for r in rows])

@app.post('/api/connect_chat')
@require_auth
def connect_chat():
    data=request.get_json(silent=True) or {}
    chat_id=str(data.get('chat_id','')).strip()
    if not re.fullmatch(r'-?\d+',chat_id): return jsonify(message='Введите числовой ID группы'),400
    result=get_admin_group(chat_id,g.user_id)
    if result is None: return jsonify(message='MAX не подтвердил права. Проверьте, что бот администратор группы.'),502
    if not result: return jsonify(message='Ваш аккаунт MAX не является администратором этой группы.'),403
    try:
        r=max_api('GET',f'/chats/{chat_id}')
        title=r.json().get('title',f'Группа {chat_id}') if r.ok else f'Группа {chat_id}'
    except (requests.RequestException,ValueError): title=f'Группа {chat_id}'
    with db() as c:
        c.execute('INSERT OR REPLACE INTO app_groups(owner_id,chat_id,title) VALUES(?,?,?)',(g.user_id,chat_id,title))
        c.commit()
    return jsonify(status='ok',title=title)

@app.get('/api/get_catalog')
@require_auth
def catalog():
    with db() as c: rows=c.execute('SELECT * FROM app_catalog WHERE owner_id=? ORDER BY created_at DESC',(g.user_id,)).fetchall()
    items=[]
    for r in rows:
        urls=[request.host_url.rstrip('/')+f'/api/images/{r["id"]}/{n}' for n in image_names(r['images'])]
        items.append(dict(id=r['id'],title=r['title'],description=r['description'],price=r['price'],tag=r['tag'],category=r['category'],address=r['address'],image_urls=urls,image_url=urls[0] if urls else None))
    return jsonify(catalog=items)

@app.post('/api/add_catalog')
@require_auth
def add_catalog():
    title=request.form.get('title','').strip()
    if not title: return jsonify(message='Введите название'),400
    try: names=save_images()
    except ValueError as e: return jsonify(message=str(e)),400
    item_id=uuid.uuid4().hex
    with db() as c:
        c.execute('INSERT INTO app_catalog VALUES(?,?,?,?,?,?,?,?,?,?)',(item_id,g.user_id,title,request.form.get('description',''),request.form.get('price',''),request.form.get('tag',''),request.form.get('address',''),request.form.get('category',request.form.get('tag','Новостройка')),json.dumps(names),iso(utcnow())))
        c.commit()
    return jsonify(status='ok',id=item_id),201

@app.get('/api/images/<item_id>/<filename>')
@require_auth
def image_file(item_id,filename):
    with db() as c: row=c.execute('SELECT images FROM app_catalog WHERE id=? AND owner_id=?',(item_id,g.user_id)).fetchone()
    if not row or filename not in image_names(row['images']): return jsonify(message='Не найдено'),404
    return send_from_directory(UPLOAD,filename)

@app.get('/api/get_clients')
@require_auth
def clients():
    with db() as c: rows=c.execute('SELECT * FROM app_clients WHERE owner_id=? ORDER BY schedule_time',(g.user_id,)).fetchall()
    return jsonify(clients=[dict(id=r['id'],name=r['name'],phone=r['phone'],text=r['text'],type=r['type'],schedule_time=r['schedule_time'],status=r['status']) for r in rows])

@app.post('/api/add_client')
@require_auth
def add_client():
    name=request.form.get('name','').strip()
    date=request.form.get('date','').strip(); tm=request.form.get('time','').strip()
    if not name or not date or not tm: return jsonify(message='Укажите имя, дату и время'),400
    try: when=parse_time(request.form.get('run_at',''))
    except ValueError: return jsonify(message='Ошибка даты или часового пояса'),400
    if when<=utcnow(): return jsonify(message='Дата должна быть в будущем'),400
    cid=uuid.uuid4().hex
    with db() as c:
        c.execute('INSERT INTO app_clients VALUES(?,?,?,?,?,?,?,?)',(cid,g.user_id,name,request.form.get('phone',''),request.form.get('text',''),request.form.get('type','Звонок'),iso(when),'Запланировано'))
        c.commit()
    return jsonify(status='ok',id=cid),201

@app.get('/api/get_jobs')
@require_auth
def jobs():
    with db() as c:
        rows=c.execute('SELECT id,text,run_at,status FROM app_jobs WHERE owner_id=? ORDER BY run_at',(g.user_id,)).fetchall()
        people=c.execute('SELECT id,name,text,schedule_time,status FROM app_clients WHERE owner_id=? ORDER BY schedule_time',(g.user_id,)).fetchall()
    tasks=[dict(id=r['id'],type='group',schedule=r['run_at'],text=r['text'],status=r['status']) for r in rows]
    tasks += [dict(id=r['id'],type='client',schedule=r['schedule_time'],text=f"{r['name']}: {r['text']}",status=r['status']) for r in people]
    return jsonify(jobs=sorted(tasks,key=lambda x:x['schedule']))

@app.post('/api/submit_ad')
@require_auth
def submit_ad():
    chat_id=request.form.get('chat_id','').strip(); text=request.form.get('text','').strip()
    if not chat_id or not text: return jsonify(message='Выберите группу и напишите текст'),400
    if not owned_group(chat_id): return jsonify(message='Группа не подключена к вашему аккаунту'),403
    try: when=parse_time(request.form.get('run_at',''))
    except ValueError: return jsonify(message='Укажите корректные дату, время и часовой пояс'),400
    if when<=utcnow(): return jsonify(message='Дата должна быть в будущем'),400
    if get_admin_group(chat_id,g.user_id) is not True: return jsonify(message='Не удалось подтвердить права администратора'),403
    try: names=save_images()
    except ValueError as e: return jsonify(message=str(e)),400
    jid=uuid.uuid4().hex
    with db() as c:
        c.execute('INSERT INTO app_jobs VALUES(?,?,?,?,?,?,?,?)',(jid,g.user_id,chat_id,text,iso(when),0,'scheduled',json.dumps(names)))
        c.commit()
    # Sending disabled until a single dedicated worker with retry/locking is configured.
    return jsonify(status='ok',id=jid,message='Черновик расписания сохранён. Автоматическая отправка пока отключена.'),201

@app.get('/api/get_stats')
@require_auth
def stats():
    with db() as c:
        groups=c.execute('SELECT COUNT(*) FROM app_groups WHERE owner_id=?',(g.user_id,)).fetchone()[0]
        people=c.execute('SELECT COUNT(*) FROM app_clients WHERE owner_id=?',(g.user_id,)).fetchone()[0]
    return jsonify(groups=groups,clients=people,subscribers=None)

@app.delete('/api/delete_job/<job_id>/<job_type>')
@require_auth
def delete_job(job_id,job_type):
    table={'group':'app_jobs','client':'app_clients'}.get(job_type)
    if not table: return jsonify(message='Неизвестный тип'),400
    with db() as c:
        c.execute(f'DELETE FROM {table} WHERE id=? AND owner_id=?',(job_id,g.user_id))
        c.commit()
    return jsonify(status='ok')

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','3000')))
