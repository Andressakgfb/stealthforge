#!/usr/bin/env bash
# Inicialização interativa em Bash 4+. Para produção contínua, use systemd.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
cads_pid=''

cleanup() {
    unset ADMIN_API_KEY RATE_LIMIT_PUBLIC RATE_LIMIT_PROCESSING RATE_LIMIT_ADMIN cads_key
    printf '\nInicialização finalizada. Variáveis da sessão removidas.\n'
}

stop_server() {
    local status=$1
    trap '' INT TERM
    # Encaminha o sinal ao supervisor Uvicorn, que encerra seus workers.
    if [[ -n "$cads_pid" ]] && kill -0 "$cads_pid" 2>/dev/null; then
        kill -TERM "$cads_pid" 2>/dev/null || true
        wait "$cads_pid" || true
    fi
    exit "$status"
}

trap cleanup EXIT
trap 'stop_server 130' INT
trap 'stop_server 143' TERM

read_rate_limit() {
    local name=$1 label=$2 default=$3 value=''
    read -r -p "$label (Enter = $default): " value
    value=${value:-$default}
    if [[ ! "$value" =~ ^[1-9][0-9]*$ ]] || (( ${#value} > 10 )); then
        printf 'ERRO: o limite deve ser um inteiro positivo.\n' >&2
        exit 1
    fi
    if (( 10#$value > 2147483647 )); then
        printf 'ERRO: o limite máximo é 2147483647.\n' >&2
        exit 1
    fi
    printf -v "$name" '%s' "$value"
    export "$name"
}

printf '=== CADS Pro - Inicialização Segura ===\n'
command -v python3 >/dev/null 2>&1 || { printf 'ERRO: instale Python 3.10+.\n' >&2; exit 1; }
python3 -c 'import sys, uvicorn, fastapi, multipart, PIL, imageio_ffmpeg; assert sys.version_info >= (3, 10)' 2>/dev/null || {
    printf 'ERRO: use Python 3.10+ e execute python3 -m pip install -r requirements.txt no venv.\n' >&2
    exit 1
}
python3 -m uvicorn --version
cads_log_level=${LOG_LEVEL:-info}
case "$cads_log_level" in critical|error|warning|info|debug) ;; *) printf 'ERRO: LOG_LEVEL inválido.\n' >&2; exit 1 ;; esac

IFS= read -r -s -p 'Digite a ADMIN_API_KEY (mínimo 32 caracteres, sem espaços): ' cads_key
printf '\n'
if (( ${#cads_key} < 32 )) || [[ "$cads_key" =~ [[:space:]] ]]; then
    printf 'ERRO: a chave deve ter ao menos 32 caracteres, sem espaços ou quebras de linha.\n' >&2
    exit 1
fi
export ADMIN_API_KEY="$cads_key"
unset cads_key
read_rate_limit RATE_LIMIT_PUBLIC 'Limite público por minuto' 60
read_rate_limit RATE_LIMIT_PROCESSING 'Limite de processamento por minuto' 10
read_rate_limit RATE_LIMIT_ADMIN 'Limite administrativo por minuto' 5

printf 'Iniciando em http://127.0.0.1:8888/ — Ctrl+C encerra o servidor.\n'
# O trap encaminha SIGTERM/SIGINT mesmo quando o shell está aguardando o filho.
python3 -m uvicorn server.main:app --host 127.0.0.1 --port 8888 --reload --no-server-header --no-proxy-headers --log-level "$cads_log_level" &
cads_pid=$!
if wait "$cads_pid"; then cads_status=0; else cads_status=$?; fi
cads_pid=''
exit "$cads_status"
