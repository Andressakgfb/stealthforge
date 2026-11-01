# Auditoria técnica e funcional — StealthForge

Data: 26/10/2026 (America/Sao_Paulo). Aplicação: https://stealthforge.com.br/.
Código analisado: `C:\Users\Administrator\.gemini\antigravity-ide\scratch\cads-app`.

## A. Resumo executivo

**Há uma falha crítica confirmada localmente: clientes aprovados conseguem ler arquivos acessíveis ao processo do servidor através do clonador.** A prova leu exclusivamente um arquivo temporário contendo um marcador sintético; não foram acessados segredos nem dados de clientes. A exposição em produção não foi explorada e depende da correspondência entre o código local e o implantado.

Foram aplicadas duas correções pequenas: o campo de nome oculto deixou de impedir o login e o resultado da análise de conteúdo passou a exibir texto escapado, sem interpretá-lo como HTML. Nenhuma alteração foi publicada. Não houve push, deploy, alteração de credenciais ou acesso aos bancos reais existentes no projeto.

Os controles de contas apresentaram bons resultados: cadastro pendente, aprovação, isolamento entre clientes, logout, expiração, suspensão, restrição administrativa e rate limiting passaram nos 11 testes de contas executados isoladamente. Também passaram sete testes funcionais e nove cenários JavaScript. Isso não equivale a uma validação de todos os fluxos em produção.

A primeira suíte executou 60 testes em 42,874 s: 42 testes sem falhas e 18 testes afetados, totalizando uma falha e 23 registros de erro, incluindo erros adicionais de limpeza. Os rastros apontaram `database or disk is full` e arquivos SQLite abertos durante a limpeza. Os 18 testes afetados tiveram resultados aprovados em repetições isoladas (11 contas, sete funcionais e um teste de configuração, com um teste de contas já aprovado anteriormente). **Não existe uma execução integral verde nesta auditoria:** uma nova tentativa completa foi interrompida por falta de espaço.

O computador tinha aproximadamente 20–22 MB livres em medições pontuais, com oscilação durante o trabalho. Esse problema é do ambiente local observado; não foi medido o disco do servidor de produção. Nenhum arquivo do usuário foi apagado para liberar espaço.

### Arquitetura identificada

| Camada | Implementação e dependências |
|---|---|
| Interface | HTML, CSS e JavaScript sem framework/build identificado. `public/index.html` contém o painel e scripts embutidos; `studio.html/studio.js`, `login.html/login.js`, `clients.html/clients.js`, `session.js`; ícones Lucide locais. |
| Servidor | Python 3.13 local, FastAPI 0.143.0, Uvicorn 0.53.0, Starlette 1.6.0 instalada transitivamente. `server/main.py` reúne rotas e processamento. |
| Contas | `server/accounts.py`: SQLite `customers.sqlite3`, senha com scrypt e salt, sessões com hash SHA-256 do token, cookie `__Host-sf_session`, perfis admin/customer e estados pending/approved/suspended. |
| Dados por cliente | ContextVar identifica o cliente; diretórios e bancos separados em `customers/<id>/`; administradores usam o diretório compartilhado padrão. Não há evidência de que todos os administradores precisem de isolamento entre si. |
| Processamento | Pillow 12.3.0; imageio-ffmpeg 0.6.0 e FFmpeg; arquivos em `processed/`; histórico reconstruído do disco e registros SQLite de itens ocultos. |
| IA e integrações | `product_video.py`: Google, deAPI e fal.ai via urllib; chave informada pelo usuário ou ambiente. Clonador HTTP, webhooks Telegram/Discord e exportação CSV. Não foram feitas chamadas pagas nem enviadas mensagens. |
| Segurança | Middleware de contas, origem, limitação de requisições e cabeçalhos; chave administrativa adicional em três rotas legadas. |
| Operação | Scripts PowerShell/Bash e unidade systemd de um worker. `CADS_DATA_DIR` separa dados graváveis. Documentação de Nginx/HTTPS em `DEPLOY.md`; configuração efetivamente implantada não foi acessada. |

Não foi encontrado `.git` nesta pasta, nem `AGENTS.md` no projeto e nos ancestrais verificados. As mudanças foram feitas no diretório fornecido; não existe identificação de commit/base nesta entrega. Foram lidos nomes e padrões de configuração em `.env.example`, código e serviço, sem consultar conteúdo de bancos reais ou imprimir credenciais. O exemplo não contém uma chave administrativa preenchida.

### Inventário funcional e cobertura

- Contas: cadastro, login, logout, sessão, clientes, aprovação/suspensão, notificações e leitura de notificações — testes locais de integração existentes.
- Painel: estatísticas, histórico, download, exclusão e ocultação do histórico — testes existentes; exclusões exclusivamente em dados temporários.
- Imagem e vídeo: transformação, limpeza de metadados, formato inválido e orientação EXIF — testes existentes com mídia sintética curta.
- Estúdio IA: configuração, validação, falhas, trava de geração e persistência — testes com provedores simulados; sem validação de contratos externos reais.
- Clonador: prova local de leitura indevida. Webhook: validação do destino por interceptação simulada, sem transmissão.
- Análise de conteúdo e roteiros: testes locais de comportamento; não houve validação jurídica ou de políticas publicitárias.
- Landing pages, SEO, cloaker, capas, todas as combinações de processamento, todos os botões e integrações: revisão parcial de código; **não classificados como aprovados ponta a ponta**.

## B. Bugs e riscos identificados

Prioridades seguem P0 crítico, P1 alto, P2 médio e P3 baixo. “Confirmado localmente” não significa explorado em produção.

| ID / prioridade | Estado, localização e evidência | Reprodução / esperado versus encontrado | Correção ou recomendação / esforço |
|---|---|---|---|
| SEC-01 / P0 | Confirmado localmente. `server/main.py:1000`, `clone_page`. | Em ambiente isolado, criar arquivo com marcador, autenticar cliente aprovado, enviar seu URI `file://` para `/api/clone/page` e baixar o resultado. Esperado: rejeitar esquemas não HTTP(S) e arquivos locais. Encontrado: HTTP 200 e conteúdo exato do marcador. | Restringir esquema/destino, bloquear redes privadas e revalidar redirecionamentos; proteger contra resolução DNS divergente; limitar resposta. Plano abaixo; não alterado. Esforço médio. |
| SEC-02 / P1 | Destino arbitrário confirmado com mock; alcance de rede não explorado. `server/main.py:1539`. | Chamar webhook Discord com URL loopback sintética, interceptando `urlopen`. Esperado: aceitar apenas endpoint autorizado do Discord. Encontrado: tentativa de usar diretamente a URL recebida, sem timeout explícito. | Allowlist estrita de host, porta e caminho, impedir redirects, impor timeout e limite de resposta. Esforço médio. |
| SEC-03 / P2 | Interpretação indevida de HTML confirmada por teste da função de interface; corrigida localmente. `public/index.html:1421`. | Enviar texto com marcação à análise. Backend devolve o texto em `safe_rewrite`; interface o inseria em `innerHTML`. Esperado: texto literal. Encontrado: marcação não escapada. Não foi demonstrada exploração entre usuários. | Reutilizado `escapeHtml`; regressão falhou antes e passou depois. Esforço baixo. |
| FUN-01 / P1 | Confirmado no navegador de produção e corrigido localmente. `public/login.js:7`, `public/login.html`. | Cadastro → nome `A` → preencher e-mail/senha → Entrar → enviar. Esperado: validar somente campos do login. Encontrado: nome oculto com `validity.tooShort=true`, habilitado, bloqueia envio sem erro visível. | Desabilitar campo quando oculto, reabilitar no cadastro; atributo inicial `disabled`. Teste real de navegador com API simulada aprovado. Esforço baixo. |
| DEP-01 / P1 | Dependência instalada afetada segundo OSV; explorabilidade das rotas não confirmada. `requirements.txt`, Pillow 12.3.0. | Consulta por versão retornou 41 identificadores GHSA. Quatro avisos inspecionados individualmente indicam correção em 13.0.0. Nem todo formato/operação descrito é alcançável pelas rotas. | Triar alcance de cada aviso e testar atualização compatível em ambiente separado. Não atualizar cegamente produção. Esforço médio. |
| UX-01 / P2 | Confirmado na página pública. `public/login.html`. | Abrir login e procurar recuperação/suporte. Esperado: caminho utilizável para recuperar acesso ou contatar suporte. Encontrado: orientação para falar com administração sem endereço/link e sem recuperação de senha na tela. | Informar canal real autorizado; recuperação de senha exige desenho e aprovação de autenticação. Esforço baixo para contato, alto para recuperação. |
| UX-02 / P3 | Confirmado na interação pública. `public/login.html/login.js`. | Focar aba Entrar e pressionar seta direita. Esperado para widget `tablist`: navegação coerente por setas. Encontrado: seleção/foco não mudam; eventos apenas de clique. | Implementar padrão de teclado e relação tab/tabpanel, ou simplificar semântica. Esforço baixo. |
| PERF-01 / P1 | Risco por inspeção; impacto sob concorrência não medido. `server/main.py`, rotas async com urllib/Pillow/subprocess síncronos. | Código de clonagem, webhook e vídeo executa trabalho bloqueante dentro de `async def`. Esperado: permitir progresso de outras requisições. Encontrado: operações síncronas no caminho do event loop. | Mover tarefas bloqueantes para execução adequada e limitar concorrência; testar latência concorrente em staging. Esforço médio/alto. |
| PERF-02 / P2 | Confirmado por estrutura do código; escala não medida. `server/main.py:214`. | `/api/stats` lista/ordena arquivos duas vezes e consulta metadados novamente; polling de contas ocorre a cada 20 s por aba visível. | Medir volumes reais; reutilizar uma listagem por requisição antes de criar índice/cache. Esforço baixo/médio. |
| OPS-01 / P1 | Confirmado no ambiente local, não em produção. | Executar suíte: SQLite reporta disco cheio; nova tentativa integral também falhou por espaço. Esperado: espaço para banco/journal/mídia temporários. | Liberar/ampliar espaço por procedimento autorizado e repetir suíte integral. Esforço baixo/médio. |

## C. Segurança

### Proteções verificadas

- Produção: `/` redirecionou com 303 para login; `/studio.html` e `/clients.html` retornaram 303 sem sessão. Login com dados sintéticos inválidos mostrou mensagem genérica. Formulário vazio apresentou validação obrigatória.
- Cabeçalhos observados: CSP, X-Frame-Options DENY, nosniff, HSTS por um ano com subdomínios, Referrer-Policy e Permissions-Policy restritiva. Presença de cabeçalhos não prova segurança completa.
- Contas locais: cookies Secure/HttpOnly/SameSite=Strict/Path=/; token persistido como hash; senhas com scrypt; tentativas limitadas; cadastro não aceita autoelevação de perfil; origem divergente rejeitada; suspensão e logout revogam acesso.
- Clientes locais: arquivo, estatística e notificação isolados; download cruzado retorna 404. Usuário pendente não processa arquivos. Rotas administrativas exigem as credenciais previstas.
- SQL revisado usa parâmetros nos fluxos de contas. Não foi realizada cobertura exaustiva de injeção de SQL ou migrações.

### Dependências

Consulta reproduzível à API OSV em 26/10/2026, registrada em `dependencias-osv.json`: FastAPI 0.143.0, Starlette 1.6.0, Uvicorn 0.53.0, python-multipart 0.0.32, imageio-ffmpeg 0.6.0, httpx 0.28.1 e Pydantic 2.13.5 não retornaram avisos nessa consulta. Isso não certifica ausência de vulnerabilidades nem cobre todas as dependências transitivas ou o binário FFmpeg.

Pillow 12.3.0 retornou 41 avisos. Amostra inspecionada:

- [GHSA-22hg-r758-rrw9](https://osv.dev/vulnerability/GHSA-22hg-r758-rrw9): loop em MIC com TIFF embutido; intervalo afetado inclui 12.3.0, correção indicada em 13.0.0.
- [GHSA-24rc-8hj2-v98c](https://osv.dev/vulnerability/GHSA-24rc-8hj2-v98c): amplificação de memória no decodificador BMP RLE; correção indicada em 13.0.0.
- [GHSA-254f-3936-288h](https://osv.dev/vulnerability/GHSA-254f-3936-288h): recursão sem limite no parser PDF; correção indicada em 13.0.0.
- [GHSA-27h8-gf7j-v5hc](https://osv.dev/vulnerability/GHSA-27h8-gf7j-v5hc): desreferência nula em filtros de imagens de tamanho zero; correção indicada em 13.0.0.

Não foram enviados arquivos maliciosos nem explorados esses avisos. Os filtros de formato e tamanho existentes reduzem algumas superfícies; é necessário analisar decodificação anterior à validação e todos os caminhos de upload. `pip check` não encontrou dependências incompatíveis. Isso verifica compatibilidade declarada, não segurança.

### Riscos ainda não confirmados por exploração

- `innerHTML` recebe nomes de arquivos em `handleVideoFileChange` e `handleCoverFileChange`, além de mensagens em outros resultados. A correção de SEC-03 não elimina todos os possíveis pontos de XSS. Revisar cada origem e reutilizar escape/textContent.
- CSP permite `'unsafe-inline'`; um ponto de injeção de HTML pode ganhar impacto. Eliminar scripts/eventos embutidos antes de endurecer a política.
- `download_video` valida DNS e depois abre uma nova conexão por hostname: avaliar DNS rebinding em staging. Não houve prova dessa hipótese.
- Exceções cruas são devolvidas em algumas rotas (`detail=str(e)`); podem revelar caminhos/detalhes internos. Segredos reais não foram provocados nem observados.
- Não há índice explícito em `customer_sessions.customer_id` ou `expires_at`, usados em revogação/limpeza. Avaliar planos de consulta com volume real antes de migrar.
- A trava global de geração impede duplicação simultânea, mas também rejeita trabalho de outros clientes durante a geração. Não há idempotência persistente contra repetição após perda de resposta.

### Plano para mudanças críticas — sujeito à aprovação

1. Priorizar SEC-01: restringir temporariamente o clonador em ambiente controlado; implementar validação de esquema, host, endereços resolvidos, redirects, tamanho e prazo. Incluir regressões para arquivo local, loopback, IPv6 privado, redirecionamento e destino público permitido, sempre com servidor de teste.
2. Corrigir SEC-02 com endpoint Discord estritamente validado e timeout; manter envio real desativado nos testes.
3. Triar os avisos Pillow e preparar atualização isolada, validando compatibilidade de imagem/vídeo e limites de recursos.
4. Rever operações bloqueantes e concorrência antes de ampliar workers; os limites atuais são por processo.
5. Se houver evidência de exploração anterior, fazer investigação e eventual rotação de credenciais por procedimento separado. Esta auditoria não constatou exploração real.

Esses itens não foram implementados porque o pedido exige aprovação prévia para lógica crítica/infraestrutura. A aprovação deve ser sobre o plano e um ambiente de validação; nenhuma alteração em produção está autorizada por esta entrega.

## D. Performance

Medições reais HTTP, uma amostra por recurso, via curl a partir deste computador, sem seguir redirects nesta série. TTFB inclui DNS/TLS/conexão; não representa exclusivamente tempo do backend. Sem aquecimento, percentis ou simulação de rede móvel.

| Recurso | HTTP | Bytes de corpo | TTFB / total (s) |
|---|---:|---:|---:|
| `/` | 303 | 0 | 0,875 / 0,875 |
| `/login.html` | 200 | 1.865 | 0,875 / 0,875 |
| `/login.js?v=1` | 200 | 2.201 | 0,875 / 0,875 |
| `/portal.css?v=1` | 200 | 6.272 | 0,906 / 0,906 |
| `/stealthforge-mark.svg` | 200 | 1.296 | 0,891 / 0,891 |
| `/studio.html` | 303 | 0 | 0,891 / 0,891 |
| `/clients.html` | 303 | 0 | 0,875 / 0,875 |

Fonte: `http-medicoes.tsv` (colunas caminho, status, bytes, TTFB, total). São transferências individuais; não somar como tempo de renderização. HTML+JS+CSS+SVG públicos somaram 11.634 bytes de corpo nessa coleta.

Tamanhos locais antes das alterações: `index.html` 81.058 B; `studio.js` 10.020 B; `studio.html` 8.777 B; `studio-theme.css` 7.857 B; `stealthforge.css` 7.568 B; `session.js` 6.465 B. Não são tamanhos comprimidos transferidos em produção.

Não foram medidos Lighthouse, LCP, CLS, INP, consumo de memória sob carga ou latência de consultas com volume real. Não houve teste agressivo. O pequeno peso do login é positivo, mas não permite concluir que o painel ou processamento sejam rápidos. As prioridades são retirar bloqueios síncronos do caminho async, limitar respostas externas e reduzir varreduras repetidas do histórico antes de adicionar infraestrutura.

## E. Experiência do usuário

Login público inspecionado visualmente em desktop. Após a correção, login local verificado com Playwright em 1280×800, 768×1024 e 390×844: sem rolagem horizontal nas três larguras. Capturas mobile/tablet foram inspecionadas; campos, títulos e botão estavam legíveis, sem sobreposição evidente. Não houve medição automática de contraste nem leitor de tela.

Arquivos: `login-desktop.png` (produção), `login-desktop-local.png`, `login-tablet-local.png`, `login-mobile-local.png` (fonte local corrigida, API simulada). Essas capturas não representam o painel autenticado.

O login usa labels, autocomplete e alerta para erros. A mensagem de credenciais inválidas é compreensível. A interface explica a aprovação manual antes de usar processamento. Faltam um canal de suporte acionável e interação de teclado completa das abas. O painel, o estúdio e a administração ainda precisam de revisão visual responsiva com contas sintéticas em staging; a aprovação dos testes de API não substitui essa etapa.

## F. Testes automatizados

| Execução | Resultado real |
|---|---|
| `python -m unittest discover -s tests -v` | 60 testes, 42 sem falhas, 18 afetados; 1 failure e 23 errors, incluindo limpeza. 42,874 s. Disco cheio. |
| Repetição `test_accounts.py` | 11/11 aprovados em 21,450 s. |
| Repetição `test_functional_claims.py` | 7/7 aprovados em 5,686 s. |
| Repetição do teste de diretório de estado/configuração | 1/1 aprovado em 1,526 s. |
| Segunda tentativa integral | Interrompida por falta de espaço; não aprovada. |
| `node tests/test_dashboard.js` antes de adicionar regressão | 8 cenários aprovados. |
| Regressão de HTML não escapado antes da correção | Falhou, demonstrando a inserção não escapada. |
| `node tests/test_dashboard.js` após correção | 9 cenários aprovados. |
| `node tests/test_login.js` | Login após nome inválido no cadastro e preservação da validação aprovados; larguras sem overflow aprovadas. API de login simulada. |
| `node --check public/studio.js` e `node --check public/login.js` | Aprovados; somente sintaxe. |
| `python -m pip check` | Sem requisitos incompatíveis. |
| Provas locais adicionais | Leitura de marcador via clonador confirmada; webhook loopback aceito no mock; eco de marcação confirmado; visitante recebeu 401 em stats. |

Os testes de provedores utilizam mocks; não comprovam que modelos/contratos/saldo estejam operacionais hoje. Os testes de vídeo usaram mídia sintética curta. Testes locais de shell não equivalem a validar systemd num VPS Linux. Foi emitido aviso de depreciação do TestClient com httpx; avaliar migração do ambiente de desenvolvimento separadamente.

O novo teste de navegador usa Playwright disponível no runtime local, sem instalar pacotes. Para reproduzir neste computador, definir `NODE_PATH` para os módulos do runtime, `AUDIT_CHROMIUM` para o Chromium existente e executar `node tests/test_login.js`; opcionalmente definir `AUDIT_SCREENSHOTS` para uma pasta de capturas. Paths usados:

```text
NODE_PATH=C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules
AUDIT_CHROMIUM=C:\Users\Administrator\AppData\Local\ms-playwright\chromium-1248\chrome-win64\chrome.exe
```

Para testes Python, configurar `CADS_DATA_DIR` para uma pasta de teste vazia **antes de importar `server.main`**, pois o módulo inicializa bancos durante a importação. Essa separação foi aplicada nesta auditoria. Provedores reais, e-mails e webhooks não foram acionados.

## G. Correções realizadas

| Arquivo | Alteração | Validação / risco |
|---|---|---|
| `public/login.html` | Nome inicia desabilitado no modo login. | Playwright e inspeção de telas. Nenhuma mudança na autenticação do servidor. |
| `public/login.js` | Alternância de modo também alterna `disabled` do nome. | Login deixa de bloquear por nome oculto; cadastro continua exigindo nome válido. |
| `public/index.html` | `escapeHtml(data.safe_rewrite)` no resultado da análise. | Regressão demonstrada antes/depois. Texto com tags aparece como texto; outros pontos de HTML precisam revisão. |
| `tests/test_login.js` | Teste real de navegador com respostas locais simuladas. | Sem dependência adicionada ao aplicativo; requer Playwright no ambiente de testes. |
| `tests/test_dashboard.js` | Regressão para não interpretar o texto analisado como HTML. | Nove cenários aprovados. |

`correcoes.patch` registra as três alterações nos arquivos de interface; os arquivos de teste permanecem no projeto. Cópias originais dos três arquivos de interface ficaram na pasta de trabalho da auditoria. O relatório foi salvo no projeto e copiado para a pasta de entregáveis. Não foi alterado backend, banco, configuração sensível ou dependência.

## H. Melhorias futuras por ordem de execução

| Prioridade | Ação | Esforço | Impacto |
|---|---|---|---|
| P0 | Corrigir leitura de arquivos/SSRF do clonador após aprovação do plano. | Médio | Proteção do servidor e possíveis segredos acessíveis ao processo. |
| P1 | Restringir webhook e adicionar prazos/limites. | Médio | Reduzir alcance a serviços internos e bloqueios. |
| P1 | Triar/atualizar Pillow com regressões de mídia. | Médio | Reduzir riscos conhecidos em arquivos não confiáveis. |
| P1 | Resolver espaço local e obter execução integral verde. | Baixo/médio | Tornar validações reproduzíveis e confiáveis. |
| P1 | Validar e publicar separadamente as duas correções locais, somente com autorização. | Baixo | Remover bloqueio de login e interpretação de HTML neste resultado. |
| P2 | Completar revisão de sinks HTML, erros e acessibilidade. | Médio | Segurança de interface e usabilidade. |
| P2 | Validar painel/estúdio/clientes em desktop, tablet e celular com staging. | Médio | Cobrir fluxos não executados ponta a ponta. |
| P2 | Medir concorrência, varreduras de disco e consultas antes de otimizar. | Médio | Latência e capacidade por cliente. |
| P2 | Definir política de idempotência, retenção de arquivos e recuperação de acesso. | Alto | Evitar custos duplicados, crescimento ilimitado e bloqueio de usuários. |
| P3 | Ajustar teclado das abas e disponibilizar canal real de suporte. | Baixo | Acesso e orientação para novos usuários. |

### Limites finais

Esta é uma auditoria com evidências e cobertura explicitamente delimitada, não um certificado de segurança nem uma inspeção integral de produção. Sem sessão de teste em produção/staging, sem acesso ao servidor implantado e com disco local quase cheio, permaneceram pendentes: revisão visual de todas as telas internas, matriz completa de formulários/combinações, carga controlada, Lighthouse, integrações reais, inspeção de logs/configuração do VPS, migrações e consistência dos dados reais. Nenhum desses itens foi marcado como aprovado por inferência.
