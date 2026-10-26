import logging
import ipaddress
import math
import os
import secrets
import threading
import time
from collections import deque

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse


ADMIN_PATHS = frozenset({
    '/api/auth/create-key', '/api/domain/rotate', '/api/export/google-sheets',
})
SECURITY_HEADERS = {
    'Content-Security-Policy': (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; "
        "media-src 'self' blob:; frame-src 'none'; frame-ancestors 'none'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'"
    ),
    'X-Frame-Options': 'DENY',
    'X-Content-Type-Options': 'nosniff',
    'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
    'Server': 'CADS-Pro',
}
audit = logging.getLogger('uvicorn.error')


class RateLimiter:
    """shortcut: limites por processo; usar armazenamento compartilhado antes de adotar varios workers."""

    def __init__(self):
        self.limits = {
            'public': int(os.environ.get('RATE_LIMIT_PUBLIC', '60')),
            'processing': int(os.environ.get('RATE_LIMIT_PROCESSING', '10')),
            'admin': int(os.environ.get('RATE_LIMIT_ADMIN', '5')),
        }
        if any(limit < 1 for limit in self.limits.values()):
            raise ValueError('RATE_LIMIT_* deve ser um inteiro positivo.')
        self.requests = {}
        self.lock = threading.Lock()
        self.last_cleanup = time.monotonic()

    def check(self, ip, group):
        now = time.monotonic()
        with self.lock:
            if now - self.last_cleanup >= 60:
                self.requests = {key: entries for key, entries in self.requests.items()
                                 if entries and entries[-1] > now - 60}
                self.last_cleanup = now
            entries = self.requests.setdefault((ip, group), deque())
            while entries and entries[0] <= now - 60:
                entries.popleft()
            if len(entries) >= self.limits[group]:
                return max(1, math.ceil(60 - (now - entries[0])))
            entries.append(now)
        return 0


class RequestGuardMiddleware:
    def __init__(self, app):
        self.app = app
        self.limiter = RateLimiter()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = scope['path'].rstrip('/')
        ip = (scope.get('client') or ('unknown',))[0]
        if ip in {'127.0.0.1', '::1'}:
            try:
                ip = str(ipaddress.ip_address(Headers(scope=scope).get('x-real-ip', '')))
            except ValueError:
                pass
        is_admin = path in ADMIN_PATHS
        group = 'admin' if is_admin else ('public' if scope['method'] in {'GET', 'HEAD'} else 'processing')
        customer = scope.get('state', {}).get('customer')
        rate_key = str(customer['id']) + ':' + ip if customer else ip
        retry_after = self.limiter.check(rate_key, group) if path.startswith('/api/') else 0
        if retry_after:
            if path == '/api/auth/create-key':
                audit.warning('admin_key_creation timestamp=%s ip=%s success=false status=429',
                              time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), ip)
            response = JSONResponse({'error': 'Too many requests'}, status_code=429,
                                    headers={'Retry-After': str(retry_after)})
            return await response(scope, receive, send)
        if is_admin:
            headers = Headers(scope=scope)
            master = os.environ.get('ADMIN_API_KEY', '').strip()
            authorization = headers.get('authorization', '').split(None, 1)
            bearer = authorization[1] if len(authorization) == 2 and authorization[0].lower() == 'bearer' else ''
            candidates = (bearer, headers.get('x-admin-key', ''))
            authenticated = bool(master) and any(
                secrets.compare_digest(token.encode('utf-8'), master.encode('utf-8'))
                for token in candidates if token
            )
            if not authenticated:
                if path == '/api/auth/create-key':
                    audit.warning('admin_key_creation timestamp=%s ip=%s success=false status=401',
                                  time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), ip)
                response = JSONResponse({'error': 'Authentication required'}, status_code=401,
                                        headers={'WWW-Authenticate': 'Bearer'})
                return await response(scope, receive, send)

        async def audited_send(message):
            if path == '/api/auth/create-key' and message['type'] == 'http.response.start':
                status = message['status']
                audit.info('admin_key_creation timestamp=%s ip=%s success=%s status=%s',
                           time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), ip,
                           str(200 <= status < 300).lower(), status)
            await send(message)

        try:
            await self.app(scope, receive, audited_send)
        except Exception:
            if path == '/api/auth/create-key':
                audit.error('admin_key_creation timestamp=%s ip=%s success=false status=500',
                            time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), ip)
            raise


class SecurityHeadersMiddleware:
    """Fica fora do FastAPI para cobrir tambem respostas de erro 500."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def secured_send(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)
        await self.app(scope, receive, secured_send)
