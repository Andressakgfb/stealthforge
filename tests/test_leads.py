"""Testes isolados do módulo de leads; nenhuma API externa real é chamada."""
import asyncio
import socket
import time
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from server import accounts
from server.routers import leads as routes
from server.schemas.leads import Firmographics, Lead
from server.services import intent_service
from server.utils.csv_parser import MAX_CSV_BYTES, parse_phantom_csv

CONSENT = {'X-LGPD-Consent': 'true'}
RAW = {'name': 'Ana Teste', 'email': 'ana@example.test', 'company': 'Empresa fictícia'}
CSV = ('name,email,company,industry,score,stage,source\n'
       'Ana,ana@example.test,Acme,SaaS,85,Decision,clay\n'
       'Bruno,bruno@example.test,Acme,Fintech,40,Consideration,phantom')


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture(autouse=True)
def isolated_service(monkeypatch):
    monkeypatch.delenv('EXTERNAL_API_URL', raising=False)
    monkeypatch.delenv('EXTERNAL_API_KEY', raising=False)
    monkeypatch.setattr(intent_service, '_cache', {})
    monkeypatch.setattr(intent_service, '_attempts', intent_service.deque())
    monkeypatch.setattr(intent_service, '_lock', asyncio.Lock())
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, *args, **kwargs:
                        [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', port))])


@pytest.fixture
def customer(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, 'DATABASE_PATH', tmp_path / 'customers.sqlite3')
    accounts.initialize()
    with accounts.database() as connection:
        for identifier in [1, 2]:
            connection.execute('INSERT INTO customers(id,name,email,password_hash,role,status,created_at) VALUES(?,?,?,?,?,?,?)',
                               (identifier, 'Test Account', f'account{identifier}@example.test', 'unused-test-hash', 'customer', 'approved', time.time()))
    fake = {'id': 1, 'status': 'approved', 'role': 'customer'}
    monkeypatch.setattr(accounts, 'require_customer', Mock(side_effect=lambda request, admin=False: dict(fake)))
    return fake


@pytest.fixture
async def client(customer):
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as session:
        yield session


@pytest.fixture
def mock_provider(monkeypatch):
    original = httpx.AsyncClient
    def configure(handler):
        factory = Mock(side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
        monkeypatch.setattr(intent_service.httpx, 'AsyncClient', factory)
        return factory
    return configure


def external_config(monkeypatch, url='https://provider.example.test/leads'):
    monkeypatch.setenv('EXTERNAL_API_URL', url)
    monkeypatch.setenv('EXTERNAL_API_KEY', 'synthetic-test-key')


async def upload(client, csv=CSV):
    return await client.post('/api/leads/import', headers=CONSENT,
                             files={'file': ('leads.csv', csv.encode('utf-8'), 'text/csv')})


def test_valid_lead_and_timezone_aliases():
    lead = Lead(**RAW, favorite=True, intent={'score': 80, 'lastActivity': '2026-10-27T12:00:00'},
                firmographics={'companySize': '11-50'})
    assert lead.id.version == 4
    assert lead.is_favorite is True
    assert lead.firmographics.company_size == '11-50'
    assert lead.intent.last_activity.isoformat() == '2026-10-27T12:00:00+00:00'


def test_invalid_email():
    with pytest.raises(ValidationError):
        Lead(**{**RAW, 'email': 'invalid'})


@pytest.mark.parametrize('score', [-1, 101, True])
def test_score_out_of_bounds(score):
    with pytest.raises(ValidationError):
        Lead(**RAW, intent={'score': score})


def test_empty_firmographics():
    profile = Firmographics(industry='', company_size='', location='', revenue='')
    assert profile.model_dump() == {'industry': '', 'company_size': '', 'location': '', 'revenue': ''}


@pytest.mark.anyio
@pytest.mark.parametrize('header', [None, 'false'])
async def test_consent_required(client, header):
    response = await client.get('/api/leads', headers={} if header is None else {'X-LGPD-Consent': header})
    assert response.status_code == 403


@pytest.mark.anyio
async def test_consent_true_returns_200(client):
    response = await client.get('/api/leads', headers=CONSENT)
    assert response.status_code == 200
    assert response.json() == {'leads': []}


@pytest.mark.anyio
async def test_listing_and_each_filter(client):
    assert (await upload(client)).status_code == 200
    response = await client.get('/api/leads', headers=CONSENT)
    assert response.status_code == 200
    assert {lead['email'] for lead in response.json()['leads']} == {'ana@example.test', 'bruno@example.test'}
    for query, expected in [('source=clay', 'ana@example.test'), ('industry=Fintech', 'bruno@example.test'),
                            ('min_score=71', 'ana@example.test'), ('industry=SaaS&source=clay&min_score=80', 'ana@example.test')]:
        response = await client.get('/api/leads?' + query, headers=CONSENT)
        assert response.status_code == 200
        assert [lead['email'] for lead in response.json()['leads']] == [expected]


@pytest.mark.anyio
async def test_stats(client):
    await upload(client)
    response = await client.get('/api/leads/stats', headers=CONSENT)
    assert response.status_code == 200
    assert response.json() == {'total': 2, 'by_stage': {'Awareness': 0, 'Consideration': 1, 'Decision': 1}, 'avg_score': 62.5}


@pytest.mark.anyio
async def test_valid_csv_import(client):
    response = await upload(client)
    assert response.status_code == 200
    assert len(response.json()['success']) == 2
    assert response.json()['errors'] == []


@pytest.mark.anyio
async def test_partial_csv_errors(client):
    response = await upload(client, CSV + '\nSem email,,Acme,SaaS,0,Awareness,manual')
    assert response.status_code == 200
    assert len(response.json()['success']) == 2
    assert len(response.json()['errors']) == 1
    assert 'Linha 4' in response.json()['errors'][0]


@pytest.mark.anyio
async def test_csv_over_5mb(client):
    response = await client.post('/api/leads/import', headers=CONSENT,
                                 files={'file': ('large.csv', b'x' * (MAX_CSV_BYTES + 1), 'text/csv')})
    assert response.status_code == 413


@pytest.mark.anyio
async def test_sync_uses_mocked_service(client, monkeypatch):
    external_config(monkeypatch)
    mocked = AsyncMock(return_value=[Lead(**RAW)])
    monkeypatch.setattr(intent_service, 'fetch_external_intent', mocked)
    response = await client.post('/api/leads/sync', headers=CONSENT)
    assert response.status_code == 200
    assert response.json() == {'imported': 1}
    mocked.assert_awaited_once()


@pytest.mark.anyio
async def test_sync_with_httpx_mock_and_upsert(client, monkeypatch, mock_provider):
    external_config(monkeypatch)
    requests = []
    def provider(request):
        requests.append(request)
        return httpx.Response(200, json={'leads': [RAW]})
    mock_provider(provider)
    for _ in range(2):
        response = await client.post('/api/leads/sync', headers=CONSENT)
        assert response.status_code == 200
        assert response.json() == {'imported': 1}
    assert len(requests) == 1
    assert requests[0].headers['authorization'] == 'Bearer synthetic-test-key'
    assert (await client.get('/api/leads/stats', headers=CONSENT)).json()['total'] == 1


@pytest.mark.anyio
async def test_sync_missing_environment(client):
    response = await client.post('/api/leads/sync', headers=CONSENT)
    assert response.status_code == 503


@pytest.mark.anyio
async def test_toggle_favorite_and_upsert_preserves_it(client):
    lead = (await upload(client)).json()['success'][0]
    path = f"/api/leads/{lead['id']}/favorite"
    first = await client.patch(path, headers=CONSENT)
    assert first.status_code == 200
    assert first.json()['is_favorite'] is True
    repeated = (await upload(client)).json()['success'][0]
    assert repeated['id'] == lead['id']
    assert repeated['is_favorite'] is True
    second = await client.patch(path, headers=CONSENT)
    assert second.status_code == 200
    assert second.json()['is_favorite'] is False


@pytest.mark.anyio
async def test_missing_favorite_returns_404(client):
    response = await client.patch(f'/api/leads/{uuid4()}/favorite', headers=CONSENT)
    assert response.status_code == 404


@pytest.mark.anyio
async def test_customer_isolation(client, customer):
    lead = (await upload(client)).json()['success'][0]
    customer['id'] = 2
    assert (await client.get('/api/leads', headers=CONSENT)).json() == {'leads': []}
    assert (await client.get('/api/leads/stats', headers=CONSENT)).json()['total'] == 0
    assert (await client.patch(f"/api/leads/{lead['id']}/favorite", headers=CONSENT)).status_code == 404
    own = (await upload(client, 'name,email,company\nOutra,ana@example.test,Outra Empresa')).json()['success'][0]
    assert own['id'] != lead['id']
    customer['id'] = 1
    assert len((await client.get('/api/leads', headers=CONSENT)).json()['leads']) == 2


@pytest.mark.anyio
async def test_optout_blocks_reimport(client):
    await upload(client, 'name,email,company\nAna,ana@example.test,Acme')
    await upload(client, 'name,email,company,optout\nAna,ana@example.test,Acme,true')
    await upload(client, 'name,email,company\nAna,ana@example.test,Acme')
    assert (await client.get('/api/leads', headers=CONSENT)).json() == {'leads': []}


@pytest.mark.parametrize('delimiter', [',', ';', '\t'])
def test_csv_encodings_quotes_and_multiline(delimiter):
    content = delimiter.join(['name', 'email', 'company', 'role']) + '\n' + delimiter.join(['"Ana, Teste"', 'a@example.test', '"Empresa ""Teste"""', '"Diretora\nComercial"'])
    leads, errors = parse_phantom_csv(('\ufeff' + content).encode())
    assert errors == []
    assert leads[0].company == 'Empresa "Teste"'
    assert leads[0].role == 'Diretora\nComercial'
    leads, errors = parse_phantom_csv('name;email;company\nJoão;a@example.test;Ação'.encode('latin-1'))
    assert errors == []
    assert leads[0].name == 'João'


@pytest.mark.anyio
async def test_external_cache_retry_expiry_and_rate_limit(monkeypatch, mock_provider):
    external_config(monkeypatch)
    requests = []
    def provider(request):
        requests.append(request)
        return httpx.Response(503) if len(requests) < 3 else httpx.Response(200, json={'leads': [RAW]})
    mock_provider(provider)
    sleep = AsyncMock()
    monkeypatch.setattr(intent_service.asyncio, 'sleep', sleep)
    first, second = await asyncio.gather(intent_service.fetch_external_intent(), intent_service.fetch_external_intent())
    assert len(requests) == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.5]
    assert first[0].id == second[0].id
    first[0].name = 'Alterado'
    assert (await intent_service.fetch_external_intent())[0].name == RAW['name']
    for key, (_, value) in list(intent_service._cache.items()):
        intent_service._cache[key] = (0, value)
    await intent_service.fetch_external_intent()
    assert len(requests) == 4
    intent_service._cache.clear()
    intent_service._attempts.extend([time.monotonic()] * 100)
    with pytest.raises(intent_service.IntentServiceError) as caught:
        await intent_service.fetch_external_intent()
    assert caught.value.status_code == 429
    assert len(requests) == 4


@pytest.mark.anyio
@pytest.mark.parametrize('url', ['http://8.8.8.8/leads', 'https://127.0.0.1/leads', 'https://[::1]/leads',
                                 'https://169.254.169.254/latest/meta-data', 'https://10.0.0.1/leads',
                                 'https://user:pass@provider.example.test/leads'])
async def test_external_blocks_unsafe_targets(url, monkeypatch, mock_provider):
    external_config(monkeypatch, url)
    requests = []
    def provider(request):
        requests.append(request)
        return httpx.Response(200, json={'leads': [RAW]})
    mock_provider(provider)
    with pytest.raises(intent_service.IntentServiceError) as caught:
        await intent_service.fetch_external_intent()
    assert caught.value.status_code == 503
    assert requests == []


@pytest.mark.anyio
async def test_external_blocks_private_dns(monkeypatch, mock_provider):
    external_config(monkeypatch)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, *args, **kwargs:
                        [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', port))])
    requests = []
    mock_provider(lambda request: requests.append(request) or httpx.Response(200, json={'leads': [RAW]}))
    with pytest.raises(intent_service.IntentServiceError) as caught:
        await intent_service.fetch_external_intent()
    assert caught.value.status_code == 503
    assert requests == []


@pytest.mark.anyio
async def test_external_pins_public_ip_preserves_tls_host_and_denies_redirects(monkeypatch, mock_provider):
    external_config(monkeypatch)
    requests = []
    def provider(request):
        requests.append(request)
        return httpx.Response(302, headers={'Location': 'https://127.0.0.1/private'})
    factory = mock_provider(provider)
    with pytest.raises(intent_service.IntentServiceError):
        await intent_service.fetch_external_intent()
    assert len(requests) == 1
    assert requests[0].url.host == '8.8.8.8'
    assert requests[0].headers['host'] == 'provider.example.test'
    assert requests[0].extensions['sni_hostname'] == 'provider.example.test'
    assert factory.call_args.kwargs['follow_redirects'] is False
    assert factory.call_args.kwargs['trust_env'] is False
