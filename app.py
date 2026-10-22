import os, json, uuid, hmac, hashlib, sqlite3, logging, re, shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl
import requests
from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

TOKEN=os.getenv('MAX_BOT_TOKEN','').strip()
BASE=os.getenv('MAX_API_BASE_URL','https://platform-api2.max.ru').strip().rstrip('/')
if BASE not in ('https://platform-api2.max.ru','https://platform-api.max.ru'):
    raise RuntimeError('MAX_API_BASE_URL должен быть официальным HTTPS-адресом MAX API')
DATA=Path(os.getenv('DATA_DIR',str(Path(__file__).resolve().parent / '.data')))
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
CORS(app,resources={r'/api/*':{'origins':[origin.strip().rstrip('/') for origin in os.getenv('FRONTEND_ORIGIN','*').split(',') if origin.strip()],'allow_headers':['Authorization','Content-Type']}})

def db():
    c=sqlite3.connect(str(DB),timeout=30)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=30000')
    return c

def init_db():
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('CREATE TABLE IF NOT EXISTS app_users (id TEXT PRIMARY KEY, first_name TEXT, created_at TEXT NOT NULL, trial_end TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS app_sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS app_catalog (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT, price TEXT, tag TEXT, address TEXT, category TEXT, images TEXT, created_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS app_clients (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL, phone TEXT, text TEXT, type TEXT, schedule_time TEXT NOT NULL, status TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS app_groups (owner_id TEXT NOT NULL, chat_id TEXT NOT NULL, title TEXT NOT NULL, PRIMARY KEY(owner_id,chat_id))')
        c.execute('CREATE TABLE IF NOT EXISTS app_jobs (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, chat_id TEXT NOT NULL, text TEXT NOT NULL, run_at TEXT NOT NULL, repeat_daily INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT "scheduled", images TEXT NOT NULL DEFAULT "[]")')
        c.execute('CREATE INDEX IF NOT EXISTS idx_app_jobs_due ON app_jobs(status,run_at)')
        c.execute('CREATE TABLE IF NOT EXISTS app_known_chats (chat_id TEXT PRIMARY KEY, title TEXT, active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS app_bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        user_columns={r['name'] for r in c.execute('PRAGMA table_info(app_users)')}
        if 'paid_until' not in user_columns: c.execute('ALTER TABLE app_users ADD COLUMN paid_until TEXT')
        if 'paid_is_test' not in user_columns: c.execute('ALTER TABLE app_users ADD COLUMN paid_is_test INTEGER NOT NULL DEFAULT 0')
        c.execute('CREATE TABLE IF NOT EXISTS app_broadcast_usage (job_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, quota_day TEXT NOT NULL)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_broadcast_usage_day ON app_broadcast_usage(owner_id,quota_day)')
        client_columns={r['name'] for r in c.execute('PRAGMA table_info(app_clients)')}
        for name,declaration in [('prepared_message',"TEXT NOT NULL DEFAULT ''"),('reminder_at','TEXT'),('reminder_status',"TEXT NOT NULL DEFAULT 'disabled'"),('reminder_error','TEXT'),('reminder_attempts','INTEGER NOT NULL DEFAULT 0')]:
            if name not in client_columns: c.execute(f'ALTER TABLE app_clients ADD COLUMN {name} {declaration}')
        c.execute("INSERT OR IGNORE INTO app_broadcast_usage SELECT id,owner_id,date(run_at,'+3 hours') FROM app_jobs WHERE status IN ('scheduled','processing','sent','uncertain')")
        columns={r['name'] for r in c.execute('PRAGMA table_info(app_jobs)')}
        for name, declaration in [('attempts','INTEGER NOT NULL DEFAULT 0'), ('last_error','TEXT'), ('message_id','TEXT')]:
            if name not in columns: c.execute(f'ALTER TABLE app_jobs ADD COLUMN {name} {declaration}')
        c.commit()
init_db()

def utcnow(): return datetime.now(timezone.utc)
def iso(d): return d.astimezone(timezone.utc).isoformat()
def parse_time(s):
    if not isinstance(s,str): raise ValueError('Некорректная дата')
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('Не указан часовой пояс')
    return d.astimezone(timezone.utc)

def has_pro(connection, user_id):
    row=connection.execute('SELECT paid_until,paid_is_test FROM app_users WHERE id=?',(user_id,)).fetchone()
    return bool(row and row['paid_is_test']==int(os.getenv('YOOKASSA_TEST_MODE')=='1') and row['paid_until'] and parse_time(row['paid_until'])>utcnow())

def pro_required(message):
    return jsonify(code='subscription_required',message=message+' Подписка PRO — 499 ₽.'),402

def quota_day(when):
    return when.astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()

def reserve_broadcast(connection, user_id, job_id, when, chat_id):
    if not has_pro(connection,user_id):
        first=connection.execute('SELECT chat_id FROM app_groups WHERE owner_id=? ORDER BY rowid LIMIT 1',(user_id,)).fetchone()
        if not first or first['chat_id'] != str(chat_id): return 'Бесплатно доступна одна группа для рассылки.'
        used=connection.execute('SELECT COUNT(*) FROM app_broadcast_usage WHERE owner_id=? AND quota_day=? AND job_id!=?',(user_id,quota_day(when),job_id)).fetchone()[0]
        if used>=1: return 'Бесплатно доступно одно сообщение в день (по московскому времени).'
    connection.execute('INSERT OR REPLACE INTO app_broadcast_usage VALUES(?,?,?)',(job_id,user_id,quota_day(when)))
    return None

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
    return requests.request(method,BASE+path,headers={'Authorization':TOKEN},timeout=kw.pop('timeout',25),**kw)

def get_admin_group(chat_id,user_id,timeout=25):
    if not re.fullmatch(r'-?\d+',str(chat_id)): return None
    try:
        response=max_api('GET',f'/chats/{chat_id}/members/admins',timeout=timeout)
        response.raise_for_status()
        data=response.json()
        for admin in data.get('members',[]):
            u=admin.get('user') if isinstance(admin.get('user'),dict) else admin
            if str(u.get('user_id',u.get('id','')))==str(user_id):
                return True
        return False
    except (requests.RequestException,ValueError,TypeError,KeyError,RuntimeError):
        LOG.warning('Не удалось проверить права администратора MAX')
        return None

def owned_group(chat_id):
    with db() as c:
        return c.execute('SELECT 1 FROM app_groups WHERE owner_id=? AND chat_id=?',(g.user_id,str(chat_id))).fetchone() is not None

def image_names(value):
    try: names=json.loads(value or '[]')
    except (ValueError,TypeError): return []
    return [n for n in names if isinstance(n,str) and re.fullmatch(r'[a-f0-9]{32}\.(jpg|jpeg|png|webp|gif)',n)]

def save_images():
    files=[f for f in request.files.getlist('images') if f.filename]
    if len(files)>10: raise ValueError('Не более 10 изображений')
    validated=[]
    # Validate every file before writing any; a bad later image must not leave orphans.
    for f in files:
        ext=Path(secure_filename(f.filename)).suffix.lower()
        if ext not in ('.jpg','.jpeg','.png','.webp','.gif'): raise ValueError('Недопустимый формат изображения')
        head=f.stream.read(16)
        f.stream.seek(0)
        valid=(head.startswith(b'\xff\xd8\xff') and ext in ('.jpg','.jpeg')) or (head.startswith(b'\x89PNG\r\n\x1a\n') and ext=='.png') or (head.startswith((b'GIF87a',b'GIF89a')) and ext=='.gif') or (head[:4]==b'RIFF' and head[8:12]==b'WEBP' and ext=='.webp')
        if not valid: raise ValueError('Файл не является изображением выбранного формата')
        validated.append((f,uuid.uuid4().hex+ext))
    names=[]
    try:
        for f,name in validated:
            names.append(name)
            f.save(UPLOAD/name)
    except OSError:
        for name in names: (UPLOAD/name).unlink(missing_ok=True)
        raise
    return names

@app.get('/')
def home(): return app.send_static_file('index.html')

@app.get('/api/health')
def health(): return jsonify(status='ok', service='Непростопост API v1', max_configured=bool(TOKEN))

@app.post('/api/auth/max')
def auth_max():
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict): return jsonify(message='Ожидается JSON-объект'),400
    user=verify_max(payload.get('init_data'))
    if not TOKEN: return jsonify(message='Сервер не настроен: требуется MAX_BOT_TOKEN'),503
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
    return jsonify(user={'id':g.user_id,'first_name':u['first_name']},subscription='pro' if has_pro(c,g.user_id) else 'free',paid_until=u['paid_until'])

@app.get('/api/get_chats')
@require_auth
def chats():
    with db() as c: rows=c.execute('SELECT chat_id,title FROM app_groups WHERE owner_id=? ORDER BY title',(g.user_id,)).fetchall()
    return jsonify(chats=[{'id':r['chat_id'],'title':r['title'],'members_count':None} for r in rows])

@app.get('/api/discover_chats')
@require_auth
def discover_chats():
    if not TOKEN: return jsonify(message='MAX_BOT_TOKEN не задан'),503
    marker=request.args.get('marker','0')
    if not re.fullmatch(r'\d{1,9}',marker): return jsonify(message='Некорректная страница списка'),400
    offset=int(marker)
    with db() as c:
        rows=c.execute("""SELECT chat_id,MAX(title) AS title FROM (
            SELECT chat_id,title FROM app_known_chats WHERE active=1
            UNION ALL SELECT chat_id,title FROM app_groups WHERE owner_id=?
            ) GROUP BY chat_id ORDER BY chat_id LIMIT 6 OFFSET ?""",(g.user_id,offset)).fetchall()
        poll_error=c.execute("SELECT value FROM app_bot_state WHERE key='updates_error'").fetchone()
        checked=c.execute("SELECT value FROM app_bot_state WHERE key='updates_checked_at'").fetchone()
    candidates=[dict(row) for row in rows[:5]]
    next_marker=offset+5 if len(rows)>5 else None
    user_id=g.user_id
    # Small pages and bounded parallel requests fit the web request timeout.
    def check_candidate(chat):
        allowed=get_admin_group(chat['chat_id'],user_id,timeout=8)
        if allowed is True and not chat.get('title'):
            try:
                detail=max_api('GET',f"/chats/{chat['chat_id']}",timeout=5)
                detail.raise_for_status()
                title=detail.json().get('title')
                if isinstance(title,str): chat['title']=title
            except (requests.RequestException,ValueError,TypeError,AttributeError,RuntimeError):
                pass
        return allowed
    with ThreadPoolExecutor(max_workers=5) as pool:
        permissions=list(pool.map(check_candidate,candidates))
    with db() as c:
        connected={r['chat_id'] for r in c.execute('SELECT chat_id FROM app_groups WHERE owner_id=?',(user_id,))}
    chats=[{'id':str(chat['chat_id']),'title':chat.get('title') or 'Группа '+str(chat['chat_id']),
            'connected':str(chat['chat_id']) in connected}
           for chat,allowed in zip(candidates,permissions) if allowed is True]
    hint='Если группы нет, отправьте в нужную группу MAX команду /connect, подождите 10 секунд и повторите поиск.'
    if poll_error and poll_error['value']: hint=poll_error['value']+' Проверьте журнал запуска worker на Ботхосте.'
    elif not checked: hint='Ожидается получение событий. Отправьте /connect в группу и повторите поиск через 10 секунд. Если группа не появится, проверьте запуск python run.py на Ботхосте.'
    return jsonify(chats=chats,marker=next_marker,unavailable_count=permissions.count(None),hint=hint)

@app.post('/api/connect_chat')
@require_auth
def connect_chat():
    data=request.get_json(silent=True)
    if not isinstance(data,dict): return jsonify(message='Ожидается JSON-объект'),400
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
        c.execute('BEGIN IMMEDIATE')
        existing=c.execute('SELECT 1 FROM app_groups WHERE owner_id=? AND chat_id=?',(g.user_id,chat_id)).fetchone()
        count=c.execute('SELECT COUNT(*) FROM app_groups WHERE owner_id=?',(g.user_id,)).fetchone()[0]
        if not existing and count>=1 and not has_pro(c,g.user_id): return pro_required('Бесплатно можно подключить одну группу.')
        c.execute('INSERT INTO app_groups(owner_id,chat_id,title) VALUES(?,?,?) ON CONFLICT(owner_id,chat_id) DO UPDATE SET title=excluded.title',(g.user_id,chat_id,title))
        c.commit()
    return jsonify(status='ok',title=title)

@app.get('/api/get_catalog')
@require_auth
def catalog():
    with db() as c: rows=c.execute('SELECT * FROM app_catalog WHERE owner_id=? ORDER BY created_at DESC',(g.user_id,)).fetchall()
    items=[]
    for r in rows:
        urls=[f'/api/images/{r["id"]}/{n}' for n in image_names(r['images'])]
        items.append(dict(id=r['id'],title=r['title'],description=r['description'],price=r['price'],tag=r['tag'],category=r['category'],address=r['address'],image_urls=urls,image_url=urls[0] if urls else None))
    return jsonify(catalog=items)

@app.post('/api/add_catalog')
@require_auth
def add_catalog():
    title=request.form.get('title','').strip()
    if not title: return jsonify(message='Введите название'),400
    item_id=uuid.uuid4().hex
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        count=c.execute('SELECT COUNT(*) FROM app_catalog WHERE owner_id=?',(g.user_id,)).fetchone()[0]
        if count>=1 and not has_pro(c,g.user_id): return pro_required('Бесплатно можно сохранить один объект.')
        try: names=save_images()
        except ValueError as e: return jsonify(message=str(e)),400
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
    with db() as c:
        if not has_pro(c,g.user_id): return pro_required('Планер доступен по подписке PRO.')
    with db() as c: rows=c.execute('SELECT * FROM app_clients WHERE owner_id=? ORDER BY schedule_time',(g.user_id,)).fetchall()
    return jsonify(clients=[dict(id=r['id'],name=r['name'],phone=r['phone'],text=r['text'],type=r['type'],schedule_time=r['schedule_time'],status=r['status'],prepared_message=r['prepared_message'],reminder_status=r['reminder_status'],reminder_error=r['reminder_error']) for r in rows])

@app.post('/api/add_client')
@require_auth
def add_client():
    with db() as c:
        if not has_pro(c,g.user_id): return pro_required('Планер доступен по подписке PRO.')
    name=request.form.get('name','').strip()
    date=request.form.get('date','').strip(); tm=request.form.get('time','').strip()
    if not name or not date or not tm: return jsonify(message='Укажите имя, дату и время'),400
    try: when=parse_time(request.form.get('run_at',''))
    except ValueError: return jsonify(message='Ошибка даты или часового пояса'),400
    if when<=utcnow(): return jsonify(message='Дата должна быть в будущем'),400
    try: remind=parse_time(request.form.get('reminder_at') or iso(when))
    except ValueError: return jsonify(message='Некорректное время напоминания'),400
    if remind<=utcnow() or remind>when: return jsonify(message='Напоминание должно быть в будущем и не позже контакта'),400
    prepared=request.form.get('prepared_message','').strip()
    if len(prepared)>2000 or len(request.form.get('text',''))>1000 or len(name)>200: return jsonify(message='Слишком длинное имя, заметка или сообщение'),400
    cid=uuid.uuid4().hex
    with db() as c:
        c.execute('INSERT INTO app_clients(id,owner_id,name,phone,text,type,schedule_time,status,prepared_message,reminder_at,reminder_status) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(cid,g.user_id,name,request.form.get('phone',''),request.form.get('text',''),request.form.get('type','Звонок'),iso(when),'Запланировано',prepared,iso(remind),'scheduled'))
        c.commit()
    return jsonify(status='ok',id=cid),201

@app.get('/api/get_jobs')
@require_auth
def jobs():
    with db() as c:
        rows=c.execute('SELECT id,text,run_at,status,last_error FROM app_jobs WHERE owner_id=? ORDER BY run_at',(g.user_id,)).fetchall()
        people=c.execute('SELECT id,name,text,schedule_time,status FROM app_clients WHERE owner_id=? ORDER BY schedule_time',(g.user_id,)).fetchall() if has_pro(c,g.user_id) else []
    tasks=[dict(id=r['id'],type='group',schedule=r['run_at'],text=r['text'],status=r['status'],error=r['last_error']) for r in rows]
    tasks += [dict(id=r['id'],type='client',schedule=r['schedule_time'],text=f"{r['name']}: {r['text']}",status=r['status']) for r in people]
    return jsonify(jobs=sorted(tasks,key=lambda x:x['schedule']))

@app.post('/api/submit_ad')
@require_auth
def submit_ad():
    chat_id=request.form.get('chat_id','').strip(); text=request.form.get('text','').strip()
    catalog_id=request.form.get('catalog_id','').strip()
    catalog=None
    if catalog_id:
        with db() as c: catalog=c.execute('SELECT * FROM app_catalog WHERE id=? AND owner_id=?',(catalog_id,g.user_id)).fetchone()
        if not catalog: return jsonify(message='Объект каталога не найден'),404
        if not text: text='\n'.join(str(catalog[k]) for k in ('title','address','price','description') if catalog[k])
    if len(text)>4000: return jsonify(message='Текст не должен превышать 4000 символов'),400
    if not TOKEN: return jsonify(message='MAX_BOT_TOKEN не задан'),503
    if not chat_id or not text: return jsonify(message='Выберите группу и напишите текст или выберите объект'),400
    if not owned_group(chat_id): return jsonify(message='Группа не подключена к вашему аккаунту'),403
    try: when=parse_time(request.form.get('run_at',''))
    except ValueError: return jsonify(message='Укажите корректные дату, время и часовой пояс'),400
    if when<=utcnow(): return jsonify(message='Дата должна быть в будущем'),400
    if get_admin_group(chat_id,g.user_id) is not True: return jsonify(message='Не удалось подтвердить права администратора'),403
    jid=uuid.uuid4().hex
    names=[]
    try:
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            limit=reserve_broadcast(c,g.user_id,jid,when,chat_id)
            if limit: return pro_required(limit)
            if catalog:
                # Snapshot photos so deleting the catalogue item cannot break a pending ad.
                for source in image_names(catalog['images']):
                    name=uuid.uuid4().hex+Path(source).suffix
                    names.append(name); shutil.copyfile(UPLOAD/source,UPLOAD/name)
            names.extend(save_images())
            if len(names)>10: raise ValueError('В рассылке допускается не более 10 изображений')
            c.execute('INSERT INTO app_jobs(id,owner_id,chat_id,text,run_at,repeat_daily,status,images) VALUES(?,?,?,?,?,?,?,?)',(jid,g.user_id,chat_id,text,iso(when),0,'scheduled',json.dumps(names)))
            c.commit()
    except (ValueError,OSError) as error:
        for name in names: (UPLOAD/name).unlink(missing_ok=True)
        return jsonify(message=str(error) if isinstance(error,ValueError) else 'Не удалось подготовить фото объекта'),400
    return jsonify(status='ok',id=jid,message='Рассылка запланирована.'),201

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
        c.execute('BEGIN IMMEDIATE')
        if table == 'app_jobs':
            row=c.execute('SELECT status,images FROM app_jobs WHERE id=? AND owner_id=?',(job_id,g.user_id)).fetchone()
            if row and row['status'] == 'processing': return jsonify(message='Рассылка уже отправляется'),409
        else:
            client=c.execute('SELECT reminder_status FROM app_clients WHERE id=? AND owner_id=?',(job_id,g.user_id)).fetchone()
            if client and client['reminder_status']=='processing': return jsonify(message='Напоминание уже отправляется'),409
        if table == 'app_jobs' and row and row['status'] in ('scheduled','failed','cancelled'):
            c.execute('DELETE FROM app_broadcast_usage WHERE job_id=?',(job_id,))
        c.execute(f'DELETE FROM {table} WHERE id=? AND owner_id=?',(job_id,g.user_id))
        c.commit()
    if table == 'app_jobs' and row:
        for name in image_names(row['images']):
            try: (UPLOAD/name).unlink(missing_ok=True)
            except OSError: LOG.warning('Не удалось удалить фото удалённой рассылки')
    return jsonify(status='ok')



@app.post('/api/auth/logout')
@require_auth
def logout():
    digest=hashlib.sha256(request.headers['Authorization'][7:].encode()).hexdigest()
    with db() as c:
        c.execute('DELETE FROM app_sessions WHERE token_hash=?',(digest,))
        c.commit()
    return jsonify(status='ok')

@app.errorhandler(413)
def too_large(error):
    return jsonify(message='Размер запроса превышает 20 МБ'),413

@app.delete('/api/catalog/<item_id>')
@require_auth
def delete_catalog(item_id):
    with db() as c:
        row=c.execute('SELECT images FROM app_catalog WHERE id=? AND owner_id=?',(item_id,g.user_id)).fetchone()
        if not row: return jsonify(message='Объект не найден'),404
        c.execute('DELETE FROM app_catalog WHERE id=? AND owner_id=?',(item_id,g.user_id))
        c.commit()
    for name in image_names(row['images']): (UPLOAD/name).unlink(missing_ok=True)
    return jsonify(status='ok')

@app.delete('/api/groups/<chat_id>')
@require_auth
def disconnect_chat(chat_id):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM app_jobs WHERE owner_id=? AND chat_id=? AND status='processing'",(g.user_id,chat_id)).fetchone():
            return jsonify(message='Дождитесь окончания отправки'),409
        c.execute('DELETE FROM app_groups WHERE owner_id=? AND chat_id=?',(g.user_id,chat_id))
        c.execute("DELETE FROM app_broadcast_usage WHERE job_id IN (SELECT id FROM app_jobs WHERE owner_id=? AND chat_id=? AND status='scheduled')",(g.user_id,chat_id))
        c.execute("UPDATE app_jobs SET status='cancelled' WHERE owner_id=? AND chat_id=? AND status='scheduled'",(g.user_id,chat_id))
        c.commit()
    return jsonify(status='ok')

import sys
import billing
billing.init(sys.modules[__name__])

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','3000')))
