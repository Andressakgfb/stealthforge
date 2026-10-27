"""Importação de CSV do PhantomBuster sem dependências adicionais."""
import csv
import io
import re
import unicodedata
from typing import Any

from pydantic import ValidationError
from server.schemas.leads import Lead

MAX_CSV_BYTES = 5 * 1024 * 1024


def _header(value: str) -> str:
    """Normaliza cabeçalhos exportados por fontes distintas."""
    plain = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]', '', plain)


def parse_phantom_csv(file_content: bytes) -> tuple[list[Lead], list[str]]:
    """Retorna leads válidos e erros por linha, preservando os bytes de origem."""
    if len(file_content) > MAX_CSV_BYTES:
        return [], ['O arquivo deve ter até 5 MB.']
    try:
        text = file_content.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = file_content.decode('latin-1')
    if not text.strip():
        return [], ['CSV vazio.']
    try:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=',;\t').delimiter
        except csv.Error:
            first_line = text.splitlines()[0]
            delimiter = max(',;\t', key=first_line.count)
        reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True)
        aliases = {'fullname': 'name', 'nome': 'name', 'nomecompleto': 'name', 'emailaddress': 'email',
                   'professionalemail': 'email', 'companyname': 'company', 'empresa': 'company',
                   'jobtitle': 'role', 'title': 'role', 'cargo': 'role', 'setor': 'industry',
                   'firmographicsindustry': 'industry', 'firmographicscompanysize': 'companysize',
                   'firmographicslocation': 'location', 'firmographicsrevenue': 'revenue',
                   'intentscore': 'score', 'intentstage': 'stage', 'intentsignals': 'signals',
                   'intentlastactivity': 'lastactivity'}
        header = next(reader)
        headers = [aliases.get(_header(value), _header(value)) for value in header]
        if not all(headers) or len(set(headers)) != len(headers):
            return [], ['Cabeçalhos vazios ou duplicados.']
        if not {'email', 'company'}.issubset(headers) or not ('name' in headers or {'firstname', 'lastname'}.issubset(headers)):
            return [], ['Colunas name, email e company são obrigatórias (ou firstName + lastName).']
        leads: list[Lead] = []
        errors: list[str] = []
        while True:
            line = reader.line_num + 1
            try:
                cells = next(reader)
            except StopIteration:
                break
            if not cells or not any(value.strip() for value in cells):
                continue
            if len(cells) != len(headers):
                errors.append(f'Linha {line}: quantidade de colunas incompatível com o cabeçalho.')
                continue
            row = dict(zip(headers, (value.strip() for value in cells)))
            try:
                score = row.get('score', '') or '0'
                if not re.fullmatch(r'\d{1,3}', score):
                    raise ValueError('score inválido')
                payload: dict[str, Any] = {
                    'name': row.get('name') or f"{row.get('firstname', '')} {row.get('lastname', '')}".strip(),
                    'email': row.get('email', ''), 'company': row.get('company', ''), 'role': row.get('role', ''),
                    'firmographics': {key: row.get(column, '') for key, column in [('industry', 'industry'), ('company_size', 'companysize'), ('location', 'location'), ('revenue', 'revenue')]},
                    'technographics': [value.strip() for value in row.get('technographics', '').split('|') if value.strip()],
                    'intent': {'score': int(score), 'signals': [value.strip() for value in row.get('signals', '').split('|') if value.strip()],
                               'stage': row.get('stage') or 'Awareness', 'last_activity': row.get('lastactivity') or None},
                    'source': row.get('source') or 'phantom', 'opt_out': row.get('optout', '').lower() == 'true',
                }
                if row.get('createdat'):
                    payload['created_at'] = row['createdat']
                leads.append(Lead.model_validate(payload))
            except (ValidationError, ValueError):
                errors.append(f'Linha {line}: confira nome, e-mail, empresa, score, etapa, origem e datas.')
        return leads, errors
    except (csv.Error, StopIteration):
        return [], ['CSV malformado: confira cabeçalho, delimitadores e fechamento das aspas.']
