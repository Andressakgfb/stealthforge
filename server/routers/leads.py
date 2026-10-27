"""Rotas de leads vinculadas à sessão e ao SQLite do portal."""
import sqlite3
from contextlib import contextmanager
from typing import Annotated, Any, Iterator
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool
from server import accounts
from server.schemas.leads import Lead, Source
from server.services.intent_service import IntentServiceError, fetch_external_intent
from server.utils.csv_parser import MAX_CSV_BYTES, parse_phantom_csv


def require_lead_access(request: Request, response: Response,
                        consent: Annotated[str | None, Header(alias='X-LGPD-Consent')] = None) -> dict[str, Any]:
    """Respeita a sessão existente e exige consentimento explícito."""
    customer = accounts.require_customer(request)
    if customer['status'] != 'approved':
        raise HTTPException(403, 'Seu acesso ainda não foi liberado.')
    if consent != 'true':
        raise HTTPException(403, 'Autorize o uso de dados para acessar os leads.')
    response.headers['Cache-Control'] = 'private, no-store'
    return customer


router = APIRouter(prefix='/api/leads', tags=['leads'], dependencies=[Depends(require_lead_access)])
Account = Annotated[dict[str, Any], Depends(require_lead_access)]


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    """Acrescenta uma tabela privada sem alterar as tabelas de autenticação."""
    try:
        with accounts.database() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS intent_leads (
                customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                id TEXT NOT NULL,
                email TEXT NOT NULL,
                payload TEXT NOT NULL,
                opted_out INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(customer_id,id), UNIQUE(customer_id,email)
            )''')
            yield connection
    except (sqlite3.Error, OSError, ValueError):
        raise HTTPException(503, 'Armazenamento de leads indisponível. Tente novamente.') from None


def _save_leads(customer_id: int, leads: list[Lead]) -> list[Lead]:
    """Atualiza por e-mail e preserva favoritos e exclusões anteriores."""
    saved: list[Lead] = []
    with _database() as connection:
        connection.execute('BEGIN IMMEDIATE')
        for lead in leads:
            row = connection.execute('SELECT payload,opted_out FROM intent_leads WHERE customer_id=? AND email=?', (customer_id, lead.email)).fetchone()
            if row:
                if row['opted_out'] or lead.opt_out:
                    connection.execute('UPDATE intent_leads SET opted_out=1 WHERE customer_id=? AND email=?', (customer_id, lead.email))
                    continue
                previous = Lead.model_validate_json(row['payload'])
                lead = lead.model_copy(update={'id': previous.id, 'created_at': previous.created_at, 'is_favorite': previous.is_favorite})
            else:
                # A fonte não controla IDs internos nem favoritos da conta.
                lead = lead.model_copy(update={'id': uuid4(), 'is_favorite': False})
            connection.execute('''INSERT INTO intent_leads(customer_id,id,email,payload,opted_out) VALUES(?,?,?,?,?)
                ON CONFLICT(customer_id,email) DO UPDATE SET payload=excluded.payload''',
                               (customer_id, str(lead.id), lead.email, lead.model_dump_json(), int(lead.opt_out)))
            if not lead.opt_out:
                saved.append(lead)
    return saved


@router.get('', response_model=dict[str, list[Lead]])
def list_leads(account: Account, industry: str | None = Query(default=None, max_length=300),
               min_score: int = Query(default=0, ge=0, le=100), source: Source | None = None) -> dict[str, list[Lead]]:
    """Lista apenas os leads visíveis da conta autenticada."""
    with _database() as connection:
        rows = connection.execute('SELECT payload FROM intent_leads WHERE customer_id=? AND opted_out=0 ORDER BY rowid DESC', (account['id'],)).fetchall()
        leads = [Lead.model_validate_json(row['payload']) for row in rows]
    return {'leads': [lead for lead in leads if lead.intent.score >= min_score
                      and (industry is None or lead.firmographics.industry == industry)
                      and (source is None or lead.source == source)]}


@router.get('/stats')
def lead_stats(account: Account) -> dict[str, Any]:
    """Agrega somente os leads visíveis da conta."""
    leads = list_leads(account, industry=None, min_score=0, source=None)['leads']
    return {'total': len(leads), 'by_stage': {stage: sum(lead.intent.stage == stage for lead in leads)
                                          for stage in ['Awareness', 'Consideration', 'Decision']},
            'avg_score': round(sum(lead.intent.score for lead in leads) / len(leads), 1) if leads else 0}


@router.post('/import', response_model=dict[str, Any])
async def import_leads(account: Account, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    """Recebe multipart, valida o CSV e persiste as linhas aceitas."""
    try:
        content = await file.read(MAX_CSV_BYTES + 1)
    except (OSError, ValueError):
        raise HTTPException(400, 'Não foi possível ler o arquivo CSV.') from None
    finally:
        await file.close()
    if len(content) > MAX_CSV_BYTES:
        raise HTTPException(413, 'O arquivo deve ter até 5 MB.')
    leads, errors = await run_in_threadpool(parse_phantom_csv, content)
    if not leads and errors:
        raise HTTPException(422, {'message': 'Nenhum lead válido no arquivo.', 'errors': errors})
    saved = await run_in_threadpool(_save_leads, account['id'], leads)
    return {'success': [lead.model_dump(mode='json') for lead in saved], 'errors': errors}


@router.post('/sync')
async def sync_leads(account: Account) -> dict[str, int]:
    """Sincroniza a fonte configurada sem modificar a autenticação."""
    try:
        leads = await fetch_external_intent()
    except IntentServiceError as error:
        raise HTTPException(error.status_code, str(error)) from None
    saved = await run_in_threadpool(_save_leads, account['id'], leads)
    return {'imported': len(saved)}


@router.patch('/{lead_id}/favorite', response_model=Lead)
def toggle_favorite(lead_id: UUID, account: Account) -> Lead:
    """Alterna o favorito em uma transação isolada por conta."""
    with _database() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT payload FROM intent_leads WHERE customer_id=? AND id=? AND opted_out=0', (account['id'], str(lead_id))).fetchone()
        if not row:
            raise HTTPException(404, 'Lead não encontrado.')
        lead = Lead.model_validate_json(row['payload'])
        lead.is_favorite = not lead.is_favorite
        connection.execute('UPDATE intent_leads SET payload=? WHERE customer_id=? AND id=?', (lead.model_dump_json(), account['id'], str(lead_id)))
    return lead
