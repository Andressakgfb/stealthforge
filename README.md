# StealthForge

Aplicativo FastAPI para preparar imagens, videos e textos e usar o Estudio Criativo IA.

## Portal de clientes

O portal exige login. Clientes podem se cadastrar e conhecer a interface com acesso
pendente. A administracao aprova ou suspende contas em `/clients.html`. Processamento
e geracao exigem aprovacao; arquivos, estatisticas e notificacoes sao individuais.
O sininho avisa sobre cadastros, aprovacao e arquivos prontos.

O login da conta, a chave administrativa da API e a chave do provedor de IA sao
credenciais diferentes. A conta administrativa deve ser provisionada no ambiente
pelo operador; ela nao vem com senha padrao neste repositorio. As sessoes usam
cookies Secure, portanto o portal deve ser acessado por HTTPS.

Este repositorio contem codigo, testes e exemplos de configuracao. Dados de clientes,
SQLite, arquivos gerados, chaves e `.env` ficam apenas no ambiente de execucao.
O nome tecnico `cads-pro` permanece nos caminhos e scripts da instalacao existente.

## Instalacao
Requer **Python 3.10+**, PowerShell 5.1+ no Windows ou Bash 4+ no Linux.

## Configuração inicial

Na pasta do projeto, instale as dependências no Python que utilizará:

```powershell
python -m pip install -r requirements.txt
```

No Windows, gere a chave e guarde-a em um gerenciador de senhas:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\generate_admin_key.ps1
```

Execute `INICIAR_CADS_PRO.bat` ou:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\iniciar_cads_pro.ps1
```

O atalho existente na Área de Trabalho também foi atualizado para chamar o
inicializador da instalação. Se o Windows bloquear um arquivo baixado, confira
sua origem e use `Unblock-File .\iniciar_cads_pro.ps1`. O batch usa Bypass somente
no processo filho; não muda a política global do PowerShell.

Informe a chave de pelo menos 32 caracteres quando solicitado: ela não aparece
durante a digitação. Pressione Enter para aceitar os limites 60/10/5. Valores
inválidos interrompem a inicialização com uma mensagem e código de erro.
Quando o Uvicorn confirmar que iniciou, acesse **http://127.0.0.1:8888/**.
Use a mesma chave no campo **Chave administrativa** do painel.

Não inicie dois servidores na porta 8888. Se já houver um rodando, encerre-o pelo
terminal em que foi iniciado antes de usar o novo inicializador. Os scripts não
matam servidores existentes nem substituem uma instalação silenciosamente.

No Linux, dentro de um ambiente virtual:

```bash
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
chmod +x start_cads_pro.sh generate_admin_key.sh
./generate_admin_key.sh
./start_cads_pro.sh
```

Também pode gerar uma chave com `openssl rand -hex 32`. Os geradores exibem a
chave uma única vez e não criam arquivos. Não os execute em uma sessão gravada,
com transcrição de terminal ou em logs de CI. Não envie credenciais pelo chat.

## Encerramento e variáveis

Ctrl+C solicita o encerramento ao Uvicorn. PowerShell usa `try/finally`; Bash
encaminha SIGINT/SIGTERM ao processo filho e usa um trap de saída. As quatro
variáveis configuradas pelo inicializador são removidas do seu processo ao
finalizar. Um processo encerrado à força perde seu ambiente ao terminar, mas
não pode executar cleanup de código. Não execute os scripts com dot-sourcing
(`. .\iniciar_cads_pro.ps1` ou `source start_cads_pro.sh`).

Os scripts interativos **não carregam `.env`**, não salvam a chave e sempre a
solicitam. O `.env` é utilizado pelo systemd em produção. Não copie seu conteúdo
para o código ou para argumentos de linha de comando.

| Variável | Padrão | Uso |
|---|---|---|
| `ADMIN_API_KEY` | sem padrão | Obrigatória para as três operações administrativas; mínimo 32 caracteres no inicializador, sem espaços |
| `RATE_LIMIT_PUBLIC` | `60` | GET/HEAD em `/api/*`, por minuto e IP |
| `RATE_LIMIT_PROCESSING` | `10` | Demais chamadas públicas de API, por minuto e IP |
| `RATE_LIMIT_ADMIN` | `5` | As três rotas administrativas, por minuto e IP |
| `PLAN_LIMIT_DEFAULT` | `5000` | Referência mensal no painel; inteiro positivo, sem alterar limites diários das chaves |
| `LOG_LEVEL` | `info` | critical, error, warning, info ou debug; aplicado pelos scripts e pelo serviço |
| `CADS_DATA_DIR` | layout local existente | Diretório absoluto opcional para SQLite, `processed/` e leitura de registros locais |

As três rotas administrativas continuam sendo `/api/auth/create-key`,
`/api/domain/rotate` e `/api/export/google-sheets`, com Bearer ou X-Admin-Key.
Essas operacoes tambem respeitam as permissoes do portal; integracoes existentes podem usar a chave administrativa.

## VPS e operação contínua

Siga [DEPLOY.md](DEPLOY.md) para Ubuntu/Debian, usuário dedicado, `.env` privado,
systemd, permissões e Nginx com HTTPS. O serviço usa **um worker**, pois os limites
atuais são por processo. Usar quatro multiplicaria o limite efetivo; adote um
armazenamento compartilhado antes de aumentar esse número. Não há Redis neste
pacote. O serviço não usa `--reload`.

O código em `/opt/cads-pro` fica somente para leitura pelo serviço; os dados
ficam em `/var/lib/cads-pro`, criado e protegido pelo systemd. Não se permite
gravação sobre a pasta de código apenas para fazer o SQLite funcionar.

## Verificações

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
node tests/test_dashboard.js
node --check public/studio.js
```

Os testes de inicialização em `tests/test_launchers.py` usam processos e dados
temporários. O relatório entregue distingue execução Windows, testes no Bash
do Git e validações que exigem um VPS Linux real. Nenhum teste de inicialização
precisa imprimir a chave gerada.

O teste opcional `tests/test_login.js` exige Playwright e Chromium instalados no
ambiente de testes. Ele usa respostas simuladas e nao acessa contas reais.
