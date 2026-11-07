"""Local-media renderer. No shell invocation or user-supplied filter expressions.

Every final output has an adjacent, hash-bound ``.render.json`` manifest. Branding
and burned captions require a measurable pixel change between a baseline and an
overlay pass. This proves a visible rendering effect, not semantic identity,
caption transcription accuracy, or human lip synchronization.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any


class MediaError(RuntimeError):
    """Invalid media, failed encoder, or bounded subprocess timeout."""


MAX_MEDIA_BYTES = 512 * 1024 * 1024
MAX_SOURCE_PIXELS = 16_777_216
MAX_OUTPUT_PIXELS = 8_294_400  # 3840 × 2160, including portrait orientation.


def _input_args(path: str | Path) -> list[str]:
    """Validate magic bytes before FFmpeg, then force a safe non-playlist demuxer.

    An extension or protocol whitelist alone is insufficient: a renamed concat
    or HLS playlist can still reference local files. MOV external data references
    are disabled explicitly in addition to restricting accepted demuxers.
    """
    source = Path(path).resolve(strict=True)
    if not source.is_file() or not 1 <= source.stat().st_size <= MAX_MEDIA_BYTES:
        raise MediaError("Medya normal bir dosya olmalı ve 512 MiB sınırını aşmamalı.")
    with source.open("rb") as stream:
        header = stream.read(32)
    demuxer = ""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        demuxer = "png_pipe"
    elif header.startswith(b"\xff\xd8\xff"):
        demuxer = "jpeg_pipe"
    elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        demuxer = "webp_pipe"
    elif header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        demuxer = "wav"
    elif header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        demuxer = "avi"
    elif header.startswith(b"\x1a\x45\xdf\xa3"):
        demuxer = "matroska"
    elif header.startswith(b"OggS"):
        demuxer = "ogg"
    elif header.startswith(b"fLaC"):
        demuxer = "flac"
    elif header.startswith(b"ID3"):
        demuxer = "mp3"
    elif len(header) > 1 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0:
        demuxer = "aac" if header[1] & 0xF6 == 0xF0 else "mp3"
    elif len(header) >= 12 and header[4:8] in {b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"}:
        demuxer = "mov"
    if not demuxer:
        raise MediaError("Medya imzası desteklenmiyor; oynatma listesi ve harici dosya referansları kabul edilmez.")
    accepted = {"mov": "mov,mp4,m4a,3gp,3g2,mj2", "matroska": "matroska,webm"}.get(demuxer, demuxer)
    args = ["-protocol_whitelist", "file", "-f", demuxer, "-format_whitelist", accepted,
            "-probesize", "10485760", "-analyzeduration", "10000000", "-max_pixels", str(MAX_SOURCE_PIXELS)]
    if demuxer == "mov":
        args += ["-enable_drefs", "0", "-use_absolute_path", "0"]
    return args


def _run(args: list[str], *, cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, check=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise MediaError("FFmpeg/ffprobe kurulumu bulunamadı.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"Medya işlemi {timeout} saniyelik zaman sınırını aştı.") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace")[-2400:]
        raise MediaError(f"Medya işlemi başarısız: {detail}") from exc


def _ffmpeg(*args: str, cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    hardened: list[str] = []
    for index, argument in enumerate(args):
        if argument == "-i" and index + 1 < len(args):
            candidate = Path(args[index + 1])
            if not candidate.is_absolute() and cwd:
                candidate = cwd / candidate
            if candidate.is_file():
                hardened.extend(_input_args(candidate))
        hardened.append(argument)
    if hardened and hardened[-1].endswith(".mp4"):
        hardened[-1:-1] = ["-fs", str(MAX_MEDIA_BYTES)]
    return _run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-max_alloc", "268435456",
                 "-threads", "2", "-filter_threads", "1", "-filter_complex_threads", "1", *hardened],
                cwd=cwd, timeout=timeout)


def _probe(path: str | Path) -> dict:
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_file():
        raise MediaError("Medya yolu normal bir dosya olmalıdır.")
    result = _run(["ffprobe", "-v", "error", "-max_alloc", "268435456", *_input_args(resolved),
                   "-show_streams", "-show_format", "-of", "json", str(resolved)], timeout=45)
    try:
        info = json.loads(result.stdout)
        if len(info.get("streams", [])) > 8:
            raise MediaError("Medya en fazla 8 akış içerebilir.")
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "video":
                if int(stream.get("width", 0)) * int(stream.get("height", 0)) > MAX_SOURCE_PIXELS:
                    raise MediaError("Kaynak görüntü 16 megapiksel sınırını aşıyor.")
            if stream.get("codec_type") == "audio" and int(stream.get("channels", 0)) > 8:
                raise MediaError("Kaynak ses en fazla 8 kanal içerebilir.")
        return info
    except (ValueError, UnicodeDecodeError) as exc:
        raise MediaError("Medya bilgileri okunamadı.") from exc


def _stream(info: dict, kind: str) -> dict | None:
    return next((s for s in info.get("streams", []) if s.get("codec_type") == kind), None)


def _duration(info: dict) -> float:
    values = [info.get("format", {}).get("duration")]
    values += [stream.get("duration") for stream in info.get("streams", [])]
    for value in values:
        try:
            duration = float(value)
            if math.isfinite(duration) and duration > 0:
                return duration
        except (TypeError, ValueError):
            continue
    raise MediaError("Medya süresi belirlenemedi.")


def inspect_media(path: str | Path) -> dict:
    """Return validated timing/dimensions before pipeline choices or API upload."""
    info = _probe(path)
    video = _stream(info, "video") or {}
    return {"duration": _duration(info), "width": int(video.get("width", 0)),
            "height": int(video.get("height", 0)), "has_audio": bool(_stream(info, "audio")),
            "has_video": bool(video)}


def extract_audio(input_path: str | Path, output_path: str | Path, duration: float | None = None,
                  trim_start: float = 0, speed: float = 1) -> dict:
    """Extract bounded mono speech audio for STT, including large Drive videos.

    M4A at 48 kbit/s keeps a 10-minute clip well below the 25 MiB transcription
    limit. WAV and MP3 are also accepted; no network or playlist inputs are read.
    """
    info = inspect_media(input_path)
    if not info["has_audio"]:
        raise MediaError("Kaynak medyada çıkarılabilecek ses akışı yok.")
    start = _number(trim_start, 0, 0, info["duration"], "Ses kırpma başlangıcı")
    speed = _number(speed, 1, .5, 2, "Ses oynatma hızı")
    available = (info["duration"] - start) / speed
    target_duration = _number(duration, min(available, 600), .1, 600, "Çıkarılacak ses süresi")
    target_duration = min(target_duration, available)
    if target_duration < .1:
        raise MediaError("Çıkarılabilecek ses süresi yetersiz.")
    output = Path(output_path).resolve()
    if Path(input_path).resolve() == output:
        raise MediaError("Ses kaynağı ve çıktı aynı dosya olamaz.")
    if output.suffix.lower() not in {".m4a", ".mp3", ".wav"}:
        raise MediaError("STT ses çıktısı M4A, MP3 veya WAV olmalı.")
    codec = {".m4a": "aac", ".mp3": "libmp3lame", ".wav": "pcm_s16le"}[output.suffix.lower()]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="extract-", dir=output.parent) as temporary:
        work = Path(temporary)
        source = _copy_asset(input_path, work, "source", VIDEO_TYPES | AUDIO_TYPES)
        target = work / f"speech{output.suffix.lower()}"
        extra = [] if codec == "pcm_s16le" else ["-b:a", "48k"]
        _ffmpeg("-ss", str(start), "-i", source.name, "-vn", "-af", f"{_atempo(speed)},atrim=duration={target_duration}",
                "-t", str(target_duration), "-ar", "16000", "-ac", "1", "-c:a", codec, *extra,
                "-fs", str(25 * 1024 * 1024), target.name, cwd=work, timeout=180)
        if target.stat().st_size >= 25 * 1024 * 1024:
            raise MediaError("STT ses çıktısı 25 MiB sınırını aşıyor.")
        measured = _duration(_probe(target))
        if abs(measured - target_duration) > .25:
            raise MediaError("Çıkarılan sesin süresi beklenen aralıkta değil.")
        os.replace(target, output)
    return {"path": str(output), "duration": measured, "sample_rate": 16000, "channels": 1,
            "source_duration": info["duration"], "trim_start": start, "speed": speed}


def _number(value: Any, default: float, minimum: float, maximum: float, name: str) -> float:
    try:
        number = float(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise MediaError(f"{name} sayısal olmalıdır.") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise MediaError(f"{name}, {minimum} ile {maximum} arasında olmalıdır.")
    return number


def _dimensions(settings: dict) -> tuple[int, int]:
    width = int(_number(settings.get("width"), 1080, 64, 3840, "Genişlik"))
    height = int(_number(settings.get("height"), 1920, 64, 3840, "Yükseklik"))
    if width % 2 or height % 2:
        raise MediaError("Video genişliği ve yüksekliği çift sayı olmalıdır.")
    if width * height > MAX_OUTPUT_PIXELS:
        raise MediaError("Çıktı çözünürlüğü 3840 × 2160 piksel alanını aşmamalı.")
    return width, height


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".render.json")


def _copy_asset(path: str | Path, directory: Path, stem: str, allowed: set[str]) -> Path:
    source = Path(path).resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in allowed:
        raise MediaError(f"Desteklenmeyen medya dosyası: {source.suffix}")
    cap = 1024 * 1024 if source.suffix.lower() == ".srt" else MAX_MEDIA_BYTES
    if source.stat().st_size > cap:
        raise MediaError("Medya dosyası işleme boyut sınırını aşıyor.")
    target = directory / f"{stem}{source.suffix.lower()}"
    shutil.copyfile(source, target)
    return target


VIDEO_TYPES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
AUDIO_TYPES = {".mp3", ".wav", ".aac", ".m4a", ".ogg", ".flac", ".opus"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-threads", "2"]


def generate_mock_video(output_path: str | Path, duration: float = 6, width: int = 360,
                        height: int = 640, color: str = "0x19324a", **_: Any) -> dict:
    """Generate actual moving test footage and a tone; visibly labeled mock."""
    width, height = _dimensions({"width": width, "height": height})
    duration = _number(duration, 6, 0.25, 600, "Süre")
    if not re.fullmatch(r"(?:0x|#)?[a-fA-F0-9]{6}", color):
        raise MediaError("Mock renk altı haneli HEX olmalıdır.")
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mock-", dir=output.parent) as temporary:
        work = Path(temporary)
        (work / "label.txt").write_text("MOCK / TEST VİDEOSU", encoding="utf-8")
        _ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", str(duration), "-vf",
                f"drawbox=x=0:y=0:w=iw:h=70:color={color}:t=fill,"
                f"drawtext=textfile=label.txt:expansion=none:fontsize={max(12, width // 22)}:fontcolor=white:x=12:y=22",
                *ENCODE, "-c:a", "aac", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ar", "48000", "-movflags", "+faststart", "output.mp4", cwd=work)
        os.replace(work / "output.mp4", output)
    return {"path": str(output), "duration": duration, "width": width, "height": height, "mock": True}


def _timestamp(value: float) -> str:
    milliseconds = round(value * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def write_subtitles(text: str, duration: float, path: str | Path,
                    segments: list[dict] | None = None) -> dict:
    """Write SRT. Provider segments are preferred; interpolation is explicit."""
    duration = _number(duration, 6, 0.1, 7200, "Altyazı süresi")
    timing = "provider_timed" if segments is not None else "estimated_word_distribution"
    clean_segments: list[dict] = []
    if segments is None:
        words = text.split()
        if not words:
            raise MediaError("Altyazı metni boş olamaz.")
        chunks = [words[index:index + 7] for index in range(0, len(words), 7)]
        elapsed = 0
        for chunk in chunks:
            start = duration * elapsed / len(words)
            elapsed += len(chunk)
            clean_segments.append({"start": start, "end": duration * elapsed / len(words), "text": " ".join(chunk)})
    else:
        previous_end = 0.0
        for segment in segments:
            start = _number(segment.get("start"), 0, 0, duration, "Altyazı başlangıcı")
            end = _number(segment.get("end"), duration, 0, duration + 0.05, "Altyazı bitişi")
            content = " ".join(str(segment.get("text", "")).split())
            if not content or end <= start or start < previous_end - 0.001:
                raise MediaError("Altyazı segmentleri boş, çakışan veya ters zamanlı olamaz.")
            clean_segments.append({"start": start, "end": min(end, duration), "text": content})
            previous_end = end
        if not clean_segments:
            raise MediaError("Altyazı segmentleri boş olamaz.")
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    blocks = [f"{index}\n{_timestamp(segment['start'])} --> {_timestamp(segment['end'])}\n{segment['text']}\n"
              for index, segment in enumerate(clean_segments, 1)]
    output.write_text("\n".join(blocks), encoding="utf-8")
    result = {"path": str(output), "timing": timing, "segments": clean_segments}
    output.with_suffix(output.suffix + ".timing.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _atempo(speed: float) -> str:
    return f"atempo={speed:.8f}"  # Validated range 0.5–2; no arbitrary expression.


def _normalize_clip(source: Path, target: Path, work: Path, width: int, height: int,
                    duration: float, *, start: float = 0, speed: float = 1,
                    voice: Path | None = None, music: Path | None = None,
                    settings: dict | None = None) -> None:
    options = settings or {}
    info = _probe(source)
    if not _stream(info, "video"):
        raise MediaError("Kaynakta video akışı bulunamadı.")
    inputs = ["-protocol_whitelist", "file,pipe", "-i", source.name]
    graph = [f"[0:v]trim=start={start}:duration={duration * speed},setpts=(PTS-STARTPTS)/{speed},"
             f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
             "setsar=1,fps=30,format=yuv420p[v]"]
    audio_labels: list[str] = []
    if _stream(info, "audio"):
        volume = _number(options.get("original_audio_volume"), 0.12 if voice else 1, 0, 2, "Kaynak ses seviyesi")
        graph.append(f"[0:a]atrim=start={start}:duration={duration * speed},asetpts=PTS-STARTPTS,"
                     f"{_atempo(speed)},volume={volume},aresample=48000,apad,atrim=duration={duration}[a0]")
        audio_labels.append("[a0]")
    index = 1
    for kind, asset, default_volume in [("voice", voice, 1), ("music", music, 0.12)]:
        if not asset:
            continue
        if not _stream(_probe(asset), "audio"):
            raise MediaError(f"{kind} dosyasında ses akışı yok.")
        if kind == "music":
            inputs += ["-stream_loop", "-1"]
        inputs += ["-protocol_whitelist", "file,pipe", "-i", asset.name]
        volume = _number(options.get(f"{kind}_volume"), default_volume, 0, 2, f"{kind} ses seviyesi")
        pitch_filter = ""
        if kind == "voice":
            semitones = _number((options.get("voice") or {}).get("pitch"), 0, -12, 12, "Ses perdesi (yarım ton)")
            if semitones:
                factor = 2 ** (semitones / 12)
                changed_rate = round(48000 * factor)
                compensation = 48000 / changed_rate
                pitch_filter = f"asetrate={changed_rate},aresample=48000,{_atempo(compensation)},"
        graph.append(f"[{index}:a]asetpts=PTS-STARTPTS,volume={volume},aresample=48000,"
                     f"{pitch_filter}apad,atrim=duration={duration}[a{index}]")
        audio_labels.append(f"[a{index}]")
        index += 1
    if audio_labels:
        graph.append("".join(audio_labels) + f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0,"
                     f"atrim=duration={duration}[a]")
    else:
        graph.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration}[a]")
    _ffmpeg(*inputs, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]", "-t", str(duration),
            *ENCODE, "-c:a", "aac", "-ar", "48000", "-ac", "2", target.name, cwd=work)


def _pixels(path: Path, time: float, width: int, height: int, region: tuple[int, int, int, int] | None = None) -> bytes:
    region = region or (0, 0, width, height)
    x, y, crop_width, crop_height = region
    result = _ffmpeg("-ss", str(time), "-i", str(path), "-frames:v", "1", "-vf",
                     f"crop={crop_width}:{crop_height}:{x}:{y},scale=160:160,format=rgb24",
                     "-f", "rawvideo", "pipe:1", timeout=45)
    if len(result.stdout) != 160 * 160 * 3:
        raise MediaError("Görünür öğe kontrolü için video karesi çıkarılamadı.")
    return result.stdout


def _visual_evidence(before: Path, after: Path, time: float, width: int, height: int,
                     region: tuple[int, int, int, int] | None = None) -> dict:
    baseline, branded = _pixels(before, time, width, height, region), _pixels(after, time, width, height, region)
    differences = [abs(a - b) for a, b in zip(baseline, branded)]
    changed = sum(difference > 18 for difference in differences) / len(differences)
    mean = sum(differences) / len(differences)
    return {"method": "decoded_frame_pixel_difference", "sample_time": round(time, 3),
            "region": list(region or (0, 0, width, height)), "mean_absolute_difference": round(mean, 4),
            "changed_channel_fraction": round(changed, 5), "visible_effect": changed >= 0.003 and mean >= 0.2,
            "semantic_identity_verified": False}


def _subtitle_sample(path: Path, duration: float) -> float:
    content = path.read_text(encoding="utf-8-sig")
    matches = re.findall(r"(\d+):(\d+):(\d+),(\d+)\s+-->\s+(\d+):(\d+):(\d+),(\d+)", content)
    if not matches:
        raise MediaError("Altyazı dosyası geçerli SRT zamanlaması içermiyor.")
    for values in matches:
        h, m, s, ms, h2, m2, s2, ms2 = map(int, values)
        start, end = h * 3600 + m * 60 + s + ms / 1000, h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
        if start < end and start < duration:
            return min((start + end) / 2, duration - 0.06)
    raise MediaError("Altyazı zamanlaması video süresinin dışında.")


def _burn_subtitles(before: Path, target: Path, subtitle: Path, work: Path,
                    width: int, height: int, duration: float, style: dict) -> dict:
    size = int(_number(style.get("font_size"), max(14, round(height * .029)), 10, 120, "Altyazı yazı boyutu"))
    margin = int(_number(style.get("margin_bottom"), 35, 0, height // 2, "Altyazı alt boşluğu"))
    outline = int(_number(style.get("outline"), 2, 0, 8, "Altyazı dış çizgisi"))
    color = str(style.get("color", "FFFFFF")).lstrip("#")
    if not re.fullmatch(r"[A-Fa-f0-9]{6}", color):
        raise MediaError("Altyazı rengi altı haneli HEX olmalıdır.")
    bgr = color[4:6] + color[2:4] + color[0:2]
    # SRT's implicit ASS canvas is 384×288, which makes a UI value such as 56 px
    # enormous on 1080×1920 output. Author a target-sized ASS canvas explicitly.
    ass = work / "captions.ass"
    script = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
              "WrapStyle: 0", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
              f"Style: Default,DejaVu Sans,{size},&H00{bgr},&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{outline},1,2,{max(8, width // 20)},{max(8, width // 20)},{margin},1",
              "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    content = subtitle.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    events = 0
    for block in re.split(r"\n\s*\n", content.strip()):
        match = re.search(r"(\d+):(\d+):(\d+),(\d+)\s+-->\s+(\d+):(\d+):(\d+),(\d+)[^\n]*\n(.+)", block, re.S)
        if not match:
            raise MediaError("Altyazı dosyası geçerli SRT blokları içermiyor.")
        h, m, s, ms, h2, m2, s2, ms2 = map(int, match.groups()[:8])
        start = h * 3600 + m * 60 + s + ms / 1000
        end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
        if not 0 <= start < end <= duration + .05:
            raise MediaError("Altyazı zaman aralığı video süresinin dışında.")
        # Override tags cannot move text, load fonts, or conceal the caption.
        text = match.group(9).strip().replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", "\\N")
        ass_start = f"{h}:{m:02}:{s:02}.{ms // 10:02}"
        ass_end = f"{h2}:{m2:02}:{s2:02}.{ms2 // 10:02}"
        script.append(f"Dialogue: 0,{ass_start},{ass_end},Default,,0,0,0,,{text}")
        events += 1
    if not events:
        raise MediaError("Altyazı boş olamaz.")
    ass.write_text("\n".join(script) + "\n", encoding="utf-8")
    _ffmpeg("-i", before.name, "-vf", f"ass=filename={ass.name}",
            *ENCODE, "-c:a", "copy", target.name, cwd=work)
    control = work / "subtitle-control.mp4"
    _ffmpeg("-i", before.name, *ENCODE, "-c:a", "copy", control.name, cwd=work)
    sample = _subtitle_sample(subtitle, duration)
    evidence = _visual_evidence(control, target, sample, width, height)
    evidence["source_sha256"] = _hash(subtitle)
    return evidence


def _burn_text(before: Path, target: Path, text: str, work: Path, width: int) -> None:
    if len(text) > 300:
        raise MediaError("Üst yazı en fazla 300 karakter olabilir.")
    # Text is read from a file with expansion disabled: %{...}, colons and quotes
    # are literal text and can never become a filter expression or shell code.
    (work / "overlay.txt").write_text(text, encoding="utf-8")
    _ffmpeg("-i", before.name, "-vf", f"drawtext=textfile=overlay.txt:expansion=none:"
            f"fontsize={max(12, width // 24)}:fontcolor=white:box=1:boxcolor=black@0.55:"
            "boxborderw=8:x=(w-text_w)/2:y=h*0.10", *ENCODE, "-c:a", "copy", target.name, cwd=work)


def _combine(clips: list[tuple[Path, float]], target: Path, work: Path, transition: float) -> float:
    if len(clips) == 1:
        shutil.copyfile(clips[0][0], target)
        return clips[0][1]
    inputs: list[str] = []
    for clip, _ in clips:
        inputs += ["-i", clip.name]
    graph = []
    if transition <= 0:
        streams = "".join(f"[{index}:v][{index}:a]" for index in range(len(clips)))
        graph.append(f"{streams}concat=n={len(clips)}:v=1:a=1[v][a]")
        total = sum(duration for _, duration in clips)
    else:
        if any(duration <= transition * 2 for _, duration in clips):
            raise MediaError("Geçiş süresi her klibin yarısından kısa olmalıdır.")
        current_v, current_a, total = "0:v", "0:a", clips[0][1]
        for index in range(1, len(clips)):
            next_v, next_a = f"v{index}", f"a{index}"
            graph.append(f"[{current_v}][{index}:v]xfade=transition=fade:duration={transition}:offset={total - transition}[{next_v}]")
            graph.append(f"[{current_a}][{index}:a]acrossfade=d={transition}[{next_a}]")
            current_v, current_a = next_v, next_a
            total += clips[index][1] - transition
        graph += [f"[{current_v}]null[v]", f"[{current_a}]anull[a]"]
    _ffmpeg(*inputs, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]",
            *ENCODE, "-c:a", "aac", "-ar", "48000", target.name, cwd=work)
    return total


def _burn_logo(before: Path, target: Path, logo: Path, work: Path, width: int, height: int,
               duration: float, branding: dict, content_start: float, content_end: float) -> dict:
    info = _probe(logo)
    stream = _stream(info, "video")
    if not stream:
        raise MediaError("Logo dosyası çözümlenemedi.")
    logo_width = int(_number(branding.get("width"), width * .17, 8, width * .7, "Logo genişliği"))
    logo_height = max(1, round(logo_width * int(stream["height"]) / int(stream["width"])))
    margin = int(_number(branding.get("margin"), min(24, width // 20), 0, min(width, height) // 3, "Logo kenar boşluğu"))
    opacity = _number(branding.get("opacity"), .85, .01, 1, "Logo opaklığı")
    if logo_width + margin * 2 > width or logo_height + margin * 2 > height:
        raise MediaError("Logo ayarlanan video boyutuna sığmıyor.")
    positions = {"top-left": (margin, margin), "top-right": (width - logo_width - margin, margin),
                 "bottom-left": (margin, height - logo_height - margin),
                 "bottom-right": (width - logo_width - margin, height - logo_height - margin),
                 "center": ((width - logo_width) // 2, (height - logo_height) // 2)}
    position = str(branding.get("position", "top-right")).replace("_", "-")
    if position not in positions:
        raise MediaError("Geçersiz logo konumu.")
    x, y = positions[position]
    start = 0 if branding.get("intro_placement", True) else content_start
    end = duration if branding.get("outro_placement", True) else content_end
    graph = f"[1:v]scale={logo_width}:{logo_height},format=rgba,colorchannelmixer=aa={opacity}[logo];"
    graph += f"[0:v][logo]overlay={x}:{y}:enable='between(t,{start},{end})':eof_action=repeat[v]"
    _ffmpeg("-i", before.name, "-i", logo.name, "-filter_complex", graph,
            "-map", "[v]", "-map", "0:a?", *ENCODE, "-c:a", "copy", target.name, cwd=work)
    control = work / "logo-control.mp4"
    _ffmpeg("-i", before.name, *ENCODE, "-c:a", "copy", control.name, cwd=work)
    sample = min(max((start + end) / 2, .05), duration - .06)
    evidence = _visual_evidence(control, target, sample, width, height, (x, y, logo_width, logo_height))
    evidence.update({"source_sha256": _hash(logo), "position": position, "opacity": opacity})
    return evidence


def _audio_is_silent(path: Path) -> bool:
    """Measure decoded samples before loudnorm; short digital silence can yield
    NaN samples in FFmpeg's dynamic loudnorm filter and break AAC encoding.
    """
    result = _run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-max_alloc", "268435456",
                   *_input_args(path), "-i", str(path), "-vn", "-af", "astats=metadata=0:reset=0",
                   "-f", "null", "-"], timeout=180)
    peaks = re.findall(r"Peak level dB:\s*(-?inf|[-+\d.]+)", result.stderr.decode(errors="replace"))
    if not peaks:
        raise MediaError("Ses normalizasyonundan önce ses örnekleri ölçülemedi.")
    return all(float(value) == float("-inf") for value in peaks)


def render_video(input_path: str | Path, output_path: str | Path, settings: dict,
                 voice_path: str | Path | None = None, subtitle_path: str | Path | None = None) -> dict:
    """Render vertical or horizontal H.264/AAC. All input filters are validated.

    ``duration`` is the main content duration after speed adjustment. Intro/outro
    durations are added, with overlapping crossfades subtracted. Sources shorter
    than requested fail explicitly so a provider can regenerate enough footage.
    """
    width, height = _dimensions(settings)
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if Path(input_path).resolve() == output:
        raise MediaError("Kaynak ve çıktı aynı dosya olamaz.")
    source_info = _probe(input_path)
    source_duration = _duration(source_info)
    if source_duration > 3600:
        raise MediaError("Kaynak video bir saatlik işleme sınırını aşıyor.")
    start = _number(settings.get("trim_start"), 0, 0, source_duration, "Kırpma başlangıcı")
    speed = _number(settings.get("speed"), 1, .5, 2, "Oynatma hızı")
    available = (source_duration - start) / speed
    duration = _number(settings.get("duration"), available, .1, 600, "Video süresi")
    if duration > available + .10:
        raise MediaError(f"Kaynak video kısa: gereken {duration:.2f}s, kullanılabilir {available:.2f}s.")
    duration = min(duration, available)
    manifest: dict[str, Any] = {"version": 1, "source_sha256": _hash(Path(input_path)),
                                "width": width, "height": height, "content_duration": duration,
                                "logo": None, "subtitles": None, "normalization_target_lufs": -16}
    with tempfile.TemporaryDirectory(prefix="render-", dir=output.parent) as temporary:
        work = Path(temporary)
        source = _copy_asset(input_path, work, "source", VIDEO_TYPES)
        voice = _copy_asset(voice_path, work, "voice", AUDIO_TYPES) if voice_path else None
        music = _copy_asset(settings["music_path"], work, "music", AUDIO_TYPES) if settings.get("music_path") else None
        main = work / "main.mp4"
        _normalize_clip(source, main, work, width, height, duration, start=start, speed=speed,
                        voice=voice, music=music, settings=settings)
        if settings.get("text_overlay"):
            next_main = work / "text.mp4"
            _burn_text(main, next_main, str(settings["text_overlay"]), work, width)
            main = next_main
        if subtitle_path:
            subtitle = _copy_asset(subtitle_path, work, "captions", {".srt"})
            next_main = work / "subtitles.mp4"
            manifest["subtitles"] = _burn_subtitles(main, next_main, subtitle, work, width, height,
                                                     duration, settings.get("subtitle") or {})
            main = next_main
        clips: list[tuple[Path, float]] = []
        intro_duration = 0.0
        outro_duration = 0.0
        for label in ["intro", "main", "outro"]:
            if label == "main":
                clips.append((main, duration))
            elif settings.get(f"{label}_path"):
                asset = _copy_asset(settings[f"{label}_path"], work, label, VIDEO_TYPES)
                clip_duration = _number(_duration(_probe(asset)), 2, .1, 30, f"{label} süresi")
                normalized = work / f"{label}-normalized.mp4"
                _normalize_clip(asset, normalized, work, width, height, clip_duration)
                clips.append((normalized, clip_duration))
                if label == "intro":
                    intro_duration = clip_duration
                else:
                    outro_duration = clip_duration
        transition = _number(settings.get("transition_seconds"), 0, 0, 2, "Geçiş süresi")
        combined = work / "combined.mp4"
        final_duration = _combine(clips, combined, work, transition)
        content_start = max(0, intro_duration - (transition if intro_duration else 0))
        content_end = content_start + duration
        if settings.get("logo_path"):
            logo = _copy_asset(settings["logo_path"], work, "logo", IMAGE_TYPES)
            branded = work / "branded.mp4"
            manifest["logo"] = _burn_logo(combined, branded, logo, work, width, height, final_duration,
                                          settings.get("branding") or {}, content_start, content_end)
            combined = branded
        # Normalize only audio in the final pass; the video pixels used as visual
        # evidence remain unchanged by this stream-copy operation.
        silent = _audio_is_silent(combined)
        normalization = ["-c:a", "copy"] if silent else ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                                                         "-c:a", "aac", "-ar", "48000", "-ac", "2"]
        _ffmpeg("-i", combined.name, "-map", "0:v", "-map", "0:a", "-c:v", "copy",
                *normalization,
                "-movflags", "+faststart", "final.mp4", cwd=work)
        manifest["audio_normalization"] = "skipped_digital_silence" if silent else "loudnorm"
        os.replace(work / "final.mp4", output)
    manifest.update({"output_sha256": _hash(output), "duration": final_duration,
                     "intro_duration": intro_duration, "outro_duration": outro_duration,
                     "transition_seconds": transition})
    _manifest_path(output).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(output), "duration": final_duration, "width": width, "height": height,
            "manifest": manifest}


def quality_check(path: str | Path, settings: dict) -> dict:
    """Decode the whole artifact and measure technical publishing gates.

    Lip-sync, factual correctness, logo identity and transcription correctness
    require a semantic reviewer and are explicitly outside this technical gate.
    """
    checks: dict[str, Any] = {}
    errors: list[str] = []
    limitations = ["Dudak senkronu ve anlamsal ses-görüntü uyumu otomatik doğrulanmaz.",
                   "Altyazı metninin doğruluğu ve logonun marka kimliği piksel kontrolüyle doğrulanmaz."]

    def check(name: str, passed: bool, **detail: Any) -> None:
        checks[name] = {"passed": bool(passed), **detail}
        if not passed:
            errors.append(name)

    try:
        output = Path(path).resolve(strict=True)
        check("file_nonempty", output.is_file() and output.stat().st_size > 1024)
        info = _probe(output)
        video = _stream(info, "video")
        audio = _stream(info, "audio")
        check("video_stream", video is not None)
        if not video:
            return {"passed": False, "checks": checks, "errors": errors, "limitations": limitations}
        duration = _duration(info)
        check("bounded_duration", duration <= 660, actual=duration, maximum=660)
        if duration > 660:
            return {"passed": False, "checks": checks, "errors": errors, "limitations": limitations}
        manifest = {}
        if _manifest_path(output).is_file():
            try:
                manifest = json.loads(_manifest_path(output).read_text(encoding="utf-8"))
            except (ValueError, OSError):
                manifest = {}
        manifest_valid = bool(manifest) and manifest.get("output_sha256") == _hash(output)
        if manifest:
            check("render_provenance", manifest_valid)
        expected_width, expected_height = _dimensions(settings)
        check("resolution", video.get("width") == expected_width and video.get("height") == expected_height,
              actual=[video.get("width"), video.get("height")], expected=[expected_width, expected_height])
        expected_duration = float(manifest["duration"]) if manifest_valid else float(settings.get("duration", duration))
        check("duration", abs(duration - expected_duration) <= max(.25, expected_duration * .03),
              actual=round(duration, 3), expected=round(expected_duration, 3))
        if manifest_valid and settings.get("duration") is not None:
            requested = float(settings["duration"])
            check("content_duration", abs(float(manifest["content_duration"]) - requested) <= max(.1, requested * .03),
                  actual=manifest["content_duration"], expected=requested)
        qa = settings.get("qa") or {}
        check("audio_stream", bool(audio) or not qa.get("require_audio", True))
        decode = _run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
                       "-max_alloc", "268435456", "-threads", "2", *_input_args(output), "-i", str(output),
                       "-map", "0:v", "-map", "0:a?", "-f", "null", "-"], timeout=300)
        check("full_decode", not decode.stderr.strip(), detail=decode.stderr.decode(errors="replace")[-400:])
        measure = _run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-threads", "2", "-filter_threads", "1",
                        "-max_alloc", "268435456", *_input_args(output), "-i", str(output), "-vf", "blackdetect=d=0.08:pix_th=0.10:pic_th=0.98",
                        "-an", "-f", "null", "-"], timeout=300)
        log = measure.stderr.decode(errors="replace")
        black_seconds = sum(float(number) for number in re.findall(r"black_duration:([\d.]+)", log))
        black_fraction = black_seconds / duration
        allowed_black = _number(qa.get("max_black_fraction"), .15, 0, 1, "Siyah ekran toleransı")
        check("black_screen", black_fraction <= allowed_black, fraction=round(black_fraction, 4), threshold=allowed_black)
        if audio:
            audio_duration = float(audio.get("duration", duration))
            video_duration = float(video.get("duration", duration))
            start_delta = abs(float(audio.get("start_time", 0)) - float(video.get("start_time", 0)))
            check("stream_timing", abs(audio_duration - video_duration) < .3 and start_delta < .15,
                  duration_delta=round(abs(audio_duration - video_duration), 3), start_delta=round(start_delta, 3),
                  scope="Akış zaman çizgisi; anlamsal dudak senkronu değildir.")
            loudness = _run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-max_alloc", "268435456",
                             *_input_args(output), "-i", str(output),
                             "-vn", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"], timeout=300)
            matches = re.findall(r'\{\s*"input_i".*?\}', loudness.stderr.decode(errors="replace"), re.S)
            if not matches:
                check("audio_loudness", False, detail="Ses yüksekliği ölçülemedi.")
            else:
                measured = json.loads(matches[-1])
                integrated = float(measured["input_i"])
                peak = float(measured["input_tp"])
                minimum = _number(qa.get("loudness_min"), -24, -60, -6, "Minimum LUFS")
                maximum = _number(qa.get("loudness_max"), -9, -24, 0, "Maksimum LUFS")
                optional_silence = not qa.get("require_audio", True) and integrated == float("-inf")
                check("audio_loudness", optional_silence or (math.isfinite(integrated) and minimum <= integrated <= maximum and peak <= .5),
                      integrated_lufs=integrated if math.isfinite(integrated) else None,
                      true_peak_dbtp=peak if math.isfinite(peak) else None,
                      skipped=optional_silence, detail="Bu hesapta sessiz video açıkça izinli." if optional_silence else "")
        for kind, required in [("logo", bool(settings.get("logo_path"))),
                               ("subtitles", bool((settings.get("subtitle") or {}).get("enabled", False)) or bool(manifest.get("subtitles")))]:
            if required:
                evidence = manifest.get(kind) or {}
                visible = manifest_valid and evidence.get("method") == "decoded_frame_pixel_difference" and evidence.get("visible_effect") is True
                if kind == "logo" and settings.get("logo_path"):
                    try:
                        visible = visible and evidence.get("source_sha256") == _hash(Path(settings["logo_path"]))
                    except OSError:
                        visible = False
                check(f"{kind}_visible_render", visible, evidence=evidence)
        if qa.get("require_metadata"):
            metadata = settings.get("metadata") or {}
            check("metadata", bool(metadata.get("title")) and bool(metadata.get("description")))
    except (MediaError, OSError, ValueError, TypeError) as exc:
        check("media_readable", False, detail=str(exc))
    return {"passed": not errors, "checks": checks, "errors": errors, "limitations": limitations}
