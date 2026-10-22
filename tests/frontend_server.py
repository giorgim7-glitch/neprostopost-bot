"""Disposable HTTP server for frontend tests; MAX calls are fixtures only."""
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from werkzeug.serving import make_server, WSGIRequestHandler

with tempfile.TemporaryDirectory(prefix='neprostopost-frontend-') as data:
    os.environ['DATA_DIR'] = data
    os.environ['MAX_BOT_TOKEN'] = 'synthetic-test-key-not-a-real-token'
    os.environ['FRONTEND_ORIGIN'] = 'https://frontend.example'
    os.environ.update(YOOKASSA_SHOP_ID='fixture-shop',YOOKASSA_SECRET_KEY='synthetic-fixture-key',YOOKASSA_RETURN_URL='https://frontend.example/payment-return',YOOKASSA_RECEIPT_MODE='external',YOOKASSA_TEST_MODE='0')
    import app
    import billing,uuid,copy
    fixture_payments={}
    def fixture_provider(method,path,payload=None,key=None):
        if method=='POST':
            if key not in fixture_payments:
                fixture_payments[key]={'id':str(uuid.uuid4()),'status':'pending','paid':False,'test':False,'amount':payload['amount'],'metadata':payload['metadata'],'confirmation':{'confirmation_url':'https://yookassa.ru/checkout/fixture'},'payment_method':{'id':'fixture-method','saved':payload.get('save_payment_method',False)}}
            return copy.deepcopy(fixture_payments[key])
        payment=next(value for value in fixture_payments.values() if path.endswith(value['id']))
        payment['status']='succeeded';payment['paid']=True
        return copy.deepcopy(payment)
    billing.provider=fixture_provider
    from datetime import timedelta
    with app.db() as db:
        now=app.iso(app.utcnow())
        db.execute('INSERT INTO app_users(id,first_name,created_at,trial_end,paid_until) VALUES(?,?,?,?,?)',('123','Тест',now,now,app.iso(app.utcnow()+timedelta(days=30))));db.commit()
    from unittest.mock import Mock
    app.get_admin_group = lambda chat_id, user_id, **kwargs: True
    app.max_api = lambda *args, **kwargs: Mock(ok=True, json=lambda: {'title':'Тестовая группа','chats':[{'chat_id':-123,'title':'Тестовая группа','type':'chat'}]})
    import max_events
    with app.db() as db:
        max_events.remember(db,{'update_type':'message_created','message':{'recipient':{'chat_type':'chat','chat_id':-123}}})
        db.commit()
    class QuietHandler(WSGIRequestHandler):
        def log_request(self, *args, **kwargs): pass
    server = make_server('127.0.0.1', 0, app.app, request_handler=QuietHandler)
    print(server.server_port, flush=True)
    server.serve_forever()
