#!/usr/bin/env bash
# Gera 32 bytes criptográficos; imprime 64 caracteres hex sem salvar arquivos.
set -Eeuo pipefail
command -v openssl >/dev/null 2>&1 || { printf 'ERRO: instale openssl.\n' >&2; exit 1; }
key=$(openssl rand -hex 32)
trap 'unset key' EXIT
[[ "$key" =~ ^[0-9a-f]{64}$ ]] || { printf 'ERRO: não foi possível gerar uma chave válida.\n' >&2; exit 1; }
printf '=== Chave administrativa gerada ===\n%s\n' "$key"
printf 'Guarde em um gerenciador de senhas e informe ao inicializador quando solicitado.\n'
printf 'Não execute este gerador em terminais com gravação ou transcrição de sessão.\n'
