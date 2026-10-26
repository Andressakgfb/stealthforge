import hashlib
import hmac
import ipaddress
import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.datastructures import Headers, MutableHeaders
from server.security import ADMIN_PATHS, RateLimiter

STATE_ROOT = Path(os.environ.get('CADS_DATA_DIR') or Path(__file__).resolve().parent)
DATABASE_PATH = STATE_ROOT / 'customers.sqlite3'
CUSTOMER_ROOT = STATE_ROOT / 'customers'
CURRENT_CUSTOMER = ContextVar('stealthforge_customer', default=None)
COOKIE = '__Host-sf_session'
SESSION_SECONDS = 86400
router = APIRouter(prefix='/api/account')
logger = logging.getLogger('uvicorn.error')


def auth_limiter():
    limiter = RateLimiter()
    limiter.limits = {'login': 10, 'register': 5}
    return limiter


AUTH_LIMITER = auth_limiter()


@contextmanager
def database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    with database() as connection:
        connection.executescript('''
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','customer')),
            status TEXT NOT NULL CHECK(status IN ('pending','approved','suspended')),
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS customer_sessions (
            token_hash TEXT PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            filename TEXT,
            created_at REAL NOT NULL,
            read_at REAL,
            UNIQUE(customer_id,filename)
        );
        CREATE INDEX IF NOT EXISTS notifications_customer ON notifications(customer_id,id);
        ''')


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=131072, r=8, p=1, maxmem=268435456, dklen=32)
    return salt.hex() + ':' + digest.hex()


def password_matches(password, encoded):
    try:
        salt, expected = encoded.split(':')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=131072, r=8, p=1, maxmem=268435456, dklen=32)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def clean_email(email):
    email = email.strip().casefold()
    if len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise HTTPException(400, 'Informe um e-mail valido.')
    return email


def create_customer(name, email, password, role='customer', status='pending'):
    name, email = name.strip(), clean_email(email)
    if not 2 <= len(name) <= 80 or not 12 <= len(password) <= 256:
        raise HTTPException(400, 'Informe seu nome e uma senha de 12 a 256 caracteres.')
    encoded = password_hash(password)
    try:
        with database() as connection:
            cursor = connection.execute('INSERT INTO customers(name,email,password_hash,role,status,created_at) VALUES(?,?,?,?,?,?)',
                                        (name, email, encoded, role, status, time.time()))
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'Este e-mail ja tem um cadastro. Entre com sua senha.') from None


def new_session(customer_id):
    raw = secrets.token_urlsafe(32)
    with database() as connection:
        connection.execute('DELETE FROM customer_sessions WHERE expires_at <= ?', (time.time(),))
        connection.execute('INSERT INTO customer_sessions VALUES(?,?,?)',
                           (hashlib.sha256(raw.encode()).hexdigest(), customer_id, time.time() + SESSION_SECONDS))
    return raw


def cookie_token(scope):
    try:
        cookie = SimpleCookie()
        cookie.load(Headers(scope=scope).get('cookie', '')[:8192])
        return cookie[COOKIE].value if COOKIE in cookie else ''
    except CookieError:
        return ''


def session_customer(scope):
    raw = cookie_token(scope)
    if not raw or len(raw) > 128:
        return None
    with database() as connection:
        row = connection.execute('''SELECT c.id,c.name,c.email,c.role,c.status FROM customers c
            JOIN customer_sessions s ON s.customer_id=c.id
            WHERE s.token_hash=? AND s.expires_at>?''', (hashlib.sha256(raw.encode()).hexdigest(), time.time())).fetchone()
    return dict(row) if row else None


def session_response(customer_id):
    response = JSONResponse({'status': 'success'})
    response.set_cookie(COOKIE, new_session(customer_id), max_age=SESSION_SECONDS,
                        secure=True, httponly=True, samesite='strict', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return response


def expected_origin(request):
    scheme = request.url.scheme
    if request.client and request.client.host in {'127.0.0.1', '::1'}:
        forwarded = request.headers.get('x-forwarded-proto')
        if forwarded in {'http', 'https'}:
            scheme = forwarded
    return f'{scheme}://{request.url.netloc}'


def request_ip(request):
    address = request.client.host if request.client else 'unknown'
    if address in {'127.0.0.1', '::1'}:
        candidate = request.headers.get('x-real-ip', '')
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return address


def customer_directory(default):
    customer = CURRENT_CUSTOMER.get()
    if not customer or customer['role'] == 'admin':
        return Path(default)
    root = CUSTOMER_ROOT / str(int(customer['id']))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def customer_database(default):
    customer = CURRENT_CUSTOMER.get()
    if not customer or customer['role'] == 'admin':
        return default
    return str(customer_directory(Path(default).parent) / Path(default).name)


def customer_processed(default):
    customer = CURRENT_CUSTOMER.get()
    if not customer or customer['role'] == 'admin':
        return Path(default)
    root = customer_directory(Path(default).parent) / 'processed'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def require_customer(request, admin=False):
    customer = getattr(request.state, 'customer', None)
    if not customer:
        raise HTTPException(401, 'Entre na sua conta.')
    if admin and customer['role'] != 'admin':
        raise HTTPException(403, 'Acesso exclusivo da administracao.')
    return customer


def notify(customer_id, title, filename=None):
    with database() as connection:
        connection.execute('INSERT OR IGNORE INTO notifications(customer_id,title,filename,created_at) VALUES(?,?,?,?)',
                           (customer_id, title, filename, time.time()))


class Registration(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: str = Field(max_length=254)
    password: str = Field(min_length=12, max_length=256)


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=256)


class ClientStatus(BaseModel):
    status: Literal['approved', 'suspended']


def limit_auth(request, group, email):
    identity = hashlib.sha256(email.strip().casefold().encode()).hexdigest()
    retry = AUTH_LIMITER.check('account:' + identity, group)
    if not retry:
        retry = AUTH_LIMITER.check('ip:' + request_ip(request), group)
    if retry:
        raise HTTPException(429, 'Muitas tentativas. Aguarde antes de tentar novamente.', headers={'Retry-After': str(retry)})


@router.post('/register')
def register(fields: Registration, request: Request):
    limit_auth(request, 'register', fields.email)
    customer_id = create_customer(fields.name, fields.email, fields.password)
    with database() as connection:
        admins = connection.execute("SELECT id FROM customers WHERE role='admin'").fetchall()
    for admin in admins:
        notify(admin['id'], 'Novo cadastro aguardando liberacao: ' + fields.name.strip())
    return session_response(customer_id)


@router.post('/login')
def login(fields: Credentials, request: Request):
    limit_auth(request, 'login', fields.email)
    email = clean_email(fields.email)
    with database() as connection:
        row = connection.execute('SELECT * FROM customers WHERE email=?', (email,)).fetchone()
    encoded = row['password_hash'] if row else '00' * 16 + ':' + '00' * 32
    valid = password_matches(fields.password, encoded)
    if not row or not valid:
        raise HTTPException(401, 'E-mail ou senha incorretos.')
    if row['status'] == 'suspended':
        raise HTTPException(403, 'Acesso suspenso. Entre em contato com a administracao.')
    return session_response(row['id'])


@router.post('/logout')
def logout(request: Request):
    raw = cookie_token(request.scope)
    with database() as connection:
        connection.execute('DELETE FROM customer_sessions WHERE token_hash=?', (hashlib.sha256(raw.encode()).hexdigest(),))
    response = JSONResponse({'status': 'success'})
    response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='strict')
    return response


@router.get('/me')
def me(request: Request):
    return JSONResponse(require_customer(request), headers={'Cache-Control': 'no-store'})


@router.get('/clients')
def clients(request: Request):
    require_customer(request, admin=True)
    with database() as connection:
        rows = connection.execute("SELECT id,name,email,status,created_at FROM customers WHERE role='customer' ORDER BY created_at DESC").fetchall()
    return {'clients': [dict(row) for row in rows]}


@router.post('/clients/{customer_id}/status')
def change_status(customer_id: int, fields: ClientStatus, request: Request):
    require_customer(request, admin=True)
    with database() as connection:
        row = connection.execute("SELECT id,status FROM customers WHERE id=? AND role='customer'", (customer_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Cliente nao encontrado.')
        connection.execute('UPDATE customers SET status=? WHERE id=?', (fields.status, customer_id))
        if fields.status == 'suspended':
            connection.execute('DELETE FROM customer_sessions WHERE customer_id=?', (customer_id,))
    if row['status'] != fields.status:
        notify(customer_id, 'Seu acesso foi liberado. Explore as ferramentas.' if fields.status == 'approved' else 'Seu acesso foi suspenso. Fale com a administracao.')
    return {'status': fields.status}


@router.get('/notifications')
def notifications(request: Request):
    customer = require_customer(request)
    with database() as connection:
        unread = connection.execute('SELECT COUNT(*) FROM notifications WHERE customer_id=? AND read_at IS NULL', (customer['id'],)).fetchone()[0]
        rows = connection.execute('SELECT id,title,filename,created_at,read_at FROM notifications WHERE customer_id=? ORDER BY id DESC LIMIT 30', (customer['id'],)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item['download_url'] = '/api/download/' + quote(item.pop('filename')) if row['filename'] else None
        items.append(item)
    return JSONResponse({'notifications': items, 'unread_count': unread}, headers={'Cache-Control': 'no-store'})


@router.post('/notifications/read')
def mark_read(request: Request):
    customer = require_customer(request)
    with database() as connection:
        connection.execute('UPDATE notifications SET read_at=? WHERE customer_id=? AND read_at IS NULL', (time.time(), customer['id']))
    return {'status': 'success'}


class CustomerAccessMiddleware:
    def __init__(self, app, initialize_data, data_database, data_processed):
        self.app = app
        self.initialize_data = initialize_data
        self.data_database = data_database
        self.data_processed = data_processed

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request = Request(scope)
        path = scope['path'].rstrip('/') or '/'
        if scope['method'] not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            if origin and origin != expected_origin(request):
                return await JSONResponse({'detail': 'Inicie esta operacao pelo StealthForge.'}, status_code=403)(scope, receive, send)
        customer = session_customer(scope)
        # Existing administrative integrations can continue using their master key.
        if not customer and path in ADMIN_PATHS:
            master = os.environ.get('ADMIN_API_KEY', '').strip()
            authorization = request.headers.get('authorization', '').split(None, 1)
            bearer = authorization[1] if len(authorization) == 2 and authorization[0].lower() == 'bearer' else ''
            candidates = (bearer, request.headers.get('x-admin-key', ''))
            if master and any(hmac.compare_digest(value.encode(), master.encode()) for value in candidates if value):
                customer = {'id': 0, 'name': 'Administracao', 'email': '', 'role': 'admin', 'status': 'approved'}
        public_api = {'/api/account/register', '/api/account/login', '/api/account/logout'}
        protected_page = path in {'/', '/studio.html', '/clients.html'}
        protected_api = path.startswith('/api/') and path not in public_api
        if not customer and (protected_page or protected_api):
            response = RedirectResponse('/login.html?next=' + quote(path), status_code=303) if protected_page else JSONResponse({'detail': 'Entre na sua conta.'}, status_code=401)
            return await response(scope, receive, send)
        if customer and customer['status'] == 'suspended' and (protected_page or protected_api):
            return await JSONResponse({'detail': 'Acesso suspenso.'}, status_code=403)(scope, receive, send)
        if customer and (path == '/clients.html' or path in ADMIN_PATHS or path.startswith('/api/account/clients')) and customer['role'] != 'admin':
            return await JSONResponse({'detail': 'Acesso exclusivo da administracao.'}, status_code=403)(scope, receive, send)
        preview_api = {'/api/stats', '/api/studio/config', '/api/account/me', '/api/account/notifications', '/api/account/notifications/read'}
        if customer and customer['status'] == 'pending' and protected_api and path not in preview_api:
            return await JSONResponse({'detail': 'Seu cadastro aguarda liberacao da administracao.', 'code': 'access_pending'}, status_code=403)(scope, receive, send)
        scope.setdefault('state', {})['customer'] = customer
        token = CURRENT_CUSTOMER.set(customer)
        status = 0
        is_json = False
        payload = bytearray()

        async def notified_send(message):
            nonlocal status, is_json
            if message['type'] == 'http.response.start':
                status = message['status']
                if customer and (protected_api or protected_page):
                    MutableHeaders(scope=message)['Cache-Control'] = 'private, no-store'
                is_json = 'application/json' in Headers(raw=message['headers']).get('content-type', '')
            if message['type'] == 'http.response.body' and customer and customer['id'] and is_json and status == 200 and scope['method'] == 'POST' and not path.startswith('/api/account/'):
                if len(payload) + len(message.get('body', b'')) <= 131072:
                    payload.extend(message.get('body', b''))
                if not message.get('more_body'):
                    try:
                        result = json.loads(payload)
                        filename = result.get('filename') if isinstance(result, dict) else None
                        if filename and isinstance(filename, str) and Path(filename).name == filename and (self.data_processed() / filename).is_file():
                            notify(customer['id'], 'Seu arquivo esta pronto para baixar.', filename)
                    except (ValueError, OSError, sqlite3.Error):
                        logger.exception('Nao foi possivel registrar a notificacao de conclusao.')
            await send(message)

        try:
            if customer and customer['role'] != 'admin' and not Path(self.data_database()).is_file():
                self.initialize_data()
            await self.app(scope, receive, notified_send)
        finally:
            CURRENT_CUSTOMER.reset(token)
