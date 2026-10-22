import io
import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import test_app as base
app=base.app
worker=base.worker
import billing
import reminders


class PaidFeaturesTests(unittest.TestCase):
    setUp = base.AppTests.setUp

    def free(self):
        with app.db() as db:
            db.execute("UPDATE app_users SET paid_until=NULL WHERE id='123'");db.commit()

    def group(self, chat='-1'):
        with app.db() as db:
            db.execute('INSERT INTO app_groups VALUES(?,?,?)',('123',chat,'Группа'));db.commit()

    def test_free_limits_groups_catalog_and_planner(self):
        self.free()
        self.assertEqual(self.client.get('/api/me',headers=self.auth).json['subscription'],'free')
        with patch.object(app,'get_admin_group',return_value=True),patch.object(app,'max_api',return_value=Mock(ok=True,json=lambda:{'title':'Группа'})):
            self.assertEqual(self.client.post('/api/connect_chat',headers=self.auth,json={'chat_id':'-1'}).status_code,200)
            self.assertEqual(self.client.post('/api/connect_chat',headers=self.auth,json={'chat_id':'-2'}).status_code,402)
            self.assertEqual(self.client.post('/api/connect_chat',headers=self.auth,json={'chat_id':'-1'}).status_code,200)
        self.assertEqual(self.client.post('/api/add_catalog',headers=self.auth,data={'title':'Первый'}).status_code,201)
        self.assertEqual(self.client.post('/api/add_catalog',headers=self.auth,data={'title':'Второй'}).status_code,402)
        self.assertEqual(self.client.post('/api/add_client',headers=self.auth,data={}).status_code,402)
        self.assertEqual(self.client.get('/api/get_clients',headers=self.auth).status_code,402)

    def test_daily_quota_atomic_and_sent_deletion_does_not_reset_it(self):
        self.free();self.group()
        form={'chat_id':'-1','text':'Тест','run_at':self.future}
        def submit(_):
            with app.app.test_client() as client: return client.post('/api/submit_ad',headers=self.auth,data=form)
        with patch.object(app,'get_admin_group',return_value=True):
            with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(submit,range(2)))
            self.assertEqual(sorted(r.status_code for r in results),[201,402])
            jid=next(r.json['id'] for r in results if r.status_code==201)
            self.client.delete(f'/api/delete_job/{jid}/group',headers=self.auth)
            response=self.client.post('/api/submit_ad',headers=self.auth,data=form)
            self.assertEqual(response.status_code,201)
            jid=response.json['id'];worker.finish(jid,'sent',message_id='test')
            self.client.delete(f'/api/delete_job/{jid}/group',headers=self.auth)
            self.assertEqual(self.client.post('/api/submit_ad',headers=self.auth,data=form).status_code,402)

    def test_worker_rechecks_plan_after_subscription_expires(self):
        self.group('-1');self.group('-2');self.free()
        with app.db() as db:
            db.execute('INSERT INTO app_jobs(id,owner_id,chat_id,text,run_at,status) VALUES(?,?,?,?,?,?)',('job','123','-2','Текст',self.future,'processing'));db.commit()
            job=dict(db.execute("SELECT * FROM app_jobs WHERE id='job'").fetchone())
        with patch.object(app,'max_api') as remote:
            worker.deliver(job);remote.assert_not_called()
        with app.db() as db: self.assertEqual(db.execute("SELECT status FROM app_jobs WHERE id='job'").fetchone()[0],'failed')

    def test_catalog_ad_snapshots_text_and_photos(self):
        self.group()
        response=self.client.post('/api/add_catalog',headers=self.auth,data={'title':'Дом','price':'10 млн','description':'Описание', 'images':(io.BytesIO(b'\x89PNG\r\n\x1a\nfixture'),'photo.png')})
        item=response.json['id']
        with app.db() as db: source=json.loads(db.execute('SELECT images FROM app_catalog WHERE id=?',(item,)).fetchone()[0])[0]
        with patch.object(app,'get_admin_group',return_value=True):
            response=self.client.post('/api/submit_ad',headers=self.auth,data={'chat_id':'-1','catalog_id':item,'run_at':self.future})
        self.assertEqual(response.status_code,201)
        with app.db() as db: job=db.execute('SELECT text,images FROM app_jobs WHERE id=?',(response.json['id'],)).fetchone()
        self.assertIn('Дом',job['text']);snapshot=json.loads(job['images'])[0];self.assertNotEqual(source,snapshot)
        self.client.delete('/api/catalog/'+item,headers=self.auth)
        self.assertTrue((app.UPLOAD/snapshot).exists());self.assertFalse((app.UPLOAD/source).exists())

    def test_reminder_goes_to_owner_with_prepared_text(self):
        response=self.client.post('/api/add_client',headers=self.auth,data={'name':'Клиент','date':self.future[:10],'time':'12:00','run_at':self.future,'prepared_message':'Здравствуйте! Хотите посмотреть объект?'})
        self.assertEqual(response.status_code,201);cid=response.json['id']
        with app.db() as db: db.execute('UPDATE app_clients SET reminder_at=? WHERE id=?',(app.iso(app.utcnow()-timedelta(seconds=1)),cid));db.commit()
        with patch.object(app,'max_api',return_value=Mock(ok=True,status_code=200,json=lambda:{'message':{'body':{'mid':'reminded'}}})) as remote:
            reminders.process_one()
            self.assertEqual(remote.call_args.kwargs['params'],{'user_id':'123'})
            self.assertIn('Здравствуйте!',remote.call_args.kwargs['json']['text'])
            reminders.process_one();self.assertEqual(remote.call_count,1)


class BillingTests(unittest.TestCase):
    setUp = base.AppTests.setUp

    def set_config(self):
        return patch.dict(os.environ,{'YOOKASSA_SHOP_ID':'test-shop','YOOKASSA_SECRET_KEY':'synthetic-provider-key',
                    'YOOKASSA_RETURN_URL':'https://example.test/payment-return','YOOKASSA_RECEIPT_MODE':'external','YOOKASSA_TEST_MODE':'0'})

    def order(self,consent=False,method='bank_card'):
        with app.db() as db:
            db.execute('BEGIN IMMEDIATE');oid=billing.create_order(db,'123',method,consent,'test@example.test');db.commit()
        return oid

    def payment(self,oid,status='succeeded',saved=True):
        return {'id':str(uuid.uuid4()),'status':status,'paid':status=='succeeded','test':False,
                'amount':{'value':'499.00','currency':'RUB'},'metadata':{'order_id':oid,'owner_id':'123','product':'pro-month-v1'},
                'payment_method':{'id':'saved-method','saved':saved},
                'confirmation':{'confirmation_url':'https://yookassa.ru/checkout/test'}}

    def test_checkout_methods_explicit_consent_and_receipt_guard(self):
        with self.set_config():
            for method,consent in [('bank_card',False),('sbp',True)]:
                with app.db() as db: db.execute('DELETE FROM app_payments');db.commit()
                def created(_method,_path,payload=None,key=None):
                    data=self.payment(payload['metadata']['order_id'],'pending');data['payment_method']['saved']=False
                    return data
                with patch.object(billing,'provider',side_effect=created) as remote:
                    response=self.client.post('/api/billing/checkout',headers=self.auth,json={'method':method,'auto_renew':consent})
                self.assertEqual(response.status_code,200)
                payload=remote.call_args.args[2]
                self.assertEqual(payload['payment_method_data']['type'],method)
                self.assertEqual(payload.get('save_payment_method',False),consent)
            with patch.dict(os.environ,{'YOOKASSA_RECEIPT_MODE':''}):
                self.assertEqual(self.client.post('/api/billing/checkout',headers=self.auth,json={'method':'sbp'}).status_code,503)
            with patch.dict(os.environ,{'YOOKASSA_RECEIPT_MODE':'yookassa','YOOKASSA_VAT_CODE':'1'}):
                receipt=billing.receipt('buyer@example.test');self.assertEqual(receipt['items'][0]['amount']['value'],'499.00')
                with self.assertRaises(billing.BillingError):billing.receipt('invalid-email')

    def test_verified_payment_applied_once_and_cancel_does_not_reenable_renewal(self):
        with self.set_config():
            oid=self.order(True);data=self.payment(oid)
            billing.apply_payment(oid,data)
            with app.db() as db: until=db.execute("SELECT paid_until FROM app_users WHERE id='123'").fetchone()[0]
            billing.apply_payment(oid,data)
            with app.db() as db: self.assertEqual(db.execute("SELECT paid_until FROM app_users WHERE id='123'").fetchone()[0],until)
            self.assertTrue(billing.public_state('123')['auto_renew'])
            response=self.client.post('/api/billing/auto-renew',headers=self.auth,json={'enabled':False})
            self.assertEqual(response.status_code,200);self.assertFalse(response.json['auto_renew'])
            billing.apply_payment(oid,data);self.assertFalse(billing.public_state('123')['auto_renew'])
            oid2=self.order(True)
            self.client.post('/api/billing/auto-renew',headers=self.auth,json={'enabled':False})
            billing.apply_payment(oid2,self.payment(oid2));self.assertFalse(billing.public_state('123')['auto_renew'])

    def test_wrong_amount_owner_and_test_mode_do_not_grant_access(self):
        with self.set_config():
            oid=self.order();data=self.payment(oid)
            for mutate in [lambda p:p['amount'].update(value='1.00'),lambda p:p['metadata'].update(owner_id='other'),lambda p:p.update(test=True)]:
                data=self.payment(oid);mutate(data)
                with self.assertRaises(billing.BillingError):billing.apply_payment(oid,data)
                with app.db() as db: self.assertEqual(db.execute('SELECT applied FROM app_payments WHERE id=?',(oid,)).fetchone()[0],0)

    def test_webhook_refetches_provider_and_ignores_forged_status(self):
        with self.set_config():
            oid=self.order();data=self.payment(oid,'pending')
            with patch.object(billing,'provider',return_value=data) as remote:
                response=self.client.post('/api/billing/webhook',json={'event':'payment.succeeded','object':{'id':data['id'],'metadata':{'order_id':oid},'status':'succeeded','paid':True}})
                self.assertEqual(response.status_code,200)
                self.assertEqual(remote.call_args.args,('GET','/payments/'+data['id']))
            with app.db() as db: self.assertEqual(db.execute('SELECT applied FROM app_payments WHERE id=?',(oid,)).fetchone()[0],0)
            data['status']='succeeded';data['paid']=True
            with patch.object(billing,'provider',return_value=data):
                for _ in range(2): self.assertEqual(self.client.post('/api/billing/webhook',json={'event':'payment.succeeded','object':{'id':data['id'],'metadata':{'order_id':oid}}}).status_code,200)
            with app.db() as db: self.assertEqual(db.execute('SELECT applied FROM app_payments WHERE id=?',(oid,)).fetchone()[0],1)

    def test_payment_timeout_reuses_same_idempotency_key(self):
        with self.set_config():
            oid=self.order()
            with patch.object(billing,'provider',side_effect=billing.BillingError('timeout')) as remote:
                for _ in range(2):
                    with self.assertRaises(billing.BillingError): billing.process_order(oid)
                self.assertEqual(remote.call_args_list[0].args,remote.call_args_list[1].args)
                self.assertEqual(remote.call_args.args[3],oid)

    def test_renewal_uses_saved_method_only_with_consent_and_stops_after_cancel(self):
        with self.set_config():
            oid=self.order(True);billing.apply_payment(oid,self.payment(oid))
            with app.db() as db: db.execute("UPDATE app_subscriptions SET next_charge_at=? WHERE owner_id='123'",(app.iso(app.utcnow()-timedelta(seconds=1)),));db.commit()
            def renewed(method,path,payload=None,key=None):
                self.assertEqual(payload['payment_method_id'],'saved-method');self.assertNotIn('confirmation',payload)
                return self.payment(payload['metadata']['order_id'])
            with patch.object(billing,'provider',side_effect=renewed) as remote:
                billing.tick();self.assertEqual(remote.call_count,1)
                billing.tick();self.assertEqual(remote.call_count,1)
            self.client.post('/api/billing/auto-renew',headers=self.auth,json={'enabled':False})
            with app.db() as db: db.execute("UPDATE app_subscriptions SET next_charge_at=? WHERE owner_id='123'",(app.iso(app.utcnow()-timedelta(seconds=1)),));db.commit()
            with patch.object(billing,'provider') as remote: billing.tick();remote.assert_not_called()

    def test_provider_request_has_auth_idempotency_and_preserves_tls(self):
        with self.set_config(),patch.object(billing.requests,'request',return_value=Mock(ok=True,json=lambda:{'status':'pending'})) as transport:
            billing.provider('POST','/payments',{'amount':{'value':'499.00','currency':'RUB'}},'stable-key')
            args=transport.call_args
            self.assertEqual(args.args,('POST','https://api.yookassa.ru/v3/payments'))
            self.assertEqual(args.kwargs['headers']['Idempotence-Key'],'stable-key')
            self.assertEqual(args.kwargs['auth'],('test-shop','synthetic-provider-key'))
            self.assertNotEqual(args.kwargs.get('verify'),False)

    def test_no_saved_method_means_no_auto_renew_even_with_consent(self):
        with self.set_config():
            oid=self.order(True,'sbp');billing.apply_payment(oid,self.payment(oid,saved=False))
            state=billing.public_state('123');self.assertFalse(state['auto_renew']);self.assertFalse(state['saved_method'])
            self.assertIn('не сохранила',state['error'])

    def test_renewal_declines_stop_after_three_attempts(self):
        with self.set_config():
            oid=self.order(True);billing.apply_payment(oid,self.payment(oid))
            def declined(method,path,payload=None,key=None):return self.payment(payload['metadata']['order_id'],'canceled')
            with patch.object(billing,'provider',side_effect=declined):
                for _ in range(3):
                    with app.db() as db:db.execute("UPDATE app_subscriptions SET next_charge_at=? WHERE owner_id='123'",(app.iso(app.utcnow()-timedelta(seconds=1)),));db.commit()
                    billing.tick()
            state=billing.public_state('123');self.assertFalse(state['auto_renew']);self.assertIn('отменила',state['error'])
            with app.db() as db:self.assertEqual(db.execute("SELECT failures FROM app_subscriptions WHERE owner_id='123'").fetchone()[0],3)

    def test_test_payment_does_not_enable_live_access(self):
        with self.set_config(),patch.dict(os.environ,{'YOOKASSA_TEST_MODE':'1','YOOKASSA_RECEIPT_MODE':'test'}):
            oid=self.order(True);data=self.payment(oid);data['test']=True
            billing.apply_payment(oid,data)
            with app.db() as db:self.assertTrue(app.has_pro(db,'123'))
        with self.set_config():
            with app.db() as db:self.assertFalse(app.has_pro(db,'123'))
            with patch.object(billing,'provider') as remote:billing.tick();remote.assert_not_called()

    def test_pending_payment_belongs_to_owner_and_unknown_webhook_does_not_call_api(self):
        with self.set_config():
            oid=self.order()
            token=self.client.post('/api/auth/max',json={'init_data':base.signed(456)}).json['token']
            self.assertEqual(self.client.post('/api/billing/payments/'+oid,headers={'Authorization':'Bearer '+token}).status_code,404)
            with patch.object(billing,'provider') as remote:
                response=self.client.post('/api/billing/webhook',json={'event':'payment.succeeded','object':{'id':str(uuid.uuid4())}})
                self.assertEqual(response.status_code,200);remote.assert_not_called()

    def test_month_end_calendar_extension(self):
        self.assertEqual(billing.next_month(datetime(2027,1,31,tzinfo=timezone.utc)).day,28)
        self.assertEqual(billing.next_month(datetime(2028,1,31,tzinfo=timezone.utc)).day,29)


if __name__=='__main__':unittest.main()
