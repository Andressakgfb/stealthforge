from auth_helpers import authorize_owner
import asyncio
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from server import main
from server.security import RateLimiter, SECURITY_HEADERS, SecurityHeadersMiddleware


class SecurityTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cads-security-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        processed = self.root / 'processed'
        processed.mkdir()
        for change in (
            patch.dict(os.environ, {'ADMIN_API_KEY': 'test-admin-only', 'RATE_LIMIT_PUBLIC': '60',
                                    'RATE_LIMIT_PROCESSING': '10', 'RATE_LIMIT_ADMIN': '5'}),
            patch.multiple(main, DB_PATH=str(self.root / 'auth.db'), PROCESSED_DIR=str(processed),
                           STATS={'recent_activities': [], 'plan_limit': None}),
            patch.object(main.app.app, 'middleware_stack', None),
        ):
            change.start()
            self.addCleanup(change.stop)
        main.init_db()
        self.client = TestClient(main.app)
        authorize_owner(self, self.client, self.root)
        self.admin = {'Authorization': 'Bearer test-admin-only'}

    def test_sensitive_routes_deny_missing_and_incorrect_auth(self):
        for method, path, data in [('POST', '/api/auth/create-key', {'plan': 'pro'}),
                                    ('POST', '/api/domain/rotate', {'new_domain': 'example.test'}),
                                    ('GET', '/api/export/google-sheets', None)]:
            with self.subTest(path=path):
                response = self.client.request(method, path, data=data)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {'error': 'Authentication required'})
        response = self.client.post('/api/auth/create-key', headers={'X-Admin-Key': 'wrong'}, data={'plan': 'pro'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(list((self.root / 'processed').iterdir()), [])

    def test_valid_bearer_creates_actual_key_without_admin_token_in_response(self):
        response = self.client.post('/api/auth/create-key', headers=self.admin, data={'plan': 'pro'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['api_key'].startswith('CADS-'))
        self.assertNotIn('test-admin-only', response.text)
        self.assertTrue(main.check_api_key(response.json()['api_key'])['allowed'])

    def test_x_admin_key_support_and_signed_in_routes_work(self):
        self.assertEqual(self.client.post('/api/domain/rotate', headers={'X-Admin-Key': 'test-admin-only'},
                                          data={'new_domain': 'example.test'}).status_code, 200)
        self.assertEqual(self.client.get('/api/export/google-sheets', headers=self.admin).status_code, 200)
        self.assertEqual(self.client.get('/api/stats').status_code, 200)
        self.assertEqual(self.client.get('/api/studio/config').status_code, 200)

    def test_admin_access_fails_closed_without_configured_master(self):
        with patch.dict(os.environ, {'ADMIN_API_KEY': ''}):
            self.assertEqual(self.client.post('/api/auth/create-key', headers=self.admin,
                                              data={'plan': 'pro'}).status_code, 401)

    def test_audit_logs_success_and_failure_without_credentials(self):
        with self.assertLogs('uvicorn.error', level='INFO') as logs:
            self.client.post('/api/auth/create-key', data={'plan': 'pro'})
            self.client.post('/api/auth/create-key', data={'plan': 'pro'}, headers=self.admin)
        log = '\n'.join(logs.output)
        self.assertIn('success=false status=401', log)
        self.assertIn('success=true status=200', log)
        self.assertIn('timestamp=', log)
        self.assertIn('ip=', log)
        self.assertNotIn('test-admin-only', log)
        self.assertNotIn('CADS-', log)

    def test_headers_on_success_unauthorized_not_found_and_favicon(self):
        for path in ['/', '/studio.html', '/api/stats', '/api/export/google-sheets',
                     '/does-not-exist', '/favicon.ico']:
            with self.subTest(path=path):
                response = self.client.get(path)
                for header, expected in SECURITY_HEADERS.items():
                    self.assertEqual(response.headers[header], expected)
                self.assertNotIn('uvicorn', response.headers.get('server', '').lower())
        self.assertEqual(self.client.get('/favicon.ico').status_code, 204)

    def test_headers_even_on_unhandled_500_response(self):
        broken = FastAPI()
        @broken.get('/broken')
        def fail():
            raise RuntimeError('test-only failure')
        response = TestClient(SecurityHeadersMiddleware(broken), raise_server_exceptions=False).get('/broken')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    def test_public_burst_limit_and_forwarded_header_cannot_reset_bucket(self):
        for _ in range(60):
            self.assertEqual(self.client.get('/api/stats').status_code, 200)
        response = self.client.get('/api/stats', headers={'X-Forwarded-For': '198.51.100.21'})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers['Retry-After']), 0)
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        other_ip = TestClient(main.app, client=('198.51.100.22', 55000), cookies=self.client.cookies)
        self.assertEqual(other_ip.get('/api/stats').status_code, 200)

    def test_processing_burst_limit_does_not_consume_public_bucket(self):
        for _ in range(10):
            self.assertEqual(self.client.post('/api/camouflage/image').status_code, 422)
        self.assertEqual(self.client.post('/api/studio/generate').status_code, 429)
        self.assertEqual(self.client.get('/api/stats').status_code, 200)

    def test_admin_burst_limit(self):
        for _ in range(5):
            self.assertEqual(self.client.post('/api/auth/create-key', headers=self.admin,
                                              data={'plan': 'free'}).status_code, 200)
        with self.assertLogs('uvicorn.error', level='WARNING') as logs:
            response = self.client.post('/api/auth/create-key', headers=self.admin, data={'plan': 'free'})
        self.assertEqual(response.status_code, 429)
        self.assertIn('status=429', '\n'.join(logs.output))

    def test_stats_null_numbers_and_activity_are_normalized(self):
        main.STATS.update(processed_today=None, protected_files=None, plan_used=None,
                          plan_limit=None, recent_activities=None)
        data = self.client.get('/api/stats').json()
        for name in ['processed_today', 'protected_files', 'plan_used', 'plan_limit']:
            self.assertIsInstance(data[name], int)
        self.assertEqual(data['plan_limit'], 5000)
        self.assertEqual(data['recent_activities'], [])

    def test_invalid_plan_is_rejected_after_authentication(self):
        response = self.client.post('/api/auth/create-key', headers=self.admin, data={'plan': 'invalid'})
        self.assertEqual(response.status_code, 400)


class RateLimiterTest(unittest.TestCase):
    def test_sliding_window_recovers_and_prunes_idle_clients(self):
        with patch('server.security.time.monotonic', return_value=100) as clock:
            limiter = RateLimiter()
            limiter.limits['admin'] = 1
            self.assertEqual(limiter.check('one', 'admin'), 0)
            self.assertEqual(limiter.check('one', 'admin'), 60)
            clock.return_value = 130
            self.assertEqual(limiter.check('one', 'admin'), 30)
            clock.return_value = 161
            self.assertEqual(limiter.check('two', 'admin'), 0)
            self.assertNotIn(('one', 'admin'), limiter.requests)

    def test_concurrent_requests_cannot_overrun_limit(self):
        limiter = RateLimiter()
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: limiter.check('one', 'processing'), range(40)))
        self.assertEqual(results.count(0), limiter.limits['processing'])


if __name__ == '__main__':
    unittest.main()
