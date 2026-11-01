import os
import sys

# Forcar stdout em utf-8 pra evitar erro de encoding no Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

target_file = "server/product_video.py"
if not os.path.exists(target_file):
    print("ERRO: " + target_file + " nao encontrado.")
    sys.exit(1)

with open(target_file, "r", encoding="utf-8") as f:
    content = f.read()

original = content

# 1. Adicionar os 3 novos provedores no dict PROVIDERS
new_providers_block = '''    "fal_flux_schnell": {
        "name": "fal.ai Flux Schnell (Imagem)",
        "model": "flux-schnell",
        "endpoint": "https://queue.fal.run/fal-ai/flux/schnell",
        "cost": "~$0.003/imagem (ultra-barato)",
        "env_key": "FAL_KEY",
        "video_capable": False,
        "image_capable": True,
    },
    "replicate_flux_dev": {
        "name": "Replicate Flux Dev (Imagem)",
        "model": "flux-dev",
        "endpoint": "https://api.replicate.com/v1/predictions",
        "cost": "~$0.025/imagem (qualidade top)",
        "env_key": "REPLICATE_API_TOKEN",
        "video_capable": False,
        "image_capable": True,
    },
    "replicate_flux_pro": {
        "name": "Replicate Flux Pro 1.1 (Imagem)",
        "model": "flux-1.1-pro",
        "endpoint": "https://api.replicate.com/v1/predictions",
        "cost": "~$0.04/imagem (premium)",
        "env_key": "REPLICATE_API_TOKEN",
        "video_capable": False,
        "image_capable": True,
    },
'''

marker = '''    "fal_svd": {
        "name": "fal.ai Stable Video Diffusion",
        "model": "stable-video-diffusion",
        "endpoint": "https://queue.fal.run/fal-ai/stable-video",
        "cost": "Consulte o consumo e o saldo na fal.ai.",
        "env_key": "FAL_KEY",
        "video_capable": True,
    },
}'''

replacement = '''    "fal_svd": {
        "name": "fal.ai Stable Video Diffusion",
        "model": "stable-video-diffusion",
        "endpoint": "https://queue.fal.run/fal-ai/stable-video",
        "cost": "Consulte o consumo e o saldo na fal.ai.",
        "env_key": "FAL_KEY",
        "video_capable": True,
    },
''' + new_providers_block + '''}'''

if "fal_flux_schnell" in content:
    print("[OK] Provedores Flux ja existem no dict.")
elif marker in content:
    content = content.replace(marker, replacement)
    print("[OK] Provedores Flux adicionados ao dict PROVIDERS.")
else:
    print("[AVISO] Marcador do dict PROVIDERS nao encontrado exato. Tentando fallback...")
    # Fallback: inserir antes do ultimo } do dict
    idx = content.find('"fal_svd"')
    if idx != -1:
        end_idx = content.find("\n}", idx)
        if end_idx != -1:
            insert_point = content.rfind(",", idx, end_idx) + 1
            content = content[:insert_point] + "\n" + new_providers_block + content[insert_point:]
            print("[OK] Provedores Flux inseridos via fallback.")
        else:
            print("[ERRO] Nao foi possivel localizar fechamento do dict.")
    else:
        print("[ERRO] fal_svd nao encontrado no arquivo.")

# 2. Atualizar studio_config pra incluir image_capable
old_config_logic = '''        configured_providers[pid] = {
            "name": pinfo["name"],
            "cost": pinfo["cost"],
            "configured": bool(os.environ.get(env_key)),
            "video_capable": pinfo["video_capable"],
            "supports_scene": pid != "fal_svd",
            "supports_aspect_ratio": pid != "fal_svd",
        }'''

new_config_logic = '''        configured_providers[pid] = {
            "name": pinfo["name"],
            "cost": pinfo["cost"],
            "configured": bool(os.environ.get(env_key)),
            "video_capable": pinfo.get("video_capable", False),
            "image_capable": pinfo.get("image_capable", False),
            "supports_scene": pid not in ("fal_svd", "fal_flux_schnell", "replicate_flux_dev", "replicate_flux_pro"),
            "supports_aspect_ratio": pid not in ("fal_svd", "fal_flux_schnell"),
        }'''

if "image_capable" in content and "pinfo.get" in content:
    print("[OK] Logica de config ja atualizada.")
elif old_config_logic in content:
    content = content.replace(old_config_logic, new_config_logic)
    print("[OK] Logica de config atualizada para suportar image_capable.")
else:
    print("[AVISO] Logica de config nao encontrada exatamente. Verifique manualmente.")

# 3. Adicionar funcoes de imagem e dispatcher
image_functions_code = '''
# === fal.ai Flux Schnell: geracao de imagem (ultra-rapido, ultra-barato) ===
def request_image_fal_flux_schnell(prompt, api_key):
    payload = {
        "prompt": prompt,
        "image_size": "square_hd",
        "num_inference_steps": 4,
        "enable_safety_checker": False,
    }
    request = urllib.request.Request(
        PROVIDERS["fal_flux_schnell"]["endpoint"],
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Key {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            submit_result = json.loads(response.read())
        request_id = submit_result.get("request_id")
        if not request_id:
            raise HTTPException(502, "fal.ai nao retornou request_id")
        status_url = f"{PROVIDERS['fal_flux_schnell']['endpoint']}/requests/{quote(str(request_id), safe='')}/status"
        result_url = f"{PROVIDERS['fal_flux_schnell']['endpoint']}/requests/{quote(str(request_id), safe='')}"
        completed = False
        for _ in range(60):
            time.sleep(2)
            status_req = urllib.request.Request(status_url, headers={"Authorization": f"Key {api_key}"})
            with urllib.request.urlopen(status_req, timeout=15) as sr:
                status_data = json.loads(sr.read())
            if status_data.get("status") == "COMPLETED":
                completed = True
                break
            if status_data.get("status") == "FAILED":
                raise HTTPException(502, f"fal.ai Flux Schnell falhou: {status_data.get('error', 'unknown')}")
        if not completed:
            raise HTTPException(504, "Timeout aguardando fal.ai Flux Schnell (2 min).")
        result_req = urllib.request.Request(result_url, headers={"Authorization": f"Key {api_key}"})
        with urllib.request.urlopen(result_req, timeout=30) as rr:
            result_data = json.loads(rr.read())
        images = result_data.get("images", [])
        if not images or not images[0].get("url"):
            raise HTTPException(502, "fal.ai Flux Schnell nao retornou imagem.")
        return _download_media_safe(images[0]["url"])
    except HTTPException:
        raise
    except urllib.error.HTTPError as error:
        error.close()
        messages = {
            400: "fal.ai recusou a requisicao. Verifique o prompt.",
            401: "Chave fal.ai invalida. Confira em fal.ai/dashboard.",
            402: "Creditos fal.ai esgotados.",
            429: "Rate limit fal.ai atingido.",
        }
        raise HTTPException(502, messages.get(error.code, f"Erro fal.ai Flux Schnell: HTTP {error.code}")) from None
    except (socket.timeout, TimeoutError, urllib.error.URLError):
        raise HTTPException(504, "Timeout na comunicacao com fal.ai Flux Schnell.") from None
    except (ValueError, TypeError, AttributeError, KeyError):
        raise HTTPException(502, "A fal.ai Flux Schnell retornou uma resposta invalida.") from None


# === Replicate Flux (Dev/Pro): geracao de imagem ===
def _request_image_replicate(model_version, prompt, api_key):
    payload = {
        "version": model_version,
        "input": {
            "prompt": prompt,
            "aspect_ratio": "1:1",
            "output_format": "jpg",
            "output_quality": 92,
        },
    }
    request = urllib.request.Request(
        "https://api.replicate.com/v1/predictions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Prefer": "wait",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read())
        output = result.get("output")
        if isinstance(output, list) and output:
            image_url = output[0]
        elif isinstance(output, str):
            image_url = output
        else:
            raise HTTPException(502, "Replicate nao retornou imagem.")
        return _download_media_safe(image_url)
    except HTTPException:
        raise
    except urllib.error.HTTPError as error:
        error.close()
        messages = {
            400: "Replicate recusou a requisicao. Verifique o prompt.",
            401: "Chave Replicate invalida. Confira em replicate.com/account/api-tokens.",
            402: "Creditos Replicate esgotados.",
            429: "Rate limit Replicate atingido.",
        }
        raise HTTPException(502, messages.get(error.code, f"Erro Replicate: HTTP {error.code}")) from None
    except (socket.timeout, TimeoutError, urllib.error.URLError):
        raise HTTPException(504, "Timeout na comunicacao com Replicate.") from None
    except (ValueError, TypeError, AttributeError, KeyError):
        raise HTTPException(502, "O Replicate retornou uma resposta invalida.") from None


def request_image_replicate_flux_dev(prompt, api_key):
    return _request_image_replicate("black-forest-labs/flux-dev", prompt, api_key)


def request_image_replicate_flux_pro(prompt, api_key):
    return _request_image_replicate("black-forest-labs/flux-1.1-pro", prompt, api_key)


# === Download seguro de media (imagem) com validacao SSRF ===
def _download_media_safe(url):
    import ipaddress
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(502, "O provedor retornou um endereco de imagem invalido.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise HTTPException(502, "O provedor retornou um endereco de imagem privado.")
    except OSError:
        raise HTTPException(502, "Nao foi possivel localizar a imagem do provedor.") from None

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    with urllib.request.build_opener(NoRedirect()).open(url, timeout=60) as response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise HTTPException(502, "A imagem excedeu o limite de download.")
    return data


# === DISPATCHER: escolhe o provedor certo para IMAGEM ===
def request_image(provider, prompt, api_key):
    dispatchers = {
        "fal_flux_schnell": request_image_fal_flux_schnell,
        "replicate_flux_dev": request_image_replicate_flux_dev,
        "replicate_flux_pro": request_image_replicate_flux_pro,
    }
    fn = dispatchers.get(provider)
    if not fn:
        raise HTTPException(400, f"Provedor de imagem desconhecido: {provider}. Use: {', '.join(dispatchers.keys())}")
    return fn(prompt, api_key)


'''

dispatcher_marker = "# === DISPATCHER: escolhe o provedor certo ==="
if "def request_image(" in content:
    print("[OK] Funcoes de imagem e dispatcher ja existem.")
elif dispatcher_marker in content:
    content = content.replace(dispatcher_marker, image_functions_code + dispatcher_marker)
    print("[OK] Funcoes de imagem e dispatcher adicionados.")
else:
    print("[AVISO] Marcador de dispatcher nao encontrado. Verifique manualmente.")

# 4. Adaptar endpoint /generate - validacao de provider
old_check = '''    if not PROVIDERS[provider]["video_capable"]:
        raise HTTPException(400, "Este provedor nao gera videos. Selecione deAPI, Google pago ou fal.ai.")'''

new_check = '''    is_image_provider = PROVIDERS[provider].get("image_capable", False)
    is_video_provider = PROVIDERS[provider].get("video_capable", False)
    if not is_image_provider and not is_video_provider:
        raise HTTPException(400, "Este provedor nao gera conteudo. Selecione um provedor valido.")'''

if "is_image_provider" in content:
    print("[OK] Validacao de provider ja atualizada.")
elif old_check in content:
    content = content.replace(old_check, new_check)
    print("[OK] Validacao de provider atualizada.")
else:
    print("[AVISO] Validacao de provider nao encontrada exatamente. Verifique manualmente.")

# 5. Adaptar logica de geracao (prompt + chamada + filename)
old_gen_logic = '''        prompt = (
            f"Crie um video curto de apresentacao do produto {product_name}, usando a foto como primeiro quadro e referencia. "
            "Preserve o formato, as cores, a embalagem, a marca e os detalhes reais do produto. "
            "Nao invente propriedades, precos, ofertas ou resultados do produto. "
            f"Direcao criativa: {scene}"
        )
        video = request_video(provider, image, prompt, aspect_ratio, key)
        if not isinstance(video, bytes) or len(video) > MAX_RESPONSE_BYTES or len(video) < 12 or video[4:8] != b"ftyp":
            raise HTTPException(502, "O provedor nao retornou um MP4 valido. Nenhum arquivo foi salvo.")
        filename = f"cads_ai_produto_{secrets.token_hex(8)}.mp4"'''

new_gen_logic = '''        if is_image_provider:
            image_prompt = (
                f"Crie uma imagem profissional do produto {product_name}. "
                "Preserve o formato, as cores, a embalagem, a marca e os detalhes reais do produto. "
                "Nao invente propriedades, precos, ofertas ou resultados do produto. "
                f"Direcao criativa: {scene}"
            )
            media_data = request_image(provider, image_prompt, key)
            if not isinstance(media_data, bytes) or len(media_data) > MAX_RESPONSE_BYTES or len(media_data) < 100:
                raise HTTPException(502, "O provedor nao retornou uma imagem valida. Nenhum arquivo foi salvo.")
            filename = f"cads_ai_produto_{secrets.token_hex(8)}.jpg"
        else:
            prompt = (
                f"Crie um video curto de apresentacao do produto {product_name}, usando a foto como primeiro quadro e referencia. "
                "Preserve o formato, as cores, a embalagem, a marca e os detalhes reais do produto. "
                "Nao invente propriedades, precos, ofertas ou resultados do produto. "
                f"Direcao criativa: {scene}"
            )
            media_data = request_video(provider, image, prompt, aspect_ratio, key)
            if not isinstance(media_data, bytes) or len(media_data) > MAX_RESPONSE_BYTES or len(media_data) < 12 or media_data[4:8] != b"ftyp":
                raise HTTPException(502, "O provedor nao retornou um MP4 valido. Nenhum arquivo foi salvo.")
            filename = f"cads_ai_produto_{secrets.token_hex(8)}.mp4"'''

if "if is_image_provider:" in content and "request_image(provider" in content:
    print("[OK] Logica de geracao ja adaptada.")
elif old_gen_logic in content:
    content = content.replace(old_gen_logic, new_gen_logic)
    # Trocar destination.write(video) por destination.write(media_data)
    if "destination.write(video)" in content:
        content = content.replace("destination.write(video)", "destination.write(media_data)")
        print("[OK] Escrita de arquivo adaptada para media_data.")
    print("[OK] Logica de geracao adaptada para imagem/video.")
else:
    print("[AVISO] Logica de geracao nao encontrada exatamente. Verifique manualmente.")

# 6. Adaptar retorno JSON pra incluir media_type
old_return = '''        return {
            "status": "success",
            "filename": filename,
            "download_url": f"/api/download/{filename}",
            "model": provider_info["model"],
            "provider": provider_info["name"],
            "cost": provider_info["cost"],
        }'''

new_return = '''        return {
            "status": "success",
            "filename": filename,
            "download_url": f"/api/download/{filename}",
            "model": provider_info["model"],
            "provider": provider_info["name"],
            "cost": provider_info["cost"],
            "media_type": "image" if is_image_provider else "video",
        }'''

if "media_type" in content:
    print("[OK] Retorno JSON ja inclui media_type.")
elif old_return in content:
    content = content.replace(old_return, new_return)
    print("[OK] Retorno JSON atualizado com media_type.")
else:
    print("[AVISO] Retorno JSON nao encontrado exatamente.")

if content == original:
    print("[NADA] Nenhuma alteracao aplicada. Verifique os marcadores.")
else:
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(content)
    print("[FIM] Backend patcheado com sucesso!")

# Validacao rapida de sintaxe
try:
    compile(content, target_file, "exec")
    print("[SINTAXE] OK - arquivo compila sem erros.")
except SyntaxError as e:
    print(f"[SINTAXE] ERRO na linha {e.lineno}: {e.msg}")