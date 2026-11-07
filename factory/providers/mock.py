"""Offline providers. All generated outputs and responses are explicitly test data."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import wave
from pathlib import Path

from .base import ProviderError


def _id(text):
    return hashlib.sha256(str(text).encode()).hexdigest()[:24]


def _usage(operation):
    return {"provider": "mock", "model": "offline-test", "operation": operation,
            "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0, "actual_cost": 0, "mock": True}


class MockLLMProvider:
    def research(self, account, history):
        count = len(history)
        categories = account.get("settings", {}).get("categories") or ["Bilgi", "Karşılaştırma", "Tarih", "Soru"]
        niche = account.get("niche", "Genel")
        return {"ideas": [{"title": f"MOCK {niche}: {categories[i % len(categories)]} test fikri {count + i + 1}",
            "hook": "Bu bir deneme içeriğidir.", "topic": categories[i % len(categories)],
            "summary": "Çevrimdışı otomasyon testi; güncel haber veya gerçek araştırma değildir.",
            "target_audience": account.get("target_audience", ""), "estimated_duration": account.get("settings", {}).get("duration", 6),
            "novelty_score": 70, "relevance_score": 80, "virality_score": 50, "production_score": 95,
            "risk_score": 0, "predicted_performance": 50, "mock": True, "sources": []}
            for i in range(8)], "sources": [], "usage": _usage("research"), "mock": True}

    def script(self, idea, account):
        return {"text": "Bu içerik, otomasyon sisteminin görüntü, ses ve yayın kuyruğunu denemek için hazırlanmıştır.",
                "visual_prompt": str(idea.get("title", "Test videosu")), "safety": {"approved": True, "reason": "Test içeriği"},
                "usage": _usage("script"), "mock": True}

    def metadata(self, script, account):
        title = f"MOCK TEST • {account.get('name', 'İçerik')}"
        return {"youtube": {"title": title, "description": "Gerçek yayın yapılmayan test videosu.", "tags": ["mock"], "privacy_status": "private"},
                "instagram": {"caption": title + " #mock"}, "tiktok": {"caption": title + " #mock"},
                "usage": _usage("metadata"), "mock": True}

    def moderate(self, text):
        return {"flagged": False, "mock": True}


class MockVideoProvider:
    def generate(self, prompt, output_path, options=None):
        from factory.media.engine import generate_mock_video
        options = options or {}
        result = generate_mock_video(output_path, duration=options.get("duration", 6), width=options.get("width", 360), height=options.get("height", 640))
        cp = {"complete": True, "mock": True}
        if options.get("save_checkpoint"):
            options["save_checkpoint"](cp)
        return {**result, "status": "COMPLETED", "checkpoint": cp, "usage": _usage("video"), "mock": True}


class MockImageProvider:
    def generate(self, prompt, output_path, options=None):
        import subprocess
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x19324a:s=360x640", "-vf", "drawtext=text='MOCK TEST':fontcolor=white:fontsize=36:x=(w-tw)/2:y=(h-th)/2", "-frames:v", "1", "-threads", "1", str(output_path)], check=True, timeout=30)
        return {"status": "COMPLETED", "path": str(output_path), "mock": True, "usage": _usage("image")}


class MockVoiceProvider:
    def synthesize(self, text, output_path, options=None):
        options = options or {}
        duration = float(options.get("duration", 6))
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = 22050
        with wave.open(str(target), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            # Audible test tone, expressly not speech synthesis.
            audio.writeframes(b"".join(struct.pack("<h", int(4500 * math.sin(2 * math.pi * 440 * i / sample_rate))) for i in range(int(duration * sample_rate))))
        target.with_suffix(target.suffix + ".mock.json").write_text(json.dumps({"text": text, "duration": duration}, ensure_ascii=False), encoding="utf-8")
        return {"path": str(target), "status": "COMPLETED", "mock": True, "audio_type": "test_tone", "usage": _usage("voice")}

    def transcribe(self, path, options=None):
        sidecar = Path(path).with_suffix(Path(path).suffix + ".mock.json")
        data = json.loads(sidecar.read_text()) if sidecar.exists() else {"text": "Bu bir test videosudur.", "duration": 6}
        if options and options.get("duration"):
            data["duration"] = float(options["duration"])
        return {"text": data["text"], "segments": [{"start": 0, "end": data["duration"], "text": data["text"]}], "mock": True, "usage": _usage("transcription")}


class MockPublisher:
    def __init__(self, platform="youtube"):
        self.platform = platform

    def publish(self, video_path, metadata, credentials, checkpoint=None, save_checkpoint=None):
        cp = dict(checkpoint or {})
        key = metadata.get("idempotency_key") or str(video_path)
        remote_id = cp.get("remote_id") or "mock-" + _id(self.platform + key)
        cp.update(remote_id=remote_id, complete=True, mock=True)
        if save_checkpoint:
            save_checkpoint(cp)
        return {"status": "PUBLISHED", "remote_id": remote_id, "url": None, "checkpoint": cp, "mock": True}

    def analytics(self, remote_id, credentials, start_date=None, end_date=None):
        return {"views": 0, "likes": 0, "comments": 0, "shares": 0, "mock": True}


class MockStorageProvider:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _folder(self, folder_id):
        path = (self.root / folder_id).resolve()
        if not path.is_relative_to(self.root):
            raise ProviderError("Geçersiz mock klasör yolu.", "invalid_path")
        return path

    def validate_folder(self, folder_id):
        self._folder(folder_id).mkdir(parents=True, exist_ok=True)
        return {"id": folder_id, "mimeType": "application/vnd.google-apps.folder", "mock": True}

    def ensure_folders(self, account_name, parent_id="root"):
        prefix = _id(account_name)
        result = {kind: f"{prefix}/{kind}" for kind in ("input", "generated", "published", "failed")}
        for folder in result.values():
            self._folder(folder).mkdir(parents=True, exist_ok=True)
        return result

    def list_files(self, folder_id, *, videos_only=True):
        folder = self._folder(folder_id)
        folder.mkdir(parents=True, exist_ok=True)
        return [{"id": str(file.relative_to(self.root)), "name": file.name, "mimeType": "video/mp4", "parents": [folder_id], "size": str(file.stat().st_size), "mock": True}
                for file in folder.iterdir() if file.is_file() and (not videos_only or file.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"})]

    def upload(self, path, folder_id, idempotency_key, checkpoint=None, save_checkpoint=None):
        target = self._folder(folder_id) / (_id(idempotency_key) + Path(path).suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(path, target)
        file_id = str(target.relative_to(self.root))
        cp = {"file_id": file_id, "complete": True, "mock": True}
        if save_checkpoint:
            save_checkpoint(cp)
        return {"id": file_id, "file_id": file_id, "status": "COMPLETED", "name": target.name, "checkpoint": cp, "mock": True}

    def download(self, file_id, path):
        source = self._folder(file_id)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        return {"path": str(path), "file_id": file_id, "mock": True}

    def move(self, file_id, folder_id):
        source = self._folder(file_id)
        target = self._folder(folder_id) / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists() and source != target:
            source.replace(target)
        return {"id": str(target.relative_to(self.root)), "parents": [folder_id], "mock": True}

    def changes(self, cursor=None):
        return {"changes": [], "cursor": cursor or "mock-cursor", "mock": True}
