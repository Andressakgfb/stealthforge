# Deploy do CADS Pro em Ubuntu/Debian

Este guia prepara uma implantação nova. Os comandos devem ser executados no
VPS, em uma conta com sudo. Não foi feita instalação remota por este pacote.
Use Python 3.10+ e uma distribuição ainda suportada pelo fornecedor.

## 1. Dependências e usuário

```bash
sudo apt-get update
sudo apt-get install python3 python3-venv python3-pip openssl nginx certbot python3-certbot-nginx
id -u cads >/dev/null 2>&1 || sudo useradd --system --user-group --home-dir /var/lib/cads-pro --shell /usr/sbin/nologin cads
sudo install -d -o root -g root -m 0755 /opt/cads-pro
```

Envie o projeto para o VPS por um canal privado. A partir da pasta que contém
os arquivos do projeto, copie o código sem enviar `.env`, bancos ou mídias de
outra máquina:

```bash
sudo install -d -o root -g root -m 0755 /opt/cads-pro/server
sudo cp server/*.py /opt/cads-pro/server/
sudo cp -a public requirements.txt cads-pro.service /opt/cads-pro/
sudo chown -R root:root /opt/cads-pro
sudo chmod -R go-w /opt/cads-pro
sudo python3 -m venv /opt/cads-pro/venv
sudo /opt/cads-pro/venv/bin/python -m pip install -r /opt/cads-pro/requirements.txt
```

A cópia de `server/*.py` evita enviar o banco e os logs locais dessa pasta.
O guia usa um banco novo no diretório de estado do serviço. Não inclua bancos
de desenvolvimento ou credenciais em pacotes de código.

Confirme que os arquivos são legíveis por `cads` e que nenhuma pasta de código
é gravável por esse usuário. Não use `chmod 777`. A conta `cads` não é proprietária
do código nem do ambiente virtual; atualizações devem ser feitas por um operador.

## 2. Credencial e ambiente privado

Crie um arquivo novo; não sobrescreva um `.env` existente sem preservá-lo:

```bash
sudo install -o root -g root -m 0600 .env.example /opt/cads-pro/.env
sudoedit /opt/cads-pro/.env
```

Gere uma chave em uma sessão não gravada com `openssl rand -hex 32` ou com
`generate_admin_key.sh`. Guarde-a no seu gerenciador de senhas e preencha
`ADMIN_API_KEY` no editor. Mantenha `RATE_LIMIT_*`, `PLAN_LIMIT_DEFAULT` e
`LOG_LEVEL` nos padrões, salvo necessidade concreta.

O formato do arquivo é o de `EnvironmentFile` do systemd: uma atribuição por
linha, sem `export`, sem executar comandos e sem comentários após os valores.
Não use `source .env`. As linhas de comentário devem ficar separadas.
`ADMIN_API_KEY=` está intencionalmente vazio no exemplo: não é uma senha válida.

O systemd lê o arquivo como administrador antes de reduzir os privilégios.
Por isso, `.env` pode permanecer `root:root 0600`; não precisa ser legível pelo
usuário do aplicativo. Não deixe esse arquivo na pasta `public`.

## 3. Serviço

```bash
sudo install -o root -g root -m 0644 cads-pro.service /etc/systemd/system/cads-pro.service
sudo systemd-analyze verify /etc/systemd/system/cads-pro.service
sudo systemctl daemon-reload
sudo systemctl enable --now cads-pro
sudo systemctl status cads-pro --no-pager
sudo journalctl -u cads-pro -f
```

A validação `systemd-analyze verify` deve ser feita **depois** de instalar o
Python/venv e os caminhos referidos pela unidade. Se falhar, corrija antes de
habilitar o serviço. Se a chave estiver ausente, curta ou contiver espaços,
`ExecStartPre` recusa a configuração sem imprimir seu valor.

O serviço tem um processo, não usa reload e mantém os parâmetros
`--no-server-header --no-proxy-headers`. O processo atende somente em
`127.0.0.1:8888`, não em `0.0.0.0`. Logs vão para o journal. Não há dependência
de `/var/log/cads-pro`, `uploads/` ou `logs/`, pois não são os caminhos graváveis
usados pelo aplicativo atual.

`StateDirectory=cads-pro` cria `/var/lib/cads-pro`, com acesso restrito ao
usuário dedicado. `CADS_DATA_DIR` faz o aplicativo gravar ali o SQLite e a
subpasta `processed`. A pasta de código permanece somente para leitura com
`ProtectSystem=strict`. `PrivateTmp`, `ProtectHome`, `NoNewPrivileges` e
`UMask=0077` complementam a unidade.

Para parar ou reiniciar após uma mudança de `.env`:

```bash
sudo systemctl stop cads-pro
sudo systemctl restart cads-pro
```

Não rode esses dois comandos como uma sequência obrigatória: escolha a ação
necessária. O systemd envia SIGTERM e aguarda até 45 segundos; depois pode
encerrar à força processos que não saíram. O ambiente desaparece com o processo;
o `.env` privado permanece no disco até ser alterado ou removido pelo operador.

## 4. Verificação local no VPS

```bash
curl -i http://127.0.0.1:8888/api/stats
curl -i -X POST http://127.0.0.1:8888/api/auth/create-key -d 'plan=pro'
curl -i http://127.0.0.1:8888/favicon.ico
```

Resultados esperados: stats 200, criação sem chave 401, favicon 204; cabeçalhos
de segurança e `Server: CADS-Pro` presentes. Para testar a operação autenticada,
use a interface e o campo de senha; não coloque uma chave real na linha de
comando do curl, no histórico do shell ou em exemplos versionados.

## 5. Nginx e HTTPS

Antes de tornar o serviço público, confirme as permissões de acesso necessárias.
Só três rotas têm autenticação administrativa no escopo atual: as ferramentas
de processamento e os downloads continuam acessíveis a quem alcançar o serviço.
Não interprete a presença de ADMIN_API_KEY como login completo do aplicativo.

Aponte o DNS de um domínio que você controla para o VPS. Crie um virtual host
do Nginx substituindo `cads.seudominio.com` pelo domínio real:

```nginx
server {
    listen 80;
    server_name cads.seudominio.com;
    server_tokens off;
    # 300 MiB de video + capa e envelope multipart.
    client_max_body_size 325m;

    location / {
        proxy_pass http://127.0.0.1:8888;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        # O backend não confia em IPs encaminhados; não use cabeçalhos do cliente.
        proxy_set_header X-Forwarded-For "";
        proxy_connect_timeout 10s;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

Teste a configuração e só então recarregue:

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d cads.seudominio.com
sudo certbot renew --dry-run
```

O domínio é um exemplo e deve ser substituído em ambos os locais. O Certbot
precisa alcançar o VPS pelas portas 80/443. Restrinja a porta 8888 ao loopback;
não a abra no firewall. Não desative a validação de certificado no navegador.

Com `--no-proxy-headers`, todos os clientes que passam pelo Nginx compartilham
o IP de loopback e o limite no backend. Isso impede spoofing e mantém os
parâmetros pedidos, mas não entrega rate limiting individual por visitante
atrás do proxy. Para operação pública com limites individuais, aplique-os no
Nginx ou implemente confiança restrita no proxy e armazenamento compartilhado
em uma mudança posterior. Não altere para confiança universal em X-Forwarded-For.

## 6. Backups e atualização

Pare o serviço antes de copiar um banco SQLite sem uma ferramenta de backup
adequada. Preserve `/var/lib/cads-pro` e o `.env` privado em backup criptografado
e separado do código. Não envie bancos e fotos de clientes ao Git.

Para migrar uma instalação antiga, com o serviço parado, copie seu
`server/cads_auth.db` e o conteúdo de `processed/` para os respectivos caminhos
em `/var/lib/cads-pro`, ajuste a propriedade para `cads:cads` e confirme os
backups antes de iniciar. O código não move nem apaga seus dados automaticamente.

Atualize código e dependências como administrador, valide e reinicie. Preserve
o venv ou o pacote anterior para reverter uma atualização que falhe; restaure
banco apenas quando necessário e com uma cópia de segurança válida.

## Problemas comuns

- **Porta 8888 em uso:** identifique o processo com `sudo ss -ltnp`; encerre a
  instância antiga pelo seu gerenciador antes de iniciar outra.
- **401 no painel:** confirme `ADMIN_API_KEY`, reinicie o serviço e use a mesma
  chave no campo administrativo. A chave de IA é diferente.
- **Permission denied no SQLite:** confira `CADS_DATA_DIR`, `StateDirectory` e
  propriedade de `/var/lib/cads-pro`; não torne `/opt/cads-pro/server` gravável.
- **Dependência ausente:** execute o pip do venv, não o Python global.
- **429:** respeite Retry-After; reiniciar só para driblar limites não é solução.
- **Geração longa interrompida:** confira o painel do provedor antes de repetir,
  evitando possível cobrança duplicada. Não deixe uma chave exposta em logs.

Consulte o relatório entregue para saber quais verificações rodaram no Windows
e no Bash do Git e quais exigem validação em Ubuntu/Debian e systemd reais.
