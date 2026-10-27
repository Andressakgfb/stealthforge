"""Small randomized media transforms; hash changes do not imply perceptual anonymity."""
import logging
import math
import os
import random
import re
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg


LEVELS = {"basica": (0.01, 0.005, 1, 0.003, 16, (24, 24)),
          "normal": (0.02, 0.012, 2, 0.008, 64, (25, 26)),
          "agressiva": (0.03, 0.02, 3, 0.012, 128, (27, 28))}
logger = logging.getLogger(__name__)


class InvalidMedia(ValueError):
    pass


def _run(args, probe=False):
    try:
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin",
             "-protocol_whitelist", "file,pipe", *args],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("FFmpeg timeout: %s", exc.stderr)
        raise
    if result.returncode and not probe:
        logger.error("FFmpeg failed (%s): %s", result.returncode, result.stderr)
        raise InvalidMedia("Nao foi possivel processar a midia.")
    return result.stderr


def _probe(path, kind):
    # imageio_ffmpeg ships FFmpeg, but does not ship ffprobe.
    info = _run(["-i", str(path)], probe=True)
    duration = re.search(r"^  Duration: (\d+):(\d+):(\d+\.\d+)", info, re.MULTILINE)
    stream = next((line for line in info.splitlines()
                   if line.lstrip().startswith("Stream #") and f"{kind}:" in line
                   and "attached pic" not in line), "")
    if not duration or not stream:
        logger.error("Invalid %s input: %s", kind, info)
        raise InvalidMedia("Arquivo de midia invalido ou sem duracao.")
    hours, minutes, seconds = map(float, duration.groups())
    seconds += hours * 3600 + minutes * 60
    if seconds <= 0:
        raise InvalidMedia("A midia deve ter duracao positiva.")
    return seconds, stream, info


def _signed(amount):
    return random.choice((-1, 1)) * amount


def _metadata():
    return ["-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1",
            "-metadata", "encoder=", "-metadata:s", "encoder=", "-fflags", "+bitexact",
            "-flags:v", "+bitexact", "-empty_hdlr_name", "1"]


def _finish(temp, output, salt):
    clean = str(Path(temp).with_name("clean" + Path(temp).suffix))
    # Encoders add their own stream tags after metadata mapping; strip them by remuxing.
    _run(["-y", "-i", temp, "-map", "0", "-c", "copy", *_metadata(), clean])
    with open(clean, "ab") as handle:
        handle.write(os.urandom(salt))
    os.replace(clean, output)


def cloak_audio(input_path, output_path, level="normal"):
    """Write AAC in an M4A container, preserving the input on every failure."""
    pitch, tempo, _, _, salt, _ = LEVELS[level]
    _, stream, _ = _probe(input_path, "Audio")
    rate = re.search(r"(\d+) Hz", stream)
    if not rate:
        raise InvalidMedia("Taxa de amostragem de audio invalida.")
    bitrate = re.search(r"(\d+) kb/s", stream)
    original_bitrate = int(bitrate[1]) if bitrate else None
    target = random.choice([b for b in (96, 128, 160, 192, 224) if b != original_bitrate])
    pitch = 1 + _signed(random.uniform(0.01, pitch))
    tempo = 1 + _signed(random.uniform(0.005, tempo))
    filters = (f"asetrate={int(rate[1])}*{pitch},"
               f"aresample=44100,atempo={tempo / pitch},"
               "aresample=osf=s16:dither_method=triangular")
    with tempfile.TemporaryDirectory(prefix="_tmp_cloak_", dir=Path(output_path).parent) as folder:
        temp = str(Path(folder) / "audio.m4a")
        _run(["-y", "-i", str(input_path), "-map", "0:a:0", "-vn", "-af", filters,
              "-c:a", "aac", "-b:a", f"{target}k", *_metadata(), temp])
        _finish(temp, output_path, salt)


def cloak_video(input_path, output_path, level="normal", cover_image_path=None):
    """Write H.264/MP4 with short cloned boundary frames and optional intro cover."""
    _, _, crop, jitter, salt, crf = LEVELS[level]
    duration, stream, info = _probe(input_path, "Video")
    dimensions = re.search(r"\b(\d{2,})x(\d{2,})\b", stream)
    if not dimensions:
        raise InvalidMedia("Dimensoes de video invalidas.")
    width, height = map(int, dimensions.groups())
    rotation = re.search(r"rotation of (-?[\d.]+) degrees", info)
    if rotation and round(float(rotation[1])) % 180 == 90:
        width, height = height, width
    if min(width, height) <= crop * 2 + 2:
        raise InvalidMedia("Video pequeno demais para o recorte.")
    width, height = width // 2 * 2, height // 2 * 2
    fps_match = re.search(r"([\d.]+) fps", stream)
    fps = max(float(fps_match[1]) if fps_match else 30, math.ceil(100 / duration))
    # Keep boundary frames below 3% even for short clips and low frame rates.
    pad = max(1, int(min(duration * 0.01, 0.1) * fps)) / fps
    scale = 1 + _signed(random.uniform(0.001, (crop / 3) * 0.01))
    scaled_w = max(2, round(width * scale / 2) * 2)
    scaled_h = max(2, round(height * scale / 2) * 2)
    vf = (f"crop=iw-{crop * 2}:ih-{crop * 2}:{crop}:{crop},"
          f"eq=brightness={_signed(jitter)}:contrast={1 + _signed(jitter)},"
          f"scale={scaled_w}:{scaled_h},pad=max(iw\\,{width}):max(ih\\,{height}):(ow-iw)/2:(oh-ih)/2,"
          f"crop={width}:{height},setsar=1,setpts=PTS-STARTPTS,fps={fps},"
          # Extra tail is trimmed by -t, covering frame-rate rounding at EOF.
          f"tpad=start_mode=clone:start_duration={pad}:stop_mode=clone:stop_duration={pad + 1}")
    args = ["-y", "-i", str(input_path)]
    graph = f"[0:v:0]{vf}[base]"
    if cover_image_path is not None:
        args += ["-loop", "1", "-i", str(cover_image_path)]
        graph += (f";[1:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                  f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[cover];"
                  f"[base][cover]overlay=enable='lt(t,{pad})':shortest=1[v]")
    else:
        graph += ";[base]null[v]"
    args += ["-filter_complex", graph, "-map", "[v]"]
    if re.search(r"^\s*Stream #.*Audio:", info, re.MULTILINE):
        args += ["-map", "0:a:0", "-af",
                 f"asetpts=PTS-STARTPTS,adelay={pad * 1000}:all=1,apad=pad_dur={pad}",
                 "-c:a", "aac", "-b:a", "128k"]
    args += ["-t", str(duration + 2 * pad), "-c:v", "libx264", "-preset", "fast",
             "-crf", str(random.randint(*crf)), "-pix_fmt", "yuv420p", *_metadata()]
    with tempfile.TemporaryDirectory(prefix="_tmp_cloak_", dir=Path(output_path).parent) as folder:
        temp = str(Path(folder) / "video.mp4")
        _run([*args, temp])
        _finish(temp, output_path, salt)
