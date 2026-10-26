import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from server import accounts, main, product_video


class CustomerPortalTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='stealthforge-accounts-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        processed = self.root / 'processed'
        processed.mkdir()
        changes = [
            patch.object(accounts, 'DATABASE_PATH', self.root / 'customers.sqlite3'),
            patch.object(accounts, 'CUSTOMER_ROOT', self.root / 'customers'),
            patch.object(accounts, 'AUTH_LIMITER', accounts.auth_limiter()),
            patch.multiple(main, DB_PATH=str(self.root / 'cads_auth.db'), PROCESSED_DIR=str(processed),
                           CADS_DATA_DIR=str(self.root), STATS={'recent_activities': [], 'plan_limit': 5000}, CUSTOMER_STATS={}),
            patch.object(main.app.app, 'middleware_stack', None),
            patch.dict(os.environ, {'RATE_LIMIT_PUBLIC': '1000', 'RATE_LIMIT_PROCESSING': '1000', 'RATE_LIMIT_ADMIN': '1000', 'ADMIN_API_KEY': 'test-only-administrative-key'}),
        ]
        for change in changes:
            change.start()
            self.addCleanup(change.stop)
        accounts.initialize()
        main.init_db()
        self.owner_id = accounts.create_customer('Owner', 'owner@example.test', 'test-owner-password', role='admin', status='approved')
        self.owner = TestClient(main.app, base_url='https://testserver')
        self.owner.cookies.set(accounts.COOKIE, accounts.new_session(self.owner_id))

    def register(self, email='customer@example.test'):
        client = TestClient(main.app, base_url='https://testserver')
        response = client.post('/api/account/register', json={'name': 'Test Customer', 'email': email, 'password': 'test-customer-password'})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def approve(self, client):
        customer_id = client.get('/api/account/me').json()['id']
        response = self.owner.post(f'/api/account/clients/{customer_id}/status', json={'status': 'approved'})
        self.assertEqual(response.status_code, 200, response.text)
        return customer_id

    def photo(self, client):
        photo = io.BytesIO()
        Image.new('RGB', (32, 32), 'blue').save(photo, 'PNG')
        response = client.post('/api/camouflage/image', files={'file': ('test.png', photo.getvalue(), 'image/png')})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_registration_pending_and_approval_unlock_processing(self):
        client = self.register()
        self.assertEqual(client.get('/api/account/me').json()['status'], 'pending')
        self.assertEqual(client.get('/studio.html').status_code, 200)
        self.assertEqual(client.get('/api/stats').json()['protected_files'], 0)
        self.assertEqual(client.post('/api/camouflage/image').status_code, 403)
        self.approve(client)
        self.assertEqual(client.get('/api/account/me').json()['status'], 'approved')
        self.photo(client)

    def test_files_statistics_and_notifications_are_isolated(self):
        first = self.register('first@example.test')
        second = self.register('second@example.test')
        self.approve(first)
        self.approve(second)
        output = self.photo(first)
        self.assertEqual(first.get(output['download_url']).status_code, 200)
        self.assertEqual(second.get(output['download_url']).status_code, 404)
        self.assertEqual(second.post('/api/file/delete', data={'filename': output['filename']}).json()['status'], 'error')
        self.assertEqual(first.get('/api/stats').json()['protected_files'], 1)
        self.assertEqual(second.get('/api/stats').json()['protected_files'], 0)
        first_notifications = first.get('/api/account/notifications').json()
        second_notifications = second.get('/api/account/notifications').json()
        self.assertIn(output['filename'], str(first_notifications))
        self.assertNotIn(output['filename'], str(second_notifications))
        self.assertEqual(first.post('/api/account/notifications/read').status_code, 200)
        self.assertEqual(first.get('/api/account/notifications').json()['unread_count'], 0)
        self.assertGreater(second.get('/api/account/notifications').json()['unread_count'], 0)

    def test_cookie_password_and_session_storage(self):
        client = self.register()
        cookie = client.cookies.get(accounts.COOKIE)
        with accounts.database() as connection:
            row = connection.execute("SELECT password_hash FROM customers WHERE email='customer@example.test'").fetchone()
            self.assertNotIn('test-customer-password', row['password_hash'])
            self.assertTrue(accounts.password_matches('test-customer-password', row['password_hash']))
            self.assertNotEqual(connection.execute('SELECT token_hash FROM customer_sessions WHERE customer_id<>?', (self.owner_id,)).fetchone()[0], cookie)
        response = client.post('/api/account/login', json={'email': 'customer@example.test', 'password': 'test-customer-password'})
        header = response.headers['set-cookie'].lower()
        for flag in ['secure', 'httponly', 'samesite=strict', 'path=/']:
            self.assertIn(flag, header)
        self.assertNotIn('domain=', header)

    def test_logout_expiry_and_suspension_revoke_access(self):
        client = self.register()
        customer_id = self.approve(client)
        token = client.cookies.get(accounts.COOKIE)
        self.assertEqual(client.post('/api/account/logout').status_code, 200)
        client.cookies.set(accounts.COOKIE, token)
        self.assertEqual(client.get('/api/account/me').status_code, 401)
        client.cookies.clear()
        self.assertEqual(client.post('/api/account/login', json={'email': 'customer@example.test', 'password': 'test-customer-password'}).status_code, 200)
        with accounts.database() as connection:
            connection.execute('UPDATE customer_sessions SET expires_at=? WHERE customer_id=?', (time.time() - 1, customer_id))
        self.assertEqual(client.get('/api/account/me').status_code, 401)
        client.cookies.clear()
        client.post('/api/account/login', json={'email': 'customer@example.test', 'password': 'test-customer-password'})
        self.owner.post(f'/api/account/clients/{customer_id}/status', json={'status': 'suspended'})
        self.assertEqual(client.get('/api/stats').status_code, 401)
        self.assertEqual(client.post('/api/account/login', json={'email': 'customer@example.test', 'password': 'test-customer-password'}).status_code, 403)

    def test_no_auth_no_admin_escalation_and_cross_origin_denied(self):
        guest = TestClient(main.app, base_url='https://testserver')
        self.assertEqual(guest.get('/', follow_redirects=False).status_code, 303)
        self.assertEqual(guest.get('/api/stats').status_code, 401)
        self.assertEqual(guest.get('/api/account/clients').status_code, 401)
        response = guest.post('/api/account/register', json={'name': 'Attacker', 'email': 'attempt@example.test', 'password': 'test-customer-password', 'role': 'admin', 'status': 'approved'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(guest.get('/api/account/me').json()['role'], 'customer')
        self.assertEqual(guest.get('/api/account/me').json()['status'], 'pending')
        self.assertEqual(guest.get('/api/account/clients').status_code, 403)
        self.assertEqual(guest.post(f'/api/account/clients/{self.owner_id}/status', json={'status': 'approved'}).status_code, 403)
        self.assertEqual(guest.post('/api/account/logout', headers={'Origin': 'https://evil.example'}).status_code, 403)

    def test_login_rate_limit_and_generic_failure(self):
        client = TestClient(main.app, base_url='https://testserver')
        for _ in range(10):
            response = client.post('/api/account/login', json={'email': 'nobody@example.test', 'password': 'wrong'})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()['detail'], 'E-mail ou senha incorretos.')
        response = client.post('/api/account/login', json={'email': 'nobody@example.test', 'password': 'wrong'})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers['retry-after']), 0)

    def test_studio_proxy_origin_optional_name_and_completion_notification(self):
        client = self.register()
        self.approve(client)
        image = io.BytesIO()
        Image.new('RGB', (32, 32), 'white').save(image, 'PNG')
        with patch.object(product_video, 'request_video', return_value=b'\x00\x00\x00\x18ftypisomtest') as provider:
            response = client.post('/api/studio/generate', headers={'Origin': 'https://testserver'},
                                   data={'scene': 'Cena em estudio com movimento lento.', 'api_key': 'test-only-provider-key', 'provider': 'deapi_ltx'},
                                   files={'file': ('test.png', image.getvalue(), 'image/png')})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('produto da foto', provider.call_args.args[2])
        self.assertNotIn('test-only-provider-key', response.text)
        self.assertEqual(client.get(response.json()['download_url']).status_code, 200)
        self.assertIn(response.json()['filename'], str(client.get('/api/account/notifications').json()))

    def test_same_origin_behind_trusted_local_proxy(self):
        from starlette.requests import Request
        request = Request({'type': 'http', 'scheme': 'http', 'path': '/', 'headers': [(b'host', b'stealthforge.com.br'), (b'x-forwarded-proto', b'https')], 'client': ('127.0.0.1', 1000), 'server': ('127.0.0.1', 8888), 'query_string': b''})
        self.assertEqual(accounts.expected_origin(request), 'https://stealthforge.com.br')

    def test_concurrent_customers_keep_their_context_and_private_cache(self):
        from concurrent.futures import ThreadPoolExecutor
        first = self.register('parallel-first@example.test')
        second = self.register('parallel-second@example.test')
        self.approve(first)
        self.approve(second)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs = list(pool.map(self.photo, [first, second]))
        self.assertEqual(first.get('/api/stats').json()['protected_files'], 1)
        self.assertEqual(second.get('/api/stats').json()['protected_files'], 1)
        self.assertEqual(first.get(outputs[1]['download_url']).status_code, 404)
        self.assertEqual(second.get(outputs[0]['download_url']).status_code, 404)
        self.assertIn('no-store', first.get('/api/stats').headers['cache-control'])

    def test_master_key_integration_still_works_without_browser_session(self):
        client = TestClient(main.app, base_url='https://testserver')
        response = client.post('/api/auth/create-key', headers={'X-Admin-Key': 'test-only-administrative-key'}, data={'plan': 'free'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['api_key'].startswith('CADS-'))
        self.assertEqual(client.get('/api/stats').status_code, 401)

    def test_one_customers_rate_limit_does_not_block_another(self):
        first = self.register('limited-first@example.test')
        second = self.register('limited-second@example.test')
        self.approve(first)
        self.approve(second)
        with patch.dict(os.environ, {'RATE_LIMIT_PUBLIC': '2'}), patch.object(main.app.app, 'middleware_stack', None):
            self.assertEqual(first.get('/api/stats').status_code, 200)
            self.assertEqual(first.get('/api/stats').status_code, 200)
            self.assertEqual(first.get('/api/stats').status_code, 429)
            self.assertEqual(second.get('/api/stats').status_code, 200)

if __name__ == '__main__':
    unittest.main()
