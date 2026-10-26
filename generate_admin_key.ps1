#Requires -Version 5.1
# Exibe a chave uma única vez; não cria arquivos nem altera o ambiente.
$ErrorActionPreference = 'Stop'
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $rng.GetBytes($bytes)
    $key = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    Write-Host '=== Chave administrativa gerada ===' -ForegroundColor Green
    Write-Output $key
    Write-Host 'Guarde em um gerenciador de senhas e informe ao inicializador quando solicitado.' -ForegroundColor Yellow
    Write-Host 'Não execute este gerador em terminais com gravação ou transcrição de sessão.' -ForegroundColor Yellow
}
finally {
    $rng.Dispose()
    [Array]::Clear($bytes, 0, $bytes.Length)
    $key = $null
}
