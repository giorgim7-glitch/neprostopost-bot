import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch
from urllib.parse import urlencode

# All tests use isolated data and a synthetic key, never the running service's credentials.
_temp = tempfile.TemporaryDirectory(prefix='neprostopost-tests-')
os.environ['DATA_DIR'] = _temp.name
os.environ['MAX_BOT_TOKEN'] = 'synthetic-test-key-not-a-real-token'
import app
import worker
import max_events


def signed(user_id=123, **changes):
    data = {'auth_date':str(int(app.utcnow().timestamp())), 'user':json.dumps({'id':user_id,'first_name':'Тест'})}
    data.update(changes)
    secret = hmac.new(b'WebAppData', app.TOKEN.encode(), hashlib.sha256).digest()
    content = '\n'.join(f'{k}={v}' for k,v in sorted(data.items()))
    data['hash'] = hmac.new(secret, content.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        with app.db() as db:
            for table in ('app_jobs','app_groups','app_clients','app_catalog','app_sessions','app_users','app_known_chats','app_bot_state'):
                db.execute(f'DELETE FROM {table}')
            db.commit()
        response = self.client.post('/api/auth/max',json={'init_data':signed()})
        self.assertEqual(response.status_code,200)
        self.auth = {'Authorization':'Bearer '+response.json['token']}
        self.future = app.iso(app.utcnow()+timedelta(hours=1))

    def test_frontend_and_access_control(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'/static/app.js', response.data)
        response.close()
        self.assertEqual(self.client.get('/api/health').json['status'],'ok')
        self.assertEqual(self.client.get('/api/me').status_code,401)
        self.assertEqual(self.client.get('/api/me',headers=self.auth).json['user']['id'],'123')
        self.client.post('/api/auth/logout',headers=self.auth)
        self.assertEqual(self.client.get('/api/me',headers=self.auth).status_code,401)

    def test_signed_auth_rejects_tampered_expired_and_duplicate_data(self):
        self.assertIsNone(app.verify_max(signed()+'x'))
        self.assertIsNone(app.verify_max(signed(auth_date='1')))
        self.assertIsNone(app.verify_max(signed()+'&auth_date=1'))
        with patch.object(app,'TOKEN',''):
            self.assertEqual(self.client.post('/api/auth/max',json={'init_data':signed()}).status_code,503)

    def test_catalog_images_ownership_and_deletion(self):
        image=b'\x89PNG\r\n\x1a\n'+b'fixture'
        r=self.client.post('/api/add_catalog',headers=self.auth,data={'title':'<script>test</script>','description':'Описание','images':(io.BytesIO(image),'image.png')})
        self.assertEqual(r.status_code,201)
        item_id=r.json['id']
        catalog=self.client.get('/api/get_catalog',headers=self.auth).json['catalog']
        url=catalog[0]['image_urls'][0].replace('http://localhost','')
        self.assertEqual(self.client.get(url).status_code,401)
        response=self.client.get(url,headers=self.auth)
        self.assertEqual(response.data,image)
        response.close()
        other=self.client.post('/api/auth/max',json={'init_data':signed(456)}).json['token']
        headers={'Authorization':'Bearer '+other}
        self.assertEqual(self.client.get(url,headers=headers).status_code,404)
        self.assertEqual(self.client.delete('/api/catalog/'+item_id,headers=headers).status_code,404)
        self.assertEqual(self.client.delete('/api/catalog/'+item_id,headers=self.auth).status_code,200)
        self.assertEqual(self.client.get(url,headers=self.auth).status_code,404)
        r=self.client.post('/api/add_catalog',headers=self.auth,data={'title':'Bad image','images':(io.BytesIO(b'not-image'),'image.png')})
        self.assertEqual(r.status_code,400)

    def test_client_flow_and_dates(self):
        data={'name':'Клиент','date':self.future[:10],'time':'12:00','type':'Встреча','run_at':self.future}
        r=self.client.post('/api/add_client',headers=self.auth,data=data)
        self.assertEqual(r.status_code,201)
        cid=r.json['id']
        self.assertEqual(self.client.get('/api/get_clients',headers=self.auth).json['clients'][0]['type'],'Встреча')
        self.assertEqual(self.client.get('/api/get_stats',headers=self.auth).json['clients'],1)
        self.assertEqual(len(self.client.get('/api/get_jobs',headers=self.auth).json['jobs']),1)
        self.assertEqual(self.client.delete(f'/api/delete_job/{cid}/client',headers=self.auth).status_code,200)
        for value in ('invalid','2026-01-01T12:00:00',app.iso(app.utcnow()-timedelta(days=1))):
            data['run_at']=value
            self.assertEqual(self.client.post('/api/add_client',headers=self.auth,data=data).status_code,400)

    def add_job(self):
        with patch.object(app,'get_admin_group',return_value=True), patch.object(app,'max_api',return_value=Mock(ok=True,json=lambda:{'title':'Группа'})):
            r=self.client.post('/api/connect_chat',headers=self.auth,json={'chat_id':'-123'})
            self.assertEqual(r.status_code,200)
            r=self.client.post('/api/submit_ad',headers=self.auth,data={'chat_id':'-123','text':'Публикация','run_at':self.future})
            self.assertEqual(r.status_code,201)
        jid=r.json['id']
        with app.db() as db:
            db.execute('UPDATE app_jobs SET run_at=? WHERE id=?',(app.iso(app.utcnow()-timedelta(seconds=1)),jid)); db.commit()
        return jid

    def status(self,jid):
        with app.db() as db: return db.execute('SELECT * FROM app_jobs WHERE id=?',(jid,)).fetchone()

    def test_worker_claim_delivery_and_no_duplicate(self):
        jid=self.add_job(); job=worker.claim()
        self.assertEqual(job['id'],jid)
        self.assertIsNone(worker.claim())
        response=Mock(ok=True,status_code=200,json=lambda:{'message':{'body':{'mid':'delivered-123'}}})
        with patch.object(app,'get_admin_group',return_value=True),patch.object(app,'max_api',return_value=response) as call:
            worker.deliver(job)
            self.assertEqual(call.call_args.kwargs['json']['text'],'Публикация')
        self.assertEqual(self.status(jid)['status'],'sent')
        self.assertEqual(self.status(jid)['message_id'],'delivered-123')
        self.assertIsNone(worker.claim())

    def test_worker_ambiguous_timeout_is_not_retried(self):
        jid=self.add_job(); job=worker.claim()
        with patch.object(app,'get_admin_group',return_value=True),patch.object(app,'max_api',side_effect=app.requests.Timeout):
            worker.deliver(job)
        self.assertEqual(self.status(jid)['status'],'uncertain')
        self.assertIsNone(worker.claim())

    def test_worker_rate_limit_and_permission_revoked(self):
        jid=self.add_job(); job=worker.claim()
        with patch.object(app,'get_admin_group',return_value=True),patch.object(app,'max_api',return_value=Mock(status_code=429,json=lambda:{})):
            worker.deliver(job)
        self.assertEqual(self.status(jid)['status'],'scheduled')
        self.assertIsNone(worker.claim())
        with patch.object(app,'get_admin_group',return_value=False): worker.deliver(job)
        self.assertEqual(self.status(jid)['status'],'failed')

    def test_disconnect_cancels_pending_and_blocks_processing(self):
        jid=self.add_job(); worker.claim()
        self.assertEqual(self.client.delete('/api/groups/-123',headers=self.auth).status_code,409)
        self.assertEqual(self.client.delete(f'/api/delete_job/{jid}/group',headers=self.auth).status_code,409)
        worker.finish(jid,'scheduled')
        self.assertEqual(self.client.delete('/api/groups/-123',headers=self.auth).status_code,200)
        self.assertEqual(self.status(jid)['status'],'cancelled')

    def test_group_discovery_uses_events_and_does_not_call_removed_endpoint(self):
        with app.db() as db:
            for cid,title in [('-1','Моя группа'),('-2','Чужая группа'),('-4','Недоступная группа')]:
                db.execute('INSERT INTO app_known_chats VALUES(?,?,1,?)',(cid,title,app.iso(app.utcnow())))
            db.commit()
        def permission(chat_id,user_id,timeout=25):
            self.assertEqual(user_id,'123')
            return {'-1':True,'-2':False,'-4':None}[chat_id]
        self.assertEqual(self.client.get('/api/discover_chats').status_code,401)
        with patch.object(app,'max_api') as call, patch.object(app,'get_admin_group',side_effect=permission):
            result=self.client.get('/api/discover_chats',headers=self.auth)
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.json['chats'],[{'id':'-1','title':'Моя группа','connected':False}])
            self.assertIsNone(result.json['marker'])
            self.assertEqual(result.json['unavailable_count'],1)
            call.assert_not_called()
        self.assertEqual(self.client.get('/api/discover_chats?marker=invalid',headers=self.auth).status_code,400)

    def test_max_events_group_registration_and_persistent_marker(self):
        payload={'updates':[
            {'update_type':'message_created','message':{'recipient':{'chat_type':'chat','chat_id':-123},'body':{'text':'/connect'}}},
            {'update_type':'message_created','message':{'recipient':{'chat_type':'dialog','chat_id':123}}},
            {'update_type':'chat_title_changed','chat_id':-123,'title':'Моя группа'},
            {'update_type':'message_created','message':None},
        ],'marker':100}
        with patch.object(app,'max_api',return_value=Mock(ok=True,json=lambda:payload)) as call:
            self.assertTrue(max_events.poll_updates())
            self.assertEqual(call.call_args.args,('GET','/updates'))
        with app.db() as db:
            rows=db.execute('SELECT * FROM app_known_chats').fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['title'],'Моя группа')
        self.assertEqual(max_events.state('updates_marker'),'100')
        with patch.object(app,'max_api',return_value=Mock(ok=True,json=lambda:{'updates':[],'marker':101})) as call:
            self.assertTrue(max_events.poll_updates())
            self.assertEqual(call.call_args.kwargs['params']['marker'],'100')
        with patch.object(app,'max_api',side_effect=app.requests.Timeout):
            self.assertFalse(max_events.poll_updates())
        self.assertEqual(max_events.state('updates_marker'),'101')
        self.assertIn('TIMEOUT',max_events.state('updates_error'))

    def test_max_events_removal_cancels_pending_jobs(self):
        jid=self.add_job()
        payload={'updates':[{'update_type':'bot_removed','chat_id':-123}], 'marker':1}
        with patch.object(app,'max_api',return_value=Mock(ok=True,json=lambda:payload)):
            self.assertTrue(max_events.poll_updates())
        self.assertEqual(self.status(jid)['status'],'cancelled')
        with app.db() as db:
            self.assertEqual(db.execute('SELECT active FROM app_known_chats WHERE chat_id=?',('-123',)).fetchone()[0],0)

    def test_max_events_failure_is_actionable_without_leaking_response(self):
        with patch.object(app,'max_api',return_value=Mock(ok=False,status_code=401)):
            self.assertFalse(max_events.poll_updates())
        response=self.client.get('/api/discover_chats',headers=self.auth)
        self.assertEqual(response.status_code,200)
        self.assertIn('MAX отклонил токен',response.json['hint'])
        self.assertNotIn(app.TOKEN,response.get_data(as_text=True))

    def test_worker_polls_events_in_its_main_loop(self):
        with patch.object(max_events,'poll_updates',return_value=True) as poll, \
             patch.object(worker,'claim',return_value=None), \
             patch.object(worker.time,'sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): worker.run()
            poll.assert_called_once()

    def test_connection_diagnostics_classify_errors_without_exposing_secrets(self):
        import socket
        from urllib3.exceptions import NameResolutionError,MaxRetryError
        dns=NameResolutionError('platform-api2.max.ru',None,socket.gaierror(-2,'missing'))
        error=app.requests.ConnectionError(MaxRetryError(None,'/updates',dns))
        self.assertIn('DNS:',max_events.connection_reason(error))
        for error,prefix in [(app.requests.exceptions.SSLError('PRIVATE_TOKEN_MUST_NOT_BE_SHOWN'),'TLS:'),
                             (app.requests.exceptions.ProxyError('https://user:PRIVATE_PASSWORD@proxy'),'PROXY:'),
                             (app.requests.ConnectionError('PRIVATE_TOKEN_MUST_NOT_BE_SHOWN'),'CONNECT:')]:
            message=max_events.connection_reason(error)
            self.assertTrue(message.startswith(prefix))
            self.assertNotIn('PRIVATE',message)
        with patch.object(app,'max_api',side_effect=app.requests.exceptions.SSLError('PRIVATE_TOKEN')):
            self.assertFalse(max_events.poll_updates())
        self.assertIn('TLS:',max_events.state('updates_error'))
        self.assertNotIn('PRIVATE_TOKEN',max_events.state('updates_error'))

    def test_upload_image_contract(self):
        name='a'*32+'.png'; (app.UPLOAD/name).write_bytes(b'fixture')
        with patch.object(app,'max_api',return_value=Mock(json=lambda:{'url':'https://upload.max.ru/test'})),patch.object(worker.requests,'post',return_value=Mock(json=lambda:{'photos':{'1':{'token':'photo-token'}}})) as upload:
            attachments=worker.upload_images([name])
            self.assertIn('data',upload.call_args.kwargs['files'])
            self.assertNotIn('headers',upload.call_args.kwargs)
        self.assertEqual(attachments,[{'type':'image','payload':{'photos':{'1':{'token':'photo-token'}}}}])


if __name__ == '__main__':
    unittest.main()
