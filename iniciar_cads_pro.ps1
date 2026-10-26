#Requires -Version 5.1
# Inicia o CADS com segredo apenas no ambiente deste processo e de seus filhos.
$ErrorActionPreference = 'Stop'
$cadsExitCode = 1
$cadsOriginalLocation = Get-Location
$adminSecret = $null
$plainKey = $null

function Read-CadsRateLimit {
    param([string]$Label, [int]$Default)
    $value = Read-Host "$Label (Enter = $Default)"
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default.ToString() }
    $number = 0
    if ($value -notmatch '^[1-9][0-9]*$' -or -not [int]::TryParse($value, [ref]$number)) {
        throw 'O limite deve ser um inteiro entre 1 e 2147483647.'
    }
    return $number.ToString()
}

try {
    Write-Host '=== CADS Pro - Inicialização Segura ===' -ForegroundColor Cyan
    Set-Location -LiteralPath $PSScriptRoot
    $cadsPython = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    # Verifica o módulo no mesmo Python que executará o servidor, inclusive em venv.
    & $cadsPython -c 'import sys, uvicorn, fastapi, multipart, PIL, imageio_ffmpeg; assert sys.version_info >= (3, 10)' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Use Python 3.10+ e instale as dependências: python -m pip install -r requirements.txt' }
    & $cadsPython -m uvicorn --version
    if ($LASTEXITCODE -ne 0) { throw 'Uvicorn não está disponível neste Python.' }
    $cadsLogLevel = if ($env:LOG_LEVEL) { $env:LOG_LEVEL.ToLowerInvariant() } else { 'info' }
    if ($cadsLogLevel -notin @('critical', 'error', 'warning', 'info', 'debug')) {
        throw 'LOG_LEVEL inválido. Use critical, error, warning, info ou debug.'
    }

    $adminSecret = Read-Host 'Digite a ADMIN_API_KEY (mínimo 32 caracteres, sem espaços)' -AsSecureString
    $plainKey = [System.Net.NetworkCredential]::new('', $adminSecret).Password
    if ($plainKey.Length -lt 32 -or $plainKey -match '\s') {
        throw 'A chave deve ter ao menos 32 caracteres e não pode conter espaços ou quebras de linha.'
    }
    $env:ADMIN_API_KEY = $plainKey
    $plainKey = $null
    $adminSecret.Dispose()
    $adminSecret = $null
    $env:RATE_LIMIT_PUBLIC = Read-CadsRateLimit 'Limite público por minuto' 60
    $env:RATE_LIMIT_PROCESSING = Read-CadsRateLimit 'Limite de processamento por minuto' 10
    $env:RATE_LIMIT_ADMIN = Read-CadsRateLimit 'Limite administrativo por minuto' 5

    Write-Host 'Iniciando em http://127.0.0.1:8888/ — Ctrl+C encerra o servidor.' -ForegroundColor Green
    # Em primeiro plano, o Uvicorn recebe Ctrl+C diretamente e faz shutdown gracioso.
    & $cadsPython -m uvicorn server.main:app --host 127.0.0.1 --port 8888 --reload --no-server-header --no-proxy-headers --log-level $cadsLogLevel
    $cadsExitCode = $LASTEXITCODE
}
catch {
    Write-Host ("ERRO: " + $_.Exception.Message) -ForegroundColor Red
}
finally {
    # finally também executa quando o usuário interrompe o script com Ctrl+C.
    foreach ($name in @('ADMIN_API_KEY', 'RATE_LIMIT_PUBLIC', 'RATE_LIMIT_PROCESSING', 'RATE_LIMIT_ADMIN')) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    $plainKey = $null
    if ($null -ne $adminSecret) { $adminSecret.Dispose() }
    Set-Location -LiteralPath $cadsOriginalLocation.Path
    Write-Host 'Inicialização finalizada. Variáveis da sessão removidas.' -ForegroundColor Yellow
}
exit $cadsExitCode
