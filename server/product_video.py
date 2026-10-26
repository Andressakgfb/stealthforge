import base64
import binascii
import io
import json
import os
import secrets
import socket
import threading
import time
import math
from urllib.parse import urlsplit, quote
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from server.accounts import customer_processed, expected_origin

router = APIRouter(prefix="/api/studio")
PROCESSED_DIR = Path(os.environ.get("CADS_DATA_DIR") or Path(__file__).resolve().parent.parent) / "processed"
GENERATION_LOCK = threading.Lock()
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = 96 * 1024 * 1024

# === PROVEDORES DE IA (GRATUITOS + PAGOS) ===
PROVIDERS = {
    "gemini_free": {
        "name": "Google Gemini (Free Tier)",
        "model": "gemini-2.0-flash-exp",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent",
        "cost": "GRATUITO (~15 req/min)",
        "env_key": "GEMINI_API_KEY",
        "video_capable": False,  # Free tier nao gera video nativo - usa imagem animada
    },
    "gemini_paid": {
        "name": "Google Gemini Omni Flash (API paga)",
        "model": "gemini-omni-1.1-flash",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/interactions",
        "cost": "PAGO (faturamento ativo)",
        "env_key": "GEMINI_API_KEY",
        "video_capable": True,
    },
    "deapi_ltx": {
        "name": "deAPI LTX-Video",
        "model": "Ltxv_13B_0_9_8_Distilled_FP8",
        "endpoint": "https://api.deapi.ai/api/v2/videos/animations",
        "cost": "Consulte o consumo e o saldo no deAPI.",
        "env_key": "DEAPI_KEY",
        "video_capable": True,
    },
    "fal_svd": {
        "name": "fal.ai Stable Video Diffusion",
        "model": "stable-video-diffusion",
        "endpoint": "https://queue.fal.run/fal-ai/stable-video",
        "cost": "Consulte o consumo e o saldo na fal.ai.",
        "env_key": "FAL_KEY",
        "video_capable": True,
    },
}


@router.get("/config")
def studio_config():
    configured_providers = {}
    for pid, pinfo in PROVIDERS.items():
        env_key = pinfo["env_key"]
        configured_providers[pid] = {
            "name": pinfo["name"],
            "cost": pinfo["cost"],
            "configured": bool(os.environ.get(env_key)),
            "video_capable": pinfo["video_capable"],
            "supports_scene": pid != "fal_svd",
            "supports_aspect_ratio": pid != "fal_svd",
        }
    return {
        "providers": configured_providers,
        "default": "deapi_ltx",
    }


def download_video(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(502, "O provedor retornou um endereco de video invalido.")
    import ipaddress
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise HTTPException(502, "O provedor retornou um endereco de video privado.")
    except OSError:
        raise HTTPException(502, "Nao foi possivel localizar o arquivo do provedor.") from None
    # Nao seguir redirecionamentos para destinos que nao passaram pela validacao acima.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    with urllib.request.build_opener(NoRedirect()).open(url, timeout=120) as response:
        video = response.read(MAX_RESPONSE_BYTES + 1)
    if len(video) > MAX_RESPONSE_BYTES:
        raise HTTPException(502, "O video excedeu o limite de download.")
    if len(video) < 12 or video[4:8] != b"ftyp":
        raise HTTPException(502, "O provedor nao retornou um arquivo MP4 valido.")
    return video


def prepare_image(content):
    try:
        with Image.open(io.BytesIO(content)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(400, "Envie uma foto JPG, PNG ou WEBP.")
            if source.width * source.height > 25_000_000:
                raise HTTPException(400, "A foto deve ter no maximo 25 megapixels.")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1536, 1536))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=92)
            return base64.b64encode(output.getvalue()).decode("ascii")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(400, "Nao foi possivel ler a foto. Escolha outra imagem.") from None


# === Gemini Omni Flash: geracao de video ===
def request_video_gemini_paid(image_b64, prompt, aspect_ratio, api_key):
    payload = {
        "model": "gemini-omni-1.1-flash",
        "input": [
            {"type": "image", "mime_type": "image/jpeg", "data": image_b64},
            {"type": "text", "text": prompt},
        ],
        "generation_config": {"video_config": {"task": "image_to_video"}},
        "response_format": {
            "type": "video", "delivery": "inline",
            "aspect_ratio": aspect_ratio, "resolution": "720p",
        },
        "store": False,
    }
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=360) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise HTTPException(502, "O video retornado excedeu o limite de download.")
        result = json.loads(raw)
        for step in result.get("steps", []):
            if step.get("type") != "model_output":
                continue
            for part in step.get("content", []):
                if part.get("type") == "video" and part.get("data"):
                    video = base64.b64decode(part["data"], validate=True)
                    if len(video) >= 12 and video[4:8] == b"ftyp":
                        return video
        raise HTTPException(502, "O Google nao retornou um video. A geracao pode ter sido bloqueada.")
    except urllib.error.HTTPError as error:
        messages = {
            400: "O Google recusou a foto ou descricao. Revise a solicitacao.",
            401: "Chave do Google invalida. Confira no Google AI Studio.",
            403: "Sua chave nao tem acesso ao modelo. Confira faturamento.",
            404: "Modelo de video indisponivel para sua conta/regiao.",
            429: "Cota do Google atingida. Aguarde ou confira o faturamento.",
        }
        error.close()
        raise HTTPException(502, messages.get(error.code, "Servico do Google indisponivel.")) from None
    except (socket.timeout, TimeoutError, urllib.error.URLError):
        raise HTTPException(504, "Timeout na comunicacao com o Google.") from None
    except (ValueError, TypeError, AttributeError, binascii.Error):
        raise HTTPException(502, "Resposta de video invalida do Google.") from None


# === GEMINI FREE TIER - Geracao de imagem animada (fallback) ===
def request_video_gemini_free(image_b64, prompt, aspect_ratio, api_key):
    raise HTTPException(400, "Gemini Free nao gera videos. Selecione um provedor de video.")


# === deAPI LTX-Video: multipart e polling v2 ===
def request_video_deapi(image_b64, prompt, aspect_ratio, api_key):
    try:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
        model = None
        for page in range(1, 21):
            req = urllib.request.Request(f"https://api.deapi.ai/api/v2/models?filter%5Binference_types%5D=img2video&limit=50&page={page}", headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                catalogue = json.loads(response.read(2 * 1024 * 1024))
            model = next((m for m in catalogue["data"] if m.get("slug") == PROVIDERS["deapi_ltx"]["model"]
                          and "img2video" in m.get("inference_types", [])), None)
            if model or page >= catalogue.get("meta", {}).get("last_page", 1):
                break
        if not model:
            raise HTTPException(422, "O modelo LTX configurado nao esta disponivel nesta conta deAPI. Nenhuma geracao foi enviada.")
        info = model.get("info", {})
        limits, defaults = info.get("limits", {}), info.get("defaults", {})
        step = int(limits.get("dimension_multiple") or limits.get("resolution_step") or 32)
        unit = step * math.ceil(max(512, limits.get("min_width", 0), limits.get("min_height", 0)) / (9 * step))
        width, height = (9 * unit, 16 * unit) if aspect_ratio == "9:16" else (16 * unit, 9 * unit)
        if (width > limits.get("max_width", 2048) or height > limits.get("max_height", 2048)
                or width * height > limits.get("max_pixels", 2048 * 2048)):
            raise HTTPException(422, "O modelo LTX nao oferece este formato dentro dos limites publicados. Escolha outro provedor.")
        payload = {"model": model["slug"], "prompt": prompt, "width": width, "height": height,
                   "seed": secrets.randbelow(2**31), "frames": defaults.get("frames", 120),
                   "fps": defaults.get("fps", 30)}
        for field in ("steps", "guidance"):
            if info.get("features", {}).get("supports_" + field):
                if field not in defaults:
                    raise HTTPException(422, "O deAPI nao informou os parametros padrao do modelo. Nenhuma geracao foi enviada.")
                payload[field] = defaults[field]
        for field in ("frames", "fps"):
            if not limits.get("min_" + field, 1) <= payload[field] <= limits.get("max_" + field, 10000):
                raise HTTPException(422, "Os parametros do modelo deAPI mudaram. Nenhuma geracao foi enviada.")
        image = base64.b64decode(image_b64, validate=True)
        with Image.open(io.BytesIO(image)) as photo:
            if min(photo.size) < 64 or len(image) < 1024:
                raise HTTPException(400, "Para o deAPI, use uma foto de ao menos 64 px por lado e 1 KB apos preparacao.")
        boundary = secrets.token_hex(16)
        body = b""
        for field, value in payload.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="first_frame_image"; filename="produto.jpg"\r\n'
                 'Content-Type: image/jpeg\r\n\r\n').encode() + image + f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(PROVIDERS["deapi_ltx"]["endpoint"], data=body,
                                     headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as response:
            request_id = json.loads(response.read(1024 * 1024))["data"]["request_id"]
        for _ in range(60):
            req = urllib.request.Request(f"https://api.deapi.ai/api/v2/jobs/{quote(str(request_id), safe='')}", headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                job = json.loads(response.read(1024 * 1024))["data"]
            if job.get("status") == "done":
                return download_video(job.get("result_url", ""))
            if job.get("status") == "error":
                raise HTTPException(502, "O deAPI informou falha na geracao. Confira o pedido e o saldo no painel do provedor.")
            time.sleep(5)
        raise HTTPException(504, "O deAPI ainda nao terminou. Consulte seu painel antes de tentar novamente para evitar nova cobranca.")
    except HTTPException:
        raise
    except urllib.error.HTTPError as error:
        error.close()
        messages = {
            400: "deAPI recusou a requisicao. Verifique imagem e prompt.",
            401: "Chave deAPI invalida. Confira em deapi.ai.",
            402: "Creditos deAPI esgotados. Adicione fundos ou use outro provedor.",
            403: "deAPI recusou o acesso. Confira as permissoes da chave e a lista de IPs permitidos.",
            422: "deAPI recusou os parametros, modelo ou saldo. Confira o painel do provedor.",
            429: "Rate limit deAPI atingido. Aguarde.",
        }
        raise HTTPException(502, messages.get(error.code, f"Erro deAPI: HTTP {error.code}")) from None
    except (socket.timeout, TimeoutError, urllib.error.URLError):
        raise HTTPException(504, "Comunicacao com deAPI interrompida. Consulte o painel do provedor antes de tentar novamente para evitar nova cobranca.") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(502, "O deAPI retornou uma resposta invalida.") from None


# === fal.ai Stable Video Diffusion ===
def request_video_fal(image_b64, prompt, aspect_ratio, api_key):
    # fal.ai usa queue system - submit + poll
    payload = {
        "image_url": f"data:image/jpeg;base64,{image_b64}",
        "motion_bucket_id": 127,
        "fps": 6,
        "cond_aug": 0.02,
    }
    # Submit
    request = urllib.request.Request(
        PROVIDERS["fal_svd"]["endpoint"],
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Key {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            submit_result = json.loads(response.read())
        request_id = submit_result.get("request_id")
        if not request_id:
            raise HTTPException(502, f"fal.ai nao retornou request_id: {json.dumps(submit_result)[:200]}")

        # Poll for result
        import time
        status_url = f"{PROVIDERS['fal_svd']['endpoint']}/requests/{quote(str(request_id), safe='')}/status"
        result_url = f"{PROVIDERS['fal_svd']['endpoint']}/requests/{quote(str(request_id), safe='')}"
        for _ in range(60):  # Max 5 min polling
            time.sleep(5)
            status_req = urllib.request.Request(
                status_url,
                headers={"Authorization": f"Key {api_key}"},
            )
            with urllib.request.urlopen(status_req, timeout=15) as sr:
                status_data = json.loads(sr.read())
            if status_data.get("status") == "COMPLETED":
                break
            elif status_data.get("status") == "FAILED":
                raise HTTPException(502, f"fal.ai falhou: {status_data.get('error', 'unknown')}")
        else:
            raise HTTPException(504, "Timeout aguardando fal.ai (5 min).")

        # Get result
        result_req = urllib.request.Request(
            result_url,
            headers={"Authorization": f"Key {api_key}"},
        )
        with urllib.request.urlopen(result_req, timeout=30) as rr:
            result_data = json.loads(rr.read())

        video_url = result_data.get("video", {}).get("url")
        if not video_url:
            raise HTTPException(502, f"fal.ai nao retornou URL de video: {json.dumps(result_data)[:200]}")

        return download_video(video_url)

    except HTTPException:
        raise
    except urllib.error.HTTPError as error:
        error.close()
        messages = {
            400: "fal.ai recusou a requisicao. Verifique imagem.",
            401: "Chave fal.ai invalida. Confira em fal.ai/dashboard.",
            402: "Creditos fal.ai esgotados.",
            429: "Rate limit fal.ai atingido.",
        }
        raise HTTPException(502, messages.get(error.code, f"Erro fal.ai: HTTP {error.code}")) from None
    except (socket.timeout, TimeoutError, urllib.error.URLError):
        raise HTTPException(504, "Timeout na comunicacao com fal.ai.") from None
    except (ValueError, TypeError, AttributeError, KeyError):
        raise HTTPException(502, "A fal.ai retornou uma resposta invalida.") from None


# === DISPATCHER: escolhe o provedor certo ===
def request_video(provider, image_b64, prompt, aspect_ratio, api_key):
    dispatchers = {
        "gemini_free": request_video_gemini_free,
        "gemini_paid": request_video_gemini_paid,
        "deapi_ltx": request_video_deapi,
        "fal_svd": request_video_fal,
    }
    fn = dispatchers.get(provider)
    if not fn:
        raise HTTPException(400, f"Provedor desconhecido: {provider}. Use: {', '.join(dispatchers.keys())}")
    return fn(image_b64, prompt, aspect_ratio, api_key)


@router.post("/generate")
def generate_product_video(
    request: Request,
    file: UploadFile = File(...),
    product_name: str = Form(""),
    scene: str = Form(...),
    aspect_ratio: str = Form("9:16"),
    api_key: str = Form(""),
    provider: str = Form("deapi_ltx"),
):
    origin = request.headers.get("origin")
    if origin and origin != expected_origin(request):
        raise HTTPException(403, "Inicie a geracao pelo proprio CADS.")

    product_name, scene = product_name.strip() or "produto da foto", scene.strip()
    if not 1 <= len(product_name) <= 120 or not 10 <= len(scene) <= 2000:
        raise HTTPException(400, "Informe o produto (ate 120 chars) e uma cena de 10 a 2.000 chars.")
    if aspect_ratio not in {"9:16", "16:9"}:
        raise HTTPException(400, "Escolha o formato vertical ou horizontal.")
    if provider not in PROVIDERS:
        raise HTTPException(400, f"Provedor invalido: {provider}. Opcoes: {', '.join(PROVIDERS.keys())}")
    if not PROVIDERS[provider]["video_capable"]:
        raise HTTPException(400, "Este provedor nao gera videos. Selecione deAPI, Google pago ou fal.ai.")

    # Resolver API key
    provider_info = PROVIDERS[provider]
    env_key_name = provider_info["env_key"]
    key = api_key.strip() or os.environ.get(env_key_name, "").strip()
    if not key:
        raise HTTPException(503,
            f"Adicione sua chave do {provider_info['name']} em Conectar IA. "
            f"Variavel de ambiente: {env_key_name}"
        )
    if len(key) > 512 or any(c.isspace() for c in key):
        raise HTTPException(400, "Confira a chave da API.")

    content = file.file.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "A foto deve ter no maximo 10 MB.")

    image = prepare_image(content)

    if not GENERATION_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Ja existe uma geracao em andamento. Aguarde.")

    try:
        prompt = (
            f"Crie um video curto de apresentacao do produto {product_name}, usando a foto como primeiro quadro e referencia. "
            "Preserve o formato, as cores, a embalagem, a marca e os detalhes reais do produto. "
            "Nao invente propriedades, precos, ofertas ou resultados do produto. "
            f"Direcao criativa: {scene}"
        )
        video = request_video(provider, image, prompt, aspect_ratio, key)
        if not isinstance(video, bytes) or len(video) > MAX_RESPONSE_BYTES or len(video) < 12 or video[4:8] != b"ftyp":
            raise HTTPException(502, "O provedor nao retornou um MP4 valido. Nenhum arquivo foi salvo.")
        filename = f"cads_ai_produto_{secrets.token_hex(8)}.mp4"
        path = customer_processed(PROCESSED_DIR) / filename
        try:
            with path.open("xb") as destination:
                destination.write(video)
        except FileExistsError:
            raise HTTPException(500, "Nao foi possivel criar nome unico para o video.") from None
        except OSError:
            path.unlink(missing_ok=True)
            raise HTTPException(507, "Video gerado mas nao salvo. Confira espaco em disco.") from None
        return {
            "status": "success",
            "filename": filename,
            "download_url": f"/api/download/{filename}",
            "model": provider_info["model"],
            "provider": provider_info["name"],
            "cost": provider_info["cost"],
        }
    finally:
        GENERATION_LOCK.release()
