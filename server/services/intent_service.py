"""Consulta assíncrona a uma fonte externa configurada no servidor."""
import asyncio
import ipaddress
import json
import os
import socket
import time
from collections import deque
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError
from server.schemas.leads import Lead

CACHE_TTL = 3600
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
# shortcut: cache e limite por processo; compartilhar estado antes de usar vários workers.
_cache: dict[tuple[str, str], tuple[float, list[Lead]]] = {}
_attempts: deque[float] = deque()
_lock = asyncio.Lock()


class IntentServiceError(Exception):
    """Falha segura para apresentar ao cliente, sem credenciais do provedor."""
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _public_target(url: str) -> tuple[httpx.URL, str, str]:
    """Valida o DNS e fixa o IP público, preservando Host e o nome do certificado."""
    try:
        target = httpx.URL(url)
        hostname = target.host
        try:
            addresses = [ipaddress.ip_address(hostname)]
        except ValueError:
            resolved = await asyncio.to_thread(socket.getaddrinfo, hostname, target.port or 443,
                                               type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(entry[4][0]) for entry in resolved]
        if not addresses or any(not address.is_global or address.is_multicast for address in addresses):
            raise IntentServiceError('A fonte externa deve usar apenas endereços IP públicos.', 503)
        return target.copy_with(host=str(addresses[0])), hostname, target.netloc.decode('ascii')
    except (httpx.InvalidURL, OSError, ValueError):
        raise IntentServiceError('Não foi possível validar o endereço público da fonte externa.', 503) from None


async def fetch_external_intent() -> list[Lead]:
    """Busca até três vezes, respeitando cache e 100 chamadas/hora por processo."""
    url, key = os.environ.get('EXTERNAL_API_URL', '').strip(), os.environ.get('EXTERNAL_API_KEY', '').strip()
    try:
        target = urlsplit(url)
        valid = target.scheme == 'https' and bool(target.hostname) and not target.username and not target.password
    except ValueError:
        valid = False
    if not valid or not key:
        raise IntentServiceError('Configure EXTERNAL_API_URL (HTTPS) e EXTERNAL_API_KEY no servidor.', 503)
    cache_key = (url, key)
    async with _lock:
        cached = _cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return deepcopy(cached[1])
        _cache.clear()
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            for attempt in range(3):
                now = time.monotonic()
                while _attempts and _attempts[0] <= now - 3600:
                    _attempts.popleft()
                if len(_attempts) >= 100:
                    raise IntentServiceError('Limite da fonte externa: 100 chamadas por hora.', 429)
                target_url, hostname, host_header = await _public_target(url)
                _attempts.append(now)
                try:
                    async with client.stream('GET', target_url,
                                             headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json', 'Host': host_header},
                                             extensions={'sni_hostname': hostname}) as response:
                        if response.status_code == 429 or response.status_code >= 500:
                            raise IntentServiceError('Fonte externa temporariamente indisponível.')
                        if not 200 <= response.status_code < 300:
                            raise IntentServiceError('A fonte externa recusou a configuração.', 503)
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_RESPONSE_BYTES:
                                raise IntentServiceError('Resposta externa excede 5 MB.', 503)
                    payload: Any = json.loads(body)
                    rows = payload if isinstance(payload, list) else payload.get('leads') if isinstance(payload, dict) else None
                    if not isinstance(rows, list) or len(rows) > 10000:
                        raise IntentServiceError('A fonte deve retornar um array ou {leads: []}, com até 10 mil itens.', 503)
                    leads = [Lead.model_validate({**row, 'source': row.get('source', 'clay')}) for row in rows]
                    _cache[cache_key] = (time.monotonic() + CACHE_TTL, leads)
                    return deepcopy(leads)
                except IntentServiceError as error:
                    if error.status_code == 503 or attempt == 2:
                        raise
                except (httpx.HTTPError, TimeoutError):
                    if attempt == 2:
                        raise IntentServiceError('Não foi possível consultar a fonte externa.') from None
                except (ValueError, TypeError, AttributeError, ValidationError):
                    raise IntentServiceError('A fonte externa retornou leads inválidos.', 503) from None
                await asyncio.sleep(0.25 * 2 ** attempt)
    raise IntentServiceError('Não foi possível consultar a fonte externa.')
