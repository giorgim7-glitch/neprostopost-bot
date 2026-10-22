"""YooKassa payments with verified settlement, durable idempotency and explicit renewal consent."""
import calendar
import json
import os
import re
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
import requests
from flask import request, jsonify, g

b = None
PRICE = '499.00'
CONSENT_VERSION = 'monthly-499-v1'
API = 'https://api.yookassa.ru/v3'


class BillingError(Exception):
    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def configuration_error():
    if not os.getenv('YOOKASSA_SHOP_ID') or not os.getenv('YOOKASSA_SECRET_KEY'):
        return 'Оплата ещё не настроена: нужны реквизиты API ЮKassa на сервере.'
    url = os.getenv('YOOKASSA_RETURN_URL', '')
    if urlsplit(url).scheme != 'https' or not urlsplit(url).hostname:
        return 'Нужно настроить HTTPS-адрес возврата YOOKASSA_RETURN_URL.'
    mode = os.getenv('YOOKASSA_RECEIPT_MODE', '')
    if mode not in ('yookassa', 'external') and not (mode=='test' and os.getenv('YOOKASSA_TEST_MODE')=='1'):
        return 'Владелец приложения должен настроить формирование чеков в ЮKassa.'
    if mode == 'yookassa' and not re.fullmatch(r'[1-9][0-9]?', os.getenv('YOOKASSA_VAT_CODE', '')):
        return 'Нужно указать код НДС YOOKASSA_VAT_CODE из настроек вашего магазина.'
    return None


def provider(method, path, payload=None, key=None):
    headers = {'Idempotence-Key': key} if key else {}
    try:
        response = requests.request(method, API + path, auth=(os.getenv('YOOKASSA_SHOP_ID',''), os.getenv('YOOKASSA_SECRET_KEY','')),
                                    headers=headers, json=payload, timeout=20)
        if not response.ok:
            raise BillingError(f'ЮKassa вернула HTTP {response.status_code}. Проверьте настройки магазина и способ оплаты.', response.status_code in (400,401,403,404))
        data = response.json()
        if not isinstance(data, dict): raise ValueError('Invalid payment')
        return data
    except (requests.RequestException, ValueError):
        raise BillingError('Ответ ЮKassa не получен. Статус будет проверен повторно; не оплачивайте второй раз.') from None


def next_month(date):
    year, month = (date.year+1,1) if date.month == 12 else (date.year,date.month+1)
    return date.replace(year=year, month=month, day=min(date.day,calendar.monthrange(year,month)[1]))


def subscription(db, owner):
    db.execute('INSERT OR IGNORE INTO app_subscriptions(owner_id) VALUES(?)', (owner,))
    return db.execute('SELECT * FROM app_subscriptions WHERE owner_id=?',(owner,)).fetchone()


def public_state(owner):
    with b.db() as db:
        sub = subscription(db,owner)
        user = db.execute('SELECT paid_until,paid_is_test FROM app_users WHERE id=?',(owner,)).fetchone()
        pending = db.execute("SELECT id,confirmation_url,status FROM app_payments WHERE owner_id=? AND status IN ('creating','pending','waiting_for_capture','unknown') ORDER BY created_at DESC LIMIT 1",(owner,)).fetchone()
        return dict(price=499, old_price=999, period='month', subscription='pro' if b.has_pro(db,owner) else 'free',
                    paid_until=user['paid_until'] if user else None, auto_renew=bool(sub['auto_renew'] and sub['payment_method_test']==int(os.getenv('YOOKASSA_TEST_MODE')=='1')),
                    saved_method=bool(sub['payment_method_id'] and sub['payment_method_test']==int(os.getenv('YOOKASSA_TEST_MODE')=='1')), next_charge_at=sub['next_charge_at'],
                    error=sub['last_error'], configured=not configuration_error(), configuration_error=configuration_error(),
                    receipt_email_required=os.getenv('YOOKASSA_RECEIPT_MODE')=='yookassa',
                    test_mode=os.getenv('YOOKASSA_TEST_MODE')=='1', pending_payment=dict(pending) if pending else None,
                    limits={'groups':None if b.has_pro(db,owner) else 1,'messages_per_day':None if b.has_pro(db,owner) else 1,
                            'catalog':None if b.has_pro(db,owner) else 1,'planner':b.has_pro(db,owner)})


def receipt(email):
    if os.getenv('YOOKASSA_RECEIPT_MODE') != 'yookassa': return None
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(email)>254:
        raise BillingError('Укажите email для кассового чека.',True)
    result={'customer':{'email':email},'items':[{'description':'Непростопост PRO — 1 месяц','quantity':'1.00',
            'amount':{'value':PRICE,'currency':'RUB'},'vat_code':int(os.environ['YOOKASSA_VAT_CODE']),
            'payment_mode':'full_payment','payment_subject':'service'}]}
    tax=os.getenv('YOOKASSA_TAX_SYSTEM_CODE','')
    if tax:
        if tax not in ('1','2','3','4','5','6'): raise BillingError('Некорректная настройка системы налогообложения.',True)
        result['tax_system_code']=int(tax)
    return result


def consent_event(db,owner,event):
    db.execute('INSERT INTO app_consent_events VALUES(?,?,?,?,?)',(str(uuid.uuid4()),owner,event,b.iso(b.utcnow()),CONSENT_VERSION))


def create_order(db,owner,method,consent,email,renewal=False):
    sub=subscription(db,owner)
    order_id=str(uuid.uuid4())
    body={'amount':{'value':PRICE,'currency':'RUB'},'capture':True,'description':'Непростопост PRO — 1 месяц',
          'metadata':{'order_id':order_id,'owner_id':owner,'product':'pro-month-v1'}}
    if renewal:
        body['payment_method_id']=sub['payment_method_id']
    else:
        body['payment_method_data']={'type':method}
        body['confirmation']={'type':'redirect','return_url':os.environ['YOOKASSA_RETURN_URL']}
        if consent: body['save_payment_method']=True
        db.execute('UPDATE app_subscriptions SET revision=revision+1 WHERE owner_id=?',(owner,))
        sub=subscription(db,owner)
    rec=receipt(email)
    if rec: body['receipt']=rec
    db.execute('''INSERT INTO app_payments(id,owner_id,status,request_body,consent,revision,kind,email,created_at)
                  VALUES(?,?,'creating',?,?,?,?,?,?)''',
               (order_id,owner,json.dumps(body),int(consent),sub['revision'],'renewal' if renewal else 'initial',email,b.iso(b.utcnow())))
    if consent and not renewal:
        consent_event(db,owner,'checkout_consent')
        db.execute('UPDATE app_subscriptions SET consent_at=?,consent_version=? WHERE owner_id=?',
                   (b.iso(b.utcnow()),CONSENT_VERSION,owner))
    return order_id


def renewal_failure(db,order,message):
    if order['kind']!='renewal': return
    sub=subscription(db,order['owner_id'])
    if sub['revision']!=order['revision']: return
    failures=sub['failures']+1
    db.execute('''UPDATE app_subscriptions SET failures=?,auto_renew=CASE WHEN ?>=3 THEN 0 ELSE auto_renew END,
                  next_charge_at=?,last_error=? WHERE owner_id=?''',
               (failures,failures,b.iso(b.utcnow()+timedelta(days=1)),message,order['owner_id']))


def apply_payment(order_id,data):
    # Data must come from authenticated YooKassa API, never directly from a webhook body.
    with b.db() as db:
        db.execute('BEGIN IMMEDIATE')
        order=db.execute('SELECT * FROM app_payments WHERE id=?',(order_id,)).fetchone()
        if not order: raise BillingError('Платёж не найден.',True)
        meta=data.get('metadata') or {}
        if not isinstance(meta,dict) or not isinstance(data.get('amount'),dict): raise BillingError('Некорректный ответ ЮKassa.',True)
        try: valid_amount=Decimal(str(data.get('amount',{}).get('value')))==Decimal(PRICE)
        except (InvalidOperation,TypeError): valid_amount=False
        test=bool(data.get('test'))
        if (meta.get('order_id')!=order_id or str(meta.get('owner_id'))!=order['owner_id'] or meta.get('product')!='pro-month-v1'
            or not valid_amount or data.get('amount',{}).get('currency')!='RUB'
            or test!=(os.getenv('YOOKASSA_TEST_MODE')=='1')):
            raise BillingError('Реквизиты платежа не совпадают с заказом. Подписка не изменена.',True)
        provider_id=data.get('id')
        if not isinstance(provider_id,str) or not re.fullmatch(r'[0-9a-f-]{36}',provider_id): raise BillingError('Некорректный ID платежа.',True)
        if order['provider_id'] and order['provider_id']!=provider_id: raise BillingError('ID платежа не совпадает.',True)
        status=data.get('status')
        if status not in ('pending','waiting_for_capture','succeeded','canceled'): raise BillingError('Неизвестный статус платежа.')
        url=(data.get('confirmation') or {}).get('confirmation_url')
        if url and (urlsplit(url).scheme!='https' or not urlsplit(url).hostname): raise BillingError('Некорректная ссылка оплаты.',True)
        if order['applied']:
            # Duplicate/out-of-order webhook must not extend the subscription or downgrade settlement.
            return
        db.execute('UPDATE app_payments SET provider_id=?,status=?,confirmation_url=COALESCE(?,confirmation_url) WHERE id=?',
                   (provider_id,status,url,order_id))
        if status=='succeeded':
            if data.get('paid') is not True: raise BillingError('ЮKassa не подтвердила оплату.')
            user=db.execute('SELECT paid_until,paid_is_test FROM app_users WHERE id=?',(order['owner_id'],)).fetchone()
            if not user: raise BillingError('Пользователь платежа не найден.',True)
            current=b.parse_time(user['paid_until']) if user['paid_until'] and user['paid_is_test']==int(test) else b.utcnow()
            until=b.iso(next_month(max(current,b.utcnow())))
            db.execute('UPDATE app_users SET paid_until=?,paid_is_test=? WHERE id=?',(until,int(test),order['owner_id']))
            db.execute('UPDATE app_payments SET applied=1 WHERE id=?',(order_id,))
            sub=subscription(db,order['owner_id'])
            db.execute('UPDATE app_subscriptions SET next_charge_at=? WHERE owner_id=?',(until,order['owner_id']))
            payment_method=data.get('payment_method') or {}
            if sub['revision']==order['revision']:
                if order['kind']=='initial':
                    saved=payment_method.get('saved') is True and bool(payment_method.get('id'))
                    auto=bool(order['consent'] and saved)
                    db.execute('''UPDATE app_subscriptions SET payment_method_id=?,payment_method_test=?,auto_renew=?,next_charge_at=?,email=?,failures=0,last_error=? WHERE owner_id=?''',
                               (payment_method.get('id') if saved else None,int(test),int(auto),until,order['email'],
                                'ЮKassa не сохранила способ оплаты; следующее продление вручную.' if order['consent'] and not saved else None,order['owner_id']))
                else:
                    db.execute('UPDATE app_subscriptions SET next_charge_at=?,failures=0,last_error=NULL WHERE owner_id=?',(until,order['owner_id']))
            db.execute("UPDATE app_clients SET reminder_status='scheduled',reminder_error=NULL WHERE owner_id=? AND reminder_status='paused' AND reminder_at>?",(order['owner_id'],b.iso(b.utcnow())))
        elif status=='canceled' and order['status']!='canceled':
            renewal_failure(db,order,'ЮKassa отменила продление. Проверьте способ оплаты.')
        db.commit()


def process_order(order_id):
    with b.db() as db: order=db.execute('SELECT * FROM app_payments WHERE id=?',(order_id,)).fetchone()
    if not order: raise BillingError('Платёж не найден.',True)
    if order['applied'] or order['status'] in ('canceled','failed'): return
    with b.db() as db:
        db.execute('UPDATE app_payments SET checked_at=? WHERE id=?',(b.iso(b.utcnow()),order_id));db.commit()
    if order['kind']=='renewal' and not order['provider_id']:
        with b.db() as db:
            sub=subscription(db,order['owner_id'])
            if not sub['auto_renew'] or sub['revision']!=order['revision']:
                db.execute("UPDATE app_payments SET status='canceled' WHERE id=?",(order_id,));db.commit();return
    if order['status']=='unknown': raise BillingError('Статус платежа требует проверки владельцем магазина.',True)
    try:
        if order['provider_id']:
            data=provider('GET','/payments/'+order['provider_id'])
        else:
            if b.utcnow()-b.parse_time(order['created_at'])>timedelta(hours=23):
                with b.db() as db:
                    db.execute("UPDATE app_payments SET status='unknown' WHERE id=?",(order_id,));db.commit()
                raise BillingError('Истёк безопасный срок повтора платежа. Нужна проверка в ЮKassa.',True)
            data=provider('POST','/payments',json.loads(order['request_body']),order_id)
        apply_payment(order_id,data)
    except BillingError as error:
        if error.permanent and not order['provider_id'] and order['status']=='creating':
            with b.db() as db:
                fresh=db.execute('SELECT * FROM app_payments WHERE id=?',(order_id,)).fetchone()
                if fresh['status']=='creating':
                    db.execute("UPDATE app_payments SET status='failed' WHERE id=?",(order_id,))
                    renewal_failure(db,order,str(error));db.commit()
        raise


def tick():
    if configuration_error(): return
    with b.db() as db:
        pending=db.execute("SELECT id FROM app_payments WHERE status IN ('creating','pending','waiting_for_capture') ORDER BY COALESCE(checked_at,created_at) LIMIT 10").fetchall()
    for order in pending:
        try: process_order(order['id'])
        except BillingError: pass
    with b.db() as db:
        db.execute('BEGIN IMMEDIATE')
        sub=db.execute('''SELECT * FROM app_subscriptions s WHERE auto_renew=1 AND payment_method_id IS NOT NULL AND next_charge_at<=? AND payment_method_test=?
            AND NOT EXISTS(SELECT 1 FROM app_payments p WHERE p.owner_id=s.owner_id AND p.status IN ('creating','pending','waiting_for_capture','unknown'))
            AND EXISTS(SELECT 1 FROM app_users u WHERE u.id=s.owner_id AND u.paid_is_test=? AND u.paid_until<=?)
            ORDER BY next_charge_at LIMIT 1''',(b.iso(b.utcnow()),int(os.getenv('YOOKASSA_TEST_MODE')=='1'),int(os.getenv('YOOKASSA_TEST_MODE')=='1'),b.iso(b.utcnow()))).fetchone()
        if not sub: return
        order_id=create_order(db,sub['owner_id'],None,True,sub['email'] or '',renewal=True)
        db.commit()
    try: process_order(order_id)
    except BillingError: pass


def init(backend):
    global b
    b=backend
    with b.db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS app_consent_events(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,event TEXT NOT NULL,created_at TEXT NOT NULL,version TEXT NOT NULL)')
        db.execute('''CREATE TABLE IF NOT EXISTS app_subscriptions(owner_id TEXT PRIMARY KEY,auto_renew INTEGER NOT NULL DEFAULT 0,
            payment_method_id TEXT,next_charge_at TEXT,email TEXT,revision INTEGER NOT NULL DEFAULT 0,consent_at TEXT,
            consent_version TEXT,failures INTEGER NOT NULL DEFAULT 0,last_error TEXT,payment_method_test INTEGER NOT NULL DEFAULT 0)''')
        if 'payment_method_test' not in {row['name'] for row in db.execute('PRAGMA table_info(app_subscriptions)')}:
            db.execute('ALTER TABLE app_subscriptions ADD COLUMN payment_method_test INTEGER NOT NULL DEFAULT 0')
        db.execute('''CREATE TABLE IF NOT EXISTS app_payments(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,provider_id TEXT UNIQUE,
            status TEXT NOT NULL,request_body TEXT NOT NULL,confirmation_url TEXT,consent INTEGER NOT NULL,revision INTEGER NOT NULL,
            kind TEXT NOT NULL,email TEXT,created_at TEXT NOT NULL,applied INTEGER NOT NULL DEFAULT 0,checked_at TEXT)''')
        if 'checked_at' not in {row['name'] for row in db.execute('PRAGMA table_info(app_payments)')}:
            db.execute('ALTER TABLE app_payments ADD COLUMN checked_at TEXT')
        db.commit()

    @b.require_auth
    def info(): return jsonify(public_state(g.user_id))

    @b.require_auth
    def checkout():
        problem=configuration_error()
        if problem: return jsonify(message=problem),503
        data=request.get_json(silent=True)
        if not isinstance(data,dict) or data.get('method') not in ('bank_card','sbp') or type(data.get('auto_renew',False)) is not bool:
            return jsonify(message='Выберите карту или СБП и настройку автопродления.'),400
        email=str(data.get('email','')).strip()
        if email and (len(email)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email)):
            return jsonify(message='Укажите корректный email для чека.'),400
        try:
            with b.db() as db:
                db.execute('BEGIN IMMEDIATE')
                pending=db.execute("SELECT id FROM app_payments WHERE owner_id=? AND status IN ('creating','pending','waiting_for_capture','unknown') ORDER BY created_at LIMIT 1",(g.user_id,)).fetchone()
                order_id=pending['id'] if pending else create_order(db,g.user_id,data['method'],data.get('auto_renew',False),email)
                db.commit()
            process_order(order_id)
            return jsonify(public_state(g.user_id))
        except BillingError as error: return jsonify(message=str(error)),502 if not error.permanent else 400

    @b.require_auth
    def check_payment(order_id):
        with b.db() as db: row=db.execute('SELECT id FROM app_payments WHERE id=? AND owner_id=?',(order_id,g.user_id)).fetchone()
        if not row: return jsonify(message='Платёж не найден'),404
        try: process_order(order_id)
        except BillingError as error: return jsonify(message=str(error)),502
        return jsonify(public_state(g.user_id))

    @b.require_auth
    def auto_renew():
        data=request.get_json(silent=True)
        if not isinstance(data,dict) or type(data.get('enabled')) is not bool: return jsonify(message='Укажите enabled: true или false'),400
        with b.db() as db:
            db.execute('BEGIN IMMEDIATE');sub=subscription(db,g.user_id)
            if data['enabled'] and (not sub['payment_method_id'] or sub['payment_method_test']!=int(os.getenv('YOOKASSA_TEST_MODE')=='1') or not b.has_pro(db,g.user_id)):
                return jsonify(message='Для автопродления сначала оплатите подписку с сохранением способа оплаты.'),400
            if data['enabled']:
                until=db.execute('SELECT paid_until FROM app_users WHERE id=?',(g.user_id,)).fetchone()['paid_until']
                db.execute('UPDATE app_subscriptions SET next_charge_at=? WHERE owner_id=?',(until,g.user_id))
            consent_event(db,g.user_id,'auto_enabled' if data['enabled'] else 'auto_disabled')
            db.execute('''UPDATE app_subscriptions SET auto_renew=?,revision=revision+1,consent_at=?,consent_version=?,last_error=NULL WHERE owner_id=?''',
                       (int(data['enabled']),b.iso(b.utcnow()) if data['enabled'] else sub['consent_at'],CONSENT_VERSION,g.user_id));db.commit()
        return jsonify(public_state(g.user_id))

    def webhook():
        data=request.get_json(silent=True)
        if not isinstance(data,dict): return jsonify(message='Invalid notification'),400
        if data.get('event') not in ('payment.succeeded','payment.canceled'): return jsonify(status='ignored')
        obj=data.get('object')
        pid=obj.get('id') if isinstance(obj,dict) else None
        if not isinstance(pid,str) or not re.fullmatch(r'[0-9a-f-]{36}',pid): return jsonify(message='Invalid payment ID'),400
        metadata=obj.get('metadata') or {}
        oid_hint=metadata.get('order_id') if isinstance(metadata,dict) else None
        with b.db() as db:
            known=db.execute('SELECT id FROM app_payments WHERE provider_id=? OR (id=? AND provider_id IS NULL)',(pid,oid_hint)).fetchone()
        if not known: return jsonify(status='ignored')
        try:
            verified=provider('GET','/payments/'+pid)
            oid=(verified.get('metadata') or {}).get('order_id')
            with b.db() as db: exists=db.execute('SELECT 1 FROM app_payments WHERE id=?',(oid,)).fetchone()
            if not exists or oid != known['id']: return jsonify(message='Payment metadata mismatch'),400
            apply_payment(oid,verified)
        except BillingError: return jsonify(message='Payment verification failed'),503
        return jsonify(status='ok')

    b.app.add_url_rule('/api/billing','billing_info',info,methods=['GET'])
    b.app.add_url_rule('/api/billing/checkout','billing_checkout',checkout,methods=['POST'])
    b.app.add_url_rule('/api/billing/payments/<order_id>','billing_check',check_payment,methods=['POST'])
    b.app.add_url_rule('/api/billing/auto-renew','billing_renew',auto_renew,methods=['POST'])
    b.app.add_url_rule('/api/billing/webhook','billing_webhook',webhook,methods=['POST'])
    b.app.add_url_rule('/payment-return','payment_return',lambda: '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Непростопост</title><h1>Вернитесь в приложение MAX</h1><p>Статус оплаты проверяется сервером. В профиле нажмите «Проверить оплату».</p></html>',methods=['GET'])
