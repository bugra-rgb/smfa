"""Resumable production stages, independent publishing, research and feedback."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from factory.config import settings
from factory.db import SessionLocal
from factory.models import (
    Account, AIRun, Analytics, Connection, ContentPerformance, DriveConnection,
    DriveFile, DriveFolder, Idea, Job, PublishJob, PublishedPost,
    RenderJob, Script, Video, VideoVariant,
)
from factory.services.jobs import (
    PermanentFailure, RetryLater, UncertainEffect,
    assert_running, enqueue_job, safe_error, utcnow,
)


def account_data(account):
    return {key: getattr(account, key) for key in (
        "id", "name", "username", "niche", "language", "target_audience",
        "content_style", "tone", "daily_target", "timezone", "settings")}


def normalized(text):
    return " ".join(re.findall(r"\w+", str(text).casefold(), flags=re.UNICODE))


def fingerprint(text):
    return hashlib.sha256(normalized(text).encode()).hexdigest()


def similarity(a, b):
    left, right = set(normalized(a).split()), set(normalized(b).split())
    return len(left & right) / max(1, len(left | right))


def score01(value, default=0.5):
    try:
        number = float(value)
        return max(0, min(1, number / 100 if number > 1 else number))
    except (TypeError, ValueError):
        return default


def rank_idea(idea, recent_topics, performance):
    risk = score01(idea.get("risk_score"), 0)
    if risk >= 0.65:
        return -1
    score = sum(score01(idea.get(key)) * weight for key, weight in (
        ("novelty_score", 0.24), ("relevance_score", 0.24), ("virality_score", 0.17),
        ("production_score", 0.13), ("predicted_performance", 0.12), ("trend_score", 0.10)))
    topic = normalized(idea.get("topic", ""))
    penalty = min(0.25, recent_topics.get(topic, 0) * 0.04)
    uplift = min(0.15, performance.get(topic, 0) * 0.1)
    return round(max(0, score - risk * 0.35 - penalty + uplift), 5)


def remember(session, account_id):
    ideas = session.scalars(select(Idea).where(Idea.account_id == account_id)
                           .order_by(Idea.created_at.desc()).limit(100)).all()
    performances = session.scalars(select(ContentPerformance).where(
        ContentPerformance.account_id == account_id).order_by(ContentPerformance.score.desc()).limit(100)).all()
    by_video = {entry.video_id: entry for entry in performances}
    videos = session.scalars(select(Video).where(Video.account_id == account_id)
                            .order_by(Video.created_at.desc()).limit(100)).all()
    by_idea = {v.idea_id: by_video.get(v.id) for v in videos}
    def metrics_for(idea_id):
        performance = by_idea.get(idea_id)
        return performance.metrics if performance is not None else {}
    return [{"title": idea.title, "hook": idea.hook, "topic": idea.topic,
             "summary": idea.summary, "sources": (idea.data or {}).get("sources", []),
             "performance": metrics_for(idea.id),
             "status": idea.status} for idea in ideas]


def bind_provider(provider, ctx):
    """Guard every HTTP request, including multipart chunks and nested image calls."""
    seen = set()

    def visit(obj):
        if id(obj) in seen:
            return
        seen.add(id(obj))
        client = getattr(obj, "client", None)
        if client is not None and hasattr(client, "event_hooks"):
            client.event_hooks["request"] = [*client.event_hooks.get("request", []), lambda request: ctx.guard()]
        for key in ("image_provider", "image", "voice_provider", "fallback", "primary"):
            child = getattr(obj, key, None)
            if child is not None:
                visit(child)
    visit(provider)
    return provider


def secure_checkpoint(checkpoint):
    from factory.security import encrypt_json
    return {"encrypted": encrypt_json(checkpoint)} if checkpoint else {}


def reveal_checkpoint(checkpoint):
    from factory.security import decrypt_json
    if checkpoint and "encrypted" in checkpoint:
        return decrypt_json(checkpoint["encrypted"])
    return {key: value for key, value in (checkpoint or {}).items() if key != "worker_lease"}


def record_usage(ctx, response, operation, provider=None):
    usage = response.get("usage") or {}
    if not usage and not response.get("provider"):
        return
    provider_name = str(usage.get("provider") or response.get("provider") or provider or "unknown")
    model_name = str(usage.get("model") or response.get("model") or "")
    estimate = usage.get("estimated_cost", response.get("estimated_cost"))
    if estimate is None:
        from factory.costs import estimate_cost
        try:
            rates = json.loads(getattr(settings, "cost_rates_json", "{}"))
        except (TypeError, ValueError):
            rates = {}
        estimate = estimate_cost(provider_name, model_name, operation, {"requests": 1, **usage}, rates)
    ctx.session.add(AIRun(account_id=ctx.job.account_id,
        provider=provider_name, model=model_name, operation=operation,
        input_tokens=int(usage.get("input_tokens", 0) or 0), output_tokens=int(usage.get("output_tokens", 0) or 0),
        estimated_cost=estimate,
        actual_cost=usage.get("actual_cost", response.get("actual_cost"))))


def get_drive_for(ctx, account):
    from factory.providers import get_drive
    from factory.security import decrypt_credentials

    connection = ctx.session.scalar(select(Connection).where(Connection.account_id == account.id,
        Connection.platform.in_(("google", "drive")), Connection.status.in_(("CONNECTED", "connected"))))
    if not connection and settings.mode != "mock":
        raise PermanentFailure("Google Drive bağlantısı gerekli; hesabı panelden bağlayın.")
    credentials = ctx.external(decrypt_credentials, connection.id, ctx.session) if connection else {}
    return bind_provider(get_drive(settings, credentials), ctx), connection


def ensure_folders(ctx, account, drive):
    from factory.services.drive_shared import enabled as shared_input
    rows = ctx.session.scalars(select(DriveFolder).where(DriveFolder.account_id == account.id)).all()
    by_kind = {row.kind.lower(): row for row in rows}
    configured_input = (account.settings or {}).get("drive_input_folder_id")
    if configured_input and ("input" not in by_kind or by_kind["input"].external_id != configured_input):
        ctx.external(drive.validate_folder, configured_input)
        if "input" in by_kind:
            by_kind["input"].external_id = configured_input
        else:
            row = DriveFolder(account_id=account.id, kind="input", external_id=configured_input)
            ctx.session.add(row)
            by_kind["input"] = row
        ctx.session.commit()
    if shared_input(account):
        if "input" not in by_kind:
            raise PermanentFailure("Ortak Drive giriş klasörü seçilmeli.")
        return {"input": by_kind["input"]}
    if all(kind in by_kind for kind in ("input", "generated", "published", "failed")):
        return by_kind
    created = ctx.external(drive.ensure_folders, f"{account.name}-{account.id[:8]}")
    for kind, external_id in created.items():
        kind = kind.lower()
        if kind not in by_kind:
            row = DriveFolder(account_id=account.id, kind=kind, external_id=external_id)
            try:
                with ctx.session.begin_nested():
                    ctx.session.add(row)
                    ctx.session.flush()
                by_kind[kind] = row
            except IntegrityError:
                by_kind[kind] = ctx.session.scalar(select(DriveFolder).where(
                    DriveFolder.account_id == account.id, DriveFolder.kind == kind))
    ctx.session.commit()
    return by_kind


def select_idea(ctx, account, llm):
    session = ctx.session
    history = remember(session, account.id)
    if not (ctx.job.result or {}).get("research"):
        response = ctx.external(llm.research, account_data(account), history)
        record_usage(ctx, response, "research")
        ctx.checkpoint(research=response)
    response = ctx.job.result["research"]
    recent = Counter(normalized(row["topic"]) for row in history[:20])
    strategy = (account.settings or {}).get("optimization", {}).get("topic_scores", {})
    selected = []
    for entry in response.get("ideas", []):
        title = str(entry.get("title", "")).strip()
        if not title:
            continue
        if any(similarity(title + " " + str(entry.get("summary", "")), old["title"] + " " + old["summary"]) >= 0.80 for old in history):
            continue
        score = rank_idea(entry, recent, strategy)
        if score < 0:
            continue
        key = fingerprint(title + " " + str(entry.get("summary", "")))
        existing = session.scalar(select(Idea).where(Idea.account_id == account.id, Idea.fingerprint == key))
        if existing:
            if existing.status == "AVAILABLE":
                selected.append(existing)
            continue
        idea = Idea(account_id=account.id, title=title[:500], hook=str(entry.get("hook", "")),
                    topic=str(entry.get("topic", account.niche))[:255], summary=str(entry.get("summary", "")),
                    score=score, fingerprint=key, status="AVAILABLE",
                    data={**entry, "sources": response.get("sources", []), "mock": settings.mode == "mock"})
        try:
            with session.begin_nested():
                session.add(idea)
                session.flush()
            selected.append(idea)
        except IntegrityError:
            continue
    session.commit()
    # Claim selection under row lock to keep simultaneous productions independent.
    chosen = session.scalar(select(Idea).where(Idea.account_id == account.id, Idea.status == "AVAILABLE")
                            .order_by(Idea.score.desc()).with_for_update(skip_locked=True).limit(1))
    if not chosen:
        ctx.checkpoint(research=None)
        raise RetryLater("Tekrarsız ve güvenli yeni fikir bulunamadı; yeniden araştırılacak.", 900, True)
    chosen.status = "IN_PRODUCTION"
    session.commit()
    return chosen


def ensure_video(ctx, account, source_type):
    session = ctx.session
    if ctx.job.video_id:
        return session.get(Video, ctx.job.video_id)
    video = Video(account_id=account.id, title="", status="DRAFT", source_type=source_type,
                  fingerprint=f"job:{ctx.job.id}", metadata_json={"mock": settings.mode == "mock"})
    session.add(video)
    session.flush()
    ctx.job.video_id = video.id
    if source_type == "DRIVE":
        source = session.scalar(select(DriveFile).where(DriveFile.id == ctx.job.payload["drive_file_id"])
                                .with_for_update().execution_options(populate_existing=True))
        if not source or source.account_id != account.id:
            raise PermanentFailure("Drive dosyası bu hesaba ait değil.")
        if source.video_id and source.video_id != video.id:
            raise PermanentFailure("Drive dosyası daha önce işlendi.")
        source.video_id = video.id
        source.processing_status = "PROCESSING"
        video.source_file_id = source.id
        video.title = Path(source.filename).stem
    session.commit()
    return video


def video_options(account):
    options = dict(account.settings or {})
    options.setdefault("duration", 30)
    options.setdefault("width", 1080)
    options.setdefault("height", 1920)
    options.setdefault("subtitle", {"enabled": True})
    options.setdefault("qa", {"require_audio": True})
    options["language"] = account.language
    return options


def production_options(account, job_id):
    options = video_options(account)
    features = options.get("optimization", {}).get("features", {})
    # Deterministic assignment survives retries: one quarter explores other proven
    # candidates instead of repeatedly producing only the current winning format.
    bucket = int(hashlib.sha256(str(job_id).encode()).hexdigest()[:8], 16)
    if bucket % 4 != 0:
        return options
    suggestions = features.get("suggestions", {})
    if "content_format" in features.get("applied", []) and not account.content_style and options.get("optimize_format", True):
        choices = sorted(set(suggestions.get("content_format", {}).get("bucket_means", {})) & {"explainer", "comparison", "listicle", "story"})
        choices = [choice for choice in choices if choice != options.get("content_format")]
        if choices:
            options["content_format"] = choices[(bucket // 4) % len(choices)]
            options["exploration"] = True
    return options


def trim_transcript(transcript: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Map source timestamps onto trimmed/sped-up output without out-of-range cues."""
    start = float(options.get("trim_start", 0))
    speed = float(options.get("speed", 1))
    duration = float(options["duration"])
    segments: list[dict[str, Any]] = []
    for segment in transcript.get("segments") or []:
        left = max(0, (float(segment.get("start", 0)) - start) / speed)
        right = min(duration, (float(segment.get("end", 0)) - start) / speed)
        if right > left and segment.get("text"):
            segments.append({"start": left, "end": right, "text": str(segment["text"])})
    return {**transcript, "segments": segments,
            "text": " ".join(row["text"] for row in segments) if segments else str(transcript.get("text", ""))}


def production(ctx, source_type="AI"):
    from factory.media.engine import inspect_media, quality_check, render_video, write_subtitles
    from factory.providers import get_llm, get_music, get_video, get_voice

    session = ctx.session
    account = ctx.guard()
    if source_type == "AI" and account.source_mode == "DRIVE_ONLY":
        raise PermanentFailure("Bu hesap DRIVE_ONLY modunda; AI üretimi kapalı.")
    if source_type == "DRIVE" and account.source_mode == "AI_ONLY":
        raise PermanentFailure("Bu hesap AI_ONLY modunda; Drive girişi kapalı.")
    video = ensure_video(ctx, account, source_type)
    if video.status in ("READY", "SCHEDULED", "PUBLISHED", "AWAITING_APPROVAL"):
        return {"video_id": video.id}
    options = production_options(account, ctx.job.id)
    manual_tags = (account.settings or {}).get("caption_policy") == "manual_five_hashtags"
    if manual_tags:
        if source_type != "DRIVE":
            raise PermanentFailure("Bu hesapta yalnızca hazır Drive videoları yayınlanır.")
        options["subtitle"] = {**options.get("subtitle", {}), "enabled": False}
        options["voice"] = {**options.get("voice", {}), "replace_source": False}
    production_account = account_data(account)
    production_account["settings"] = options
    workdir = Path(settings.data_dir).resolve() / "accounts" / account.id / "videos" / video.id
    workdir.mkdir(parents=True, exist_ok=True)
    raw_path, final_path = str(workdir / "source.mp4"), str(workdir / "final.mp4")
    llm = None if manual_tags else bind_provider(get_llm(settings, account_settings=account.settings), ctx)
    voice = None if manual_tags else bind_provider(get_voice(settings, account_settings=account.settings), ctx)
    drive, _ = get_drive_for(ctx, account)
    folders = ensure_folders(ctx, account, drive)

    script = session.get(Script, video.script_id) if video.script_id else None
    if source_type == "AI":
        if not video.idea_id:
            video.status = "RESEARCH"
            session.commit()
            idea = select_idea(ctx, account, llm)
            video.idea_id, video.title = idea.id, idea.title
            session.commit()
        idea = session.get(Idea, video.idea_id)
        if not script:
            response = ctx.external(llm.script, {**idea.data, "title": idea.title, "hook": idea.hook,
                "summary": idea.summary, "topic": idea.topic}, production_account)
            if not str(response.get("text", "")).strip():
                raise PermanentFailure("AI sağlayıcısı boş senaryo döndürdü.")
            if not (response.get("safety") or {}).get("approved"):
                raise PermanentFailure("Senaryo içerik güvenlik kontrolünü geçemedi: " + str((response.get("safety") or {}).get("reason", "Onay bilgisi yok.")))
            script = Script(account_id=account.id, idea_id=idea.id, text=response["text"], data=response)
            session.add(script)
            session.flush()
            video.script_id = script.id
            record_usage(ctx, response, "script")
            video.status = "SCRIPTING"
            session.commit()
        if not ctx.job.result.get("raw_ready") or not Path(raw_path).exists():
            video.status = "GENERATING"
            session.commit()
            provider_options = dict(account.settings or {})
            if ctx.job.attempts >= 2 and provider_options.get("video_fallback_provider") and not ctx.job.result.get("video_checkpoint"):
                provider_options["video_provider"] = provider_options["video_fallback_provider"]
            provider = bind_provider(get_video(settings, account_settings=provider_options), ctx)
            checkpoint = reveal_checkpoint(ctx.job.result.get("video_checkpoint"))
            response = ctx.external(provider.generate, script.data.get("visual_prompt", script.text), raw_path,
                {**options, "idempotency_key": f"generate:{video.id}:{ctx.job.result.get('regenerations', 0)}",
                 "scenes": script.data.get("scenes", []),
                 "checkpoint": checkpoint,
                 "save_checkpoint": lambda cp: ctx.checkpoint(video_checkpoint=secure_checkpoint(cp))})
            if response.get("checkpoint"):
                ctx.checkpoint(video_checkpoint=secure_checkpoint(response["checkpoint"]))
            if response.get("status") == "PROCESSING":
                raise RetryLater("Video sağlayıcısında üretim sürüyor.", 30)
            if response.get("status") == "UNKNOWN":
                raise UncertainEffect("Video üretimi sonucu belirsiz; ikinci ücretli üretim otomatik başlatılmadı.")
            if not Path(raw_path).is_file():
                raise RuntimeError("Video sağlayıcısı çıktıyı oluşturmadı.")
            record_usage(ctx, response, "video")
            ctx.checkpoint(raw_ready=True, video_provider=response.get("provider"), video_format=response.get("format"))
    else:
        source = session.get(DriveFile, video.source_file_id)
        if not ctx.job.result.get("raw_ready") or not Path(raw_path).exists():
            from factory.services.drive_shared import download_id
            ctx.external(drive.download, download_id(account, source), raw_path)
            ctx.checkpoint(raw_ready=True)
        source_info = inspect_media(raw_path)
        available = (source_info["duration"] - float(options.get("trim_start", 0))) / float(options.get("speed", 1))
        if available <= 0:
            raise PermanentFailure("Drive videosunda seçilen başlangıçtan sonra görüntü kalmıyor.")
        options["duration"] = min(float(options["duration"]), available)
        if not script:
            supplied_text = str(options.get("voice", {}).get("text", "")).strip()
            needs_text = options.get("subtitle", {}).get("enabled", True) or options.get("voice", {}).get("replace_source", False)
            if supplied_text:
                transcript = {"text": supplied_text, "segments": [], "source": "account_configuration"}
            elif needs_text and source_info["has_audio"]:
                transcript = trim_transcript(ctx.external(voice.transcribe, raw_path,
                    {"duration": options["duration"], "trim_start": options.get("trim_start", 0), "speed": options.get("speed", 1)}),
                    {"duration": options["duration"]})
            elif needs_text:
                raise PermanentFailure("Sessiz Drive videosu için voice.text girin veya altyazı ve ses değiştirmeyi kapatın.")
            else:
                transcript = {"text": video.title, "segments": [], "source": "filename_only",
                              "instruction": "Yalnızca dosya adından doğrulanabilen metadata üret; olay veya konuşma uydurma."}
            if needs_text and not str(transcript.get("text", "")).strip():
                raise PermanentFailure("Drive videosunda metne dönüştürülebilen konuşma yok.")
            script = Script(account_id=account.id, text=transcript["text"], data=transcript)
            session.add(script)
            session.flush()
            video.script_id = script.id
            record_usage(ctx, transcript, "transcription")
            session.commit()

    if options.get("music_path"):
        music_path = str(workdir / "licensed-music" / Path(options["music_path"]).name)
        response = ctx.external(get_music(settings, account_settings=account.settings).generate, "", music_path,
            {"licensed_music_path": options["music_path"],
             "license_confirmed": bool(options.get("music_license_confirmed") or options.get("music", {}).get("license_confirmed"))})
        options["music_path"] = response["path"]

    voice_path = ctx.job.result.get("voice_path")
    needs_voice = source_type == "AI" or bool(options.get("voice", {}).get("replace_source", False))
    if needs_voice and (not voice_path or not Path(voice_path).exists()):
        voice_path = str(workdir / "voice.wav")
        response = ctx.external(voice.synthesize, script.text, voice_path,
                                {**options.get("voice", {}), "language": account.language, "duration": options["duration"]})
        record_usage(ctx, response, "voice")
        ctx.checkpoint(voice_path=voice_path)
    if needs_voice and not ctx.job.result.get("voice_fitted"):
        spoken_duration = inspect_media(voice_path)["duration"]
        if spoken_duration > float(options["duration"]) + 0.2:
            factor = spoken_duration / float(options["duration"]) * 1.03
            if factor > 1.3:
                fits = int(ctx.job.result.get("voice_fit_attempts", 0))
                if fits >= 2:
                    raise PermanentFailure("İki otomatik senaryo kısaltması sonrası ses hedef süreye sığmadı.")
                shorter = account_data(account)
                shorter["settings"] = {**options, "duration": float(options["duration"]) / factor * 0.8}
                response = ctx.external(llm.script, {"title": video.title, "summary": script.text,
                    "instruction": "Mevcut metni daha kısa yaz; yeni iddia ekleme, ana fikri koru."}, shorter)
                if not response.get("text") or not (response.get("safety") or {}).get("approved"):
                    raise PermanentFailure("Otomatik kısaltılan senaryo güvenlik kontrolünü geçemedi.")
                script.text, script.data = response["text"], response
                record_usage(ctx, response, "script_duration_fit")
                ctx.checkpoint(voice_path=None, voice_fit_attempts=fits + 1, subtitle_path=None, metadata=None)
                raise RetryLater("Seslendirme süresine göre senaryo otomatik kısaltıldı.", 5, True)
            voice_options = options.get("voice", {})
            speed = min(4, float(voice_options.get("speed", 1)) * factor)
            response = ctx.external(voice.synthesize, script.text, voice_path,
                {**voice_options, "speed": speed, "language": account.language, "duration": options["duration"]})
            record_usage(ctx, response, "voice_duration_fit")
            if inspect_media(voice_path)["duration"] > float(options["duration"]) + 0.2:
                raise PermanentFailure("Seslendirme hedef süreye sığmadı; konuşma kesilmeden üretim durduruldu.")
        ctx.checkpoint(voice_fitted=True)

    subtitle_path = ctx.job.result.get("subtitle_path")
    if options.get("subtitle", {}).get("enabled", True) and (not subtitle_path or not Path(subtitle_path).exists()):
        transcript = script.data if source_type == "DRIVE" and not needs_voice else ctx.external(voice.transcribe, voice_path)
        if needs_voice:
            transcript = trim_transcript(transcript, {"duration": options["duration"]})
        if source_type == "AI" or needs_voice:
            record_usage(ctx, transcript, "subtitle_alignment")
        subtitle_path = str(workdir / "subtitles.srt")
        raw_segments = transcript.get("segments")
        subtitle_segments = raw_segments if isinstance(raw_segments, list) else None
        subtitle = write_subtitles(script.text, float(options["duration"]), subtitle_path,
                                   segments=subtitle_segments)
        ctx.checkpoint(subtitle_path=subtitle_path, subtitle_timing=subtitle.get("timing"))

    if not ctx.job.result.get("metadata"):
        fixed_caption = options.get("publishing", {}).get("instagram", {}).get("caption", "")
        if manual_tags:
            from factory.services.hashtags import copy_for
            prepared = copy_for(account, video.id)
            response = {platform: dict(prepared) for platform in ("youtube", "instagram", "tiktok")}
        elif (account.settings or {}).get("caption_policy") == "openai_five_hashtags":
            response = {"youtube": {"title": video.title[:100], "description": "Yayın açıklaması hazırlanacak."},
                "instagram": {"caption": "Yayın açıklaması hazırlanacak."}, "tiktok": {"caption": "Yayın açıklaması hazırlanacak."}}
        elif source_type == "DRIVE" and fixed_caption:
            # Ready-made clips with an approved fixed caption do not need an LLM call.
            response = {"youtube": {"title": video.title[:100], "description": fixed_caption},
                "instagram": {"caption": fixed_caption}, "tiktok": {"caption": fixed_caption}}
        else:
            response = ctx.external(llm.metadata, {**script.data, "text": script.text, "title": video.title}, production_account)
        if fixed_caption and not manual_tags:
            response["instagram"] = {**response.get("instagram", {}), "caption": fixed_caption}
        metadata = {platform: response.get(platform, {}) for platform in ("youtube", "instagram", "tiktok")}
        if not (metadata["youtube"].get("title") and metadata["youtube"].get("description")
                and metadata["instagram"].get("caption") and metadata["tiktok"].get("caption")):
            raise RuntimeError("Platforma özel metadata eksik veya geçersiz.")
        metadata["mock"] = settings.mode == "mock"
        metadata["production"] = {"duration": options["duration"], "content_format": options.get("content_format", account.content_style or "explainer"),
            "video_provider": options.get("video_provider", "image_motion"), "source_type": source_type,
            "exploration": bool(options.get("exploration")),
            "created_at": utcnow().isoformat()}
        from factory.services.drive_repeat import FLAG
        if isinstance(ctx.job.payload.get(FLAG), dict):
            metadata[FLAG] = dict(ctx.job.payload[FLAG])
        video.metadata_json = metadata
        if not manual_tags and not (source_type == "DRIVE" and fixed_caption):
            record_usage(ctx, response, "metadata")
        ctx.checkpoint(metadata=True)

    if not ctx.job.result.get("rendered") or not Path(final_path).is_file():
        video.status = "RENDERING"
        rendering = session.scalar(select(RenderJob).where(RenderJob.job_id == ctx.job.id))
        if not rendering:
            rendering = RenderJob(account_id=account.id, job_id=ctx.job.id, video_id=video.id,
                                  status="RUNNING", settings=options, output_path=final_path)
            session.add(rendering)
        else:
            rendering.status = "RUNNING"
        session.commit()
        ctx.guard()
        rendered = render_video(raw_path, final_path, options, voice_path=voice_path, subtitle_path=subtitle_path)
        rendering.status = "SUCCEEDED"
        video.local_path = final_path
        ctx.checkpoint(rendered=rendered)
    rendered = ctx.job.result["rendered"]
    if not ctx.job.result.get("qa_passed"):
        video.status = "QA"
        session.commit()
        ctx.guard()
        qa_options = {**options, "qa": {**options.get("qa", {}), "require_metadata": True},
                      "metadata": video.metadata_json.get("youtube", {})}
        qa = quality_check(final_path, qa_options)
        video.qa = qa
        session.commit()
        if not qa.get("passed"):
            regenerations = int(ctx.job.result.get("regenerations", 0))
            if source_type == "AI" and regenerations < 2:
                ctx.checkpoint(raw_ready=False, rendered=None, video_checkpoint={}, regenerations=regenerations + 1)
                raise RetryLater("Kalite kontrolü başarısız; video yeniden üretilecek: " + "; ".join(qa.get("errors", [])), 30, True)
            video.status = "FAILED"
            session.commit()
            raise PermanentFailure("Kalite kontrolü başarısız: " + "; ".join(qa.get("errors", [])))
        ctx.checkpoint(qa_passed=True)

    from factory.services.drive_shared import enabled as shared_input
    if not video.drive_file_id and not shared_input(account):
        video.status = "UPLOADING"
        session.commit()
        response = ctx.external(drive.upload, final_path, folders["generated"].external_id,
             f"video:{video.id}:final:{ctx.job.result.get('render_revision', 'initial')}", checkpoint=reveal_checkpoint(ctx.job.result.get("drive_checkpoint")),
             save_checkpoint=lambda cp: ctx.checkpoint(drive_checkpoint=secure_checkpoint(cp)))
        file_id = response.get("file_id") or response.get("id")
        if not file_id or response.get("status") in ("PROCESSING", "UNKNOWN"):
            raise RuntimeError("Drive yükleme tamamlanma kaydı alınamadı.")
        video.drive_file_id = file_id
        session.add(DriveFile(account_id=account.id, drive_file_id=file_id, folder_id=folders["generated"].id,
                   filename=Path(final_path).name, source_type=source_type, processing_status="READY",
                   publishing_status="PENDING", video_id=video.id))
        session.commit()

    for platform in ("youtube", "instagram", "tiktok"):
        variant = session.scalar(select(VideoVariant).where(VideoVariant.video_id == video.id, VideoVariant.platform == platform))
        if not variant:
            session.add(VideoVariant(account_id=account.id, video_id=video.id, platform=platform,
                local_path=final_path, width=rendered["width"], height=rendered["height"], duration=rendered["duration"],
                metadata_json=video.metadata_json.get(platform, {})))
        else:
            variant.local_path, variant.width, variant.height, variant.duration = final_path, rendered["width"], rendered["height"], rendered["duration"]
            variant.metadata_json = video.metadata_json.get(platform, {})
    video.status = "AWAITING_APPROVAL" if account.approval_mode == "MANUAL_APPROVAL" else "READY"
    if video.idea_id:
        session.get(Idea, video.idea_id).status = "PRODUCED"
    if video.source_file_id:
        session.get(DriveFile, video.source_file_id).processing_status = "READY"
    session.commit()
    return {"video_id": video.id, "qa_passed": True, "drive_file_id": video.drive_file_id, "mock": settings.mode == "mock"}


def drive_sync(ctx):
    account = ctx.guard()
    drive, connection = get_drive_for(ctx, account)
    folders = ensure_folders(ctx, account, drive)
    session = ctx.session
    row = session.scalar(select(DriveConnection).where(DriveConnection.account_id == account.id))
    if not row and connection:
        row = DriveConnection(account_id=account.id, connection_id=connection.id, status="connected")
        session.add(row)
        session.flush()
    # Establish a changes cursor BEFORE initial scan so concurrent additions are not lost.
    changes = ctx.external(drive.changes, row.cursor if row else None)
    files = ctx.external(drive.list_files, folders["input"].external_id)
    discovered = 0
    for file in files:
        external_id = file.get("id") or file.get("file_id")
        if external_id:
            from factory.services.drive_shared import source_key
            external_id = source_key(account, external_id)
        if not external_id or session.scalar(select(DriveFile.id).where(DriveFile.drive_file_id == external_id)):
            continue
        try:
            with session.begin_nested():
                session.add(DriveFile(account_id=account.id, drive_file_id=external_id,
                    folder_id=folders["input"].id, filename=file.get("name", "input.mp4"),
                    source_type="DRIVE", processing_status="DISCOVERED", publishing_status="PENDING"))
                session.flush()
            discovered += 1
        except IntegrityError:
            continue
    if row:
        row.cursor, row.last_sync, row.status = changes.get("cursor"), utcnow(), "connected"
    session.commit()
    from factory.services.drive_repeat import scan_result
    return {"discovered": discovered, "files_seen": len(files),
            **scan_result(account, folders["input"].external_id, files)}


def publishing(ctx):
    from factory.providers import get_publisher
    from factory.security import decrypt_credentials, signed_media_url

    session = ctx.session
    account = ctx.guard()
    pub = session.scalar(select(PublishJob).where(PublishJob.id == ctx.job.payload["publish_job_id"])
                         .with_for_update().execution_options(populate_existing=True))
    if not pub or pub.account_id != account.id:
        raise PermanentFailure("Yayın işi bu hesaba ait değil.")
    existing = session.scalar(select(PublishedPost).where(PublishedPost.video_id == pub.video_id,
                                                         PublishedPost.connection_id == pub.connection_id))
    if existing:
        pub.status, pub.remote_id = "PUBLISHED", existing.remote_id
        session.commit()
        return {"post_id": existing.id, "remote_id": existing.remote_id}
    if pub.status == "CANCELLED":
        return {"cancelled": True}
    if pub.status in ("UNKNOWN", "POLICY_BLOCKED", "BLOCKED"):
        raise PermanentFailure("Yayın işi inceleme bekliyor; otomatik yeniden yayınlama kapalı.")
    lease = (pub.checkpoint or {}).get("worker_lease", {})
    if lease.get("job_id") != ctx.job.id and lease.get("until"):
        if datetime.fromisoformat(lease["until"]) > utcnow():
            session.commit()
            raise RetryLater("Bu yayını başka bir kalıcı iş yürütüyor.", 30)
    publication_lease = {"job_id": ctx.job.id, "token": ctx.token, "until": ctx.job.lease_until.isoformat()}
    pub.checkpoint = {**(pub.checkpoint or {}), "worker_lease": publication_lease}
    session.commit()
    if pub.scheduled_at > utcnow():
        raise RetryLater("Planlanan yayın saati bekleniyor.", max(1, (pub.scheduled_at - utcnow()).total_seconds()))
    from factory.services.scheduler import local_day_bounds
    from factory.services.cadence import interval_hours, next_due, pending_video
    now = utcnow()
    controlled = bool(ctx.job.payload.get("publication_request_id"))
    if controlled:
        from factory.services.planner import enforce_request
        if not enforce_request(ctx, pub, now): return {"cancelled": True}
    elif (account.settings or {}).get("cycle_v2"):
        from factory.services.control import enforce_cycle_window
        enforce_cycle_window(ctx, account, pub, now)
    elif interval_hours(account):
        first = pending_video(session, account.id)
        if first and first != pub.video_id:
            raise RetryLater("Önceki içeriğin yayın sonucu bekleniyor.", 60)
        due = next_due(session, account, now, pub.video_id)
        if due > now:
            pub.scheduled_at = due
            session.commit()
            raise RetryLater("Hesabın yayın aralığı bekleniyor.", (due - now).total_seconds())
    start, end = local_day_bounds(now, account.timezone)
    if not controlled and not interval_hours(account) and pub.scheduled_at < start:
        session.scalar(select(Account).where(Account.id == account.id).with_for_update())
        today_reserved = set(session.scalars(select(PublishJob.video_id).where(
            PublishJob.account_id == account.id, PublishJob.scheduled_at >= start,
            PublishJob.scheduled_at < end, PublishJob.status != "CANCELLED")))
        if pub.video_id not in today_reserved and len(today_reserved) >= account.daily_target:
            session.commit()
            raise RetryLater("Geciken yayın günlük hedefi aşmamak için sonraki günü bekliyor.", max(60, (end - now).total_seconds()))
        peers = session.scalars(select(PublishJob).where(PublishJob.video_id == pub.video_id,
            PublishJob.status.not_in(("PUBLISHED", "CANCELLED")))).all()
        for peer in peers:
            peer.scheduled_at = now
        session.commit()
    connection = session.get(Connection, pub.connection_id)
    if not connection or connection.status not in ("CONNECTED", "connected"):
        raise PermanentFailure("Platform bağlantısı etkin değil.")
    if (account.settings or {}).get("managed_platform"):
        from factory.services.channels import eligible
        if not eligible(account, connection):
            pub.status = "BLOCKED"
            pub.error_message = "Bağımsız kanalın platformu veya otomatik yayın izni uygun değil."
            session.commit()
            raise PermanentFailure(pub.error_message)
    if pub.platform == "x":
        from factory.providers.x import ready
        if not ready(connection) or (account.settings or {}).get("caption_policy") != "manual_five_hashtags":
            pub.status = "BLOCKED"
            pub.error_message = "X otomatik yayın izni kapalı veya gerekli kapsamlar eksik."
            session.commit()
            raise PermanentFailure(pub.error_message)
    video = session.get(Video, pub.video_id)
    if not video or not video.local_path or not Path(video.local_path).is_file():
        raise PermanentFailure("Yayınlanacak yerel video bulunamadı.")
    if not (video.qa or {}).get("passed") or video.status == "AWAITING_APPROVAL":
        raise PermanentFailure("Video kalite kontrolü veya onayı tamamlanmadı.")
    if (not account.auto_publish or account.approval_mode == "MANUAL_APPROVAL") and not (video.metadata_json or {}).get("approved"):
        raise RetryLater("Otomatik yayın kapalı; video onay bekliyor.", 300)
    publishing_config = (account.settings or {}).get("publishing", {}).get(pub.platform, {})
    # Empty fixed captions retain generated metadata.
    publishing_config = {k: v for k, v in publishing_config.items() if k != "caption" or v}
    metadata = {**video.metadata_json.get(pub.platform, {}), **(connection.settings or {}), **publishing_config,
                "idempotency_key": pub.idempotency_key, "is_ai_generated": video.source_type == "AI"}
    if pub.platform in ("instagram", "youtube", "tiktok", "facebook", "x"):
        from factory.services.hashtags import publication_copy
        metadata.update(publication_copy(ctx, account, video, pub))
    elif (account.settings or {}).get("caption_policy") == "openai_five_hashtags":
        from factory.services.control import publication_copy
        generated = publication_copy(ctx, account, video, pub)
        metadata.update(generated)
    if pub.platform == "youtube" and (account.settings or {}).get("managed_platform") == "youtube":
        from factory.services.channels import youtube_buffer
        if not youtube_buffer(connection) and publishing_config.get("privacy_status") != "private" and not settings.youtube_audit_approved:
            raise PermanentFailure("YouTube uygulama denetimi doğrulanmadan herkese açık yayın yapılamaz.")
        if type(publishing_config.get("made_for_kids")) is not bool or type(publishing_config.get("contains_synthetic_media")) is not bool:
            raise PermanentFailure("YouTube izleyici ve yapay içerik seçimleri tamamlanmalı.")
    if pub.platform == "youtube":
        from factory.services.youtube_hashtags import publication_copy as youtube_publication_copy
        metadata.update(youtube_publication_copy(ctx, account, video, pub, metadata))
        metadata["privacy_status"] = publishing_config.get("privacy_status", (account.settings or {}).get("youtube_privacy_status", "private"))
        metadata["made_for_kids"] = bool(publishing_config.get("made_for_kids", False))
        metadata["contains_synthetic_media"] = video.source_type == "AI" or bool(publishing_config.get("contains_synthetic_media", False))
    from factory.services.account_captions import apply_caption
    apply_caption(ctx, account, video, pub, metadata)
    # This recovery rollout is deliberately limited to the two diagnosed accounts.
    metadata["_smfa_wait_for_ready"] = pub.platform == "instagram" and (account.settings or {}).get('instagram_wait_for_ready') is True
    if pub.platform == "instagram":
        from factory.services.covers import resolve_publication_cover
        # Cover is resolved only from the isolated feature records, not legacy JSON.
        metadata.pop("cover_url", None); metadata.pop("cover_asset_id", None)
        try:
            metadata.update(resolve_publication_cover(session, pub, reveal_checkpoint(pub.checkpoint)))
        except Exception as exc:
            if getattr(exc, "code", "") == "cover_unavailable":
                pub.status = "BLOCKED"; pub.error_message = str(exc); session.commit()
            raise
        metadata["public_video_url"] = signed_media_url(video.id)
        metadata["video_url"] = metadata["public_video_url"]
        if (connection.settings or {}).get('publish_via') == 'buffer':
            from factory.services.buffer_instagram_media import prepare
            metadata.update(prepare(ctx, account, video, pub, metadata, reveal_checkpoint(pub.checkpoint)))
            metadata['public_video_url'] = signed_media_url(video.id, lifetime=86400)
            metadata['video_url'] = metadata['public_video_url']
    if pub.platform == 'youtube' and (connection.settings or {}).get('publish_via') == 'buffer':
        metadata['public_video_url'] = signed_media_url(video.id, lifetime=86400)
    if pub.platform == "tiktok" and (connection.settings or {}).get('publish_via') == 'buffer':
        if type(publishing_config.get('contains_synthetic_media')) is not bool:
            raise PermanentFailure('TikTok yapay/değiştirilmiş içerik seçimi tamamlanmalı.')
        metadata['contains_synthetic_media'] = video.source_type == 'AI' or publishing_config['contains_synthetic_media']
        metadata['public_video_url'] = signed_media_url(video.id, lifetime=86400)
    credentials = ctx.external(decrypt_credentials, connection.id, session)
    credentials = {**credentials, "external_id": connection.external_id}
    publisher = bind_provider(get_publisher(pub.platform, settings, publish_via=(connection.settings or {}).get("publish_via")), ctx)
    if pub.platform == "instagram" and (connection.settings or {}).get("publish_via") != "buffer" and settings.smfa_oauth_hardening_enabled and hasattr(publisher, "client"):
        from factory.services.instagram_connection import current_request_credentials
        from factory.providers.base import bearer
        def instagram_guard(request):
            fresh = current_request_credentials(connection.id, connection.external_id)
            request.headers.update(bearer(fresh))
        publisher.client.event_hooks["request"].append(instagram_guard)
    if pub.platform == "x" and hasattr(publisher, "client"):
        def x_guard(request):
            session.refresh(connection)
            from factory.providers.x import ready
            if not ready(connection):
                from factory.providers.base import ProviderError
                raise ProviderError("X otomatik yayın kapatıldı.", "x_disabled", False)
        publisher.client.event_hooks["request"].append(x_guard)
    if (account.settings or {}).get("managed_platform") and hasattr(publisher, "client"):
        def channel_guard(request):
            session.refresh(connection)
            from factory.services.channels import eligible
            from factory.providers.base import ProviderError
            if not eligible(account, connection):
                raise ProviderError("Bağımsız kanalın otomatik yayın izni kapatıldı.", "channel_disabled", False)
        publisher.client.event_hooks["request"].append(channel_guard)
    if (account.settings or {}).get("cycle_v2"):
        from factory.services.control import admit_publication
        admit_publication(ctx, account)
    pub.status = "PUBLISHING"
    pub.attempts += 1
    session.commit()

    def save_checkpoint(cp):
        ctx.check_lease()
        session.refresh(pub)
        pub.checkpoint = {**secure_checkpoint(cp), "worker_lease": publication_lease}
        if pub.platform == "instagram" and cp.get("cover_sent"):
            from factory.update_models import PublicationCover
            cover = session.get(PublicationCover, pub.id)
            if cover and not cover.sent_at: cover.sent_at = utcnow()
        session.commit()
        ctx.guard()

    try:
        from factory.services.caption_delivery31 import install as install_caption_delivery
        install_caption_delivery(publisher, account, pub, video, metadata)
        response = ctx.external(publisher.publish, video.local_path, metadata, credentials,
             checkpoint=reveal_checkpoint(pub.checkpoint), save_checkpoint=save_checkpoint)
    except Exception as exc:
        if getattr(exc, "checkpoint", None):
            pub.checkpoint = {**secure_checkpoint(getattr(exc, "checkpoint")), "worker_lease": publication_lease}
        code = str(getattr(exc, "code", ""))
        pub.status = "UNKNOWN" if code in ("publish_unknown", "unknown", "upload_unknown") else "RETRYING"
        if getattr(exc, "retryable", True) is False and pub.status != "UNKNOWN":
            pub.status = "BLOCKED"
        pub.error_message = safe_error(exc)
        session.commit()
        if pub.status == "UNKNOWN":
            raise UncertainEffect("Platform yayın sonucunu doğrulayamadı; otomatik tekrar gönderimi durduruldu.") from exc
        raise
    if response.get("checkpoint"):
        pub.checkpoint = {**secure_checkpoint(response["checkpoint"]), "worker_lease": publication_lease}
    status = response.get("status")
    if status == "PROCESSING":
        pub.status = "RETRYING"
        session.commit()
        raise RetryLater("Platform videoyu işliyor.", 60 if metadata.get("_smfa_wait_for_ready") is True else 45)
    if status in ("UNKNOWN", "POLICY_BLOCKED", "BLOCKED", "FAILED"):
        pub.status = status
        pub.error_message = response.get("message", "Platform işlemi otomatik tamamlanamadı.")
        session.commit()
        if status == "UNKNOWN":
            raise UncertainEffect(pub.error_message)
        raise PermanentFailure(pub.error_message)
    if status != "PUBLISHED" or not response.get("remote_id"):
        pub.status = "UNKNOWN"
        session.commit()
        raise UncertainEffect("Platform doğrulanmış yayın kimliği döndürmedi.")
    if pub.platform == "youtube":
        video.metadata_json = {**(video.metadata_json or {}), "confirmed_visibility": {
            **(video.metadata_json or {}).get("confirmed_visibility", {}), "youtube": response.get("visibility")},
            "youtube_publication_evidence": {"provider": response.get("visibility_source", "youtube"),
                "buffer_post_id": response.get("checkpoint", {}).get("buffer_post_id"),
                "published_at": response.get("published_at")}}
    post = PublishedPost(account_id=account.id, video_id=video.id, connection_id=connection.id,
                         platform=pub.platform, remote_id=str(response["remote_id"]), url=response.get("url"))
    session.add(post)
    pub.status, pub.remote_id, pub.error_message = "PUBLISHED", str(response["remote_id"]), None
    video.status = "PUBLISHED"
    session.flush()
    enqueue_job(session, "DRIVE_ARCHIVE", account_id=account.id, video_id=video.id,
                payload={"folder": "published"}, idempotency_key=f"archive:{video.id}:published", priority=20)
    session.commit()
    return {"post_id": post.id, "remote_id": pub.remote_id, "mock": settings.mode == "mock"}


def archive(ctx):
    account = ctx.guard()
    video = ctx.session.get(Video, ctx.job.video_id)
    from factory.services.drive_shared import enabled as shared_input, archive_local
    if shared_input(account):
        return archive_local(ctx, account, video)
    if not video or not video.drive_file_id:
        return {"archived": False, "reason": "Drive çıktısı yok"}
    drive, _ = get_drive_for(ctx, account)
    folders = ensure_folders(ctx, account, drive)
    target = ctx.job.payload.get("folder", "published")
    if target not in ("published", "failed"):
        raise PermanentFailure("Geçersiz arşiv klasörü.")
    moved = ctx.external(drive.move, video.drive_file_id, folders[target].external_id)
    old_file_id = video.drive_file_id
    moved_file_id = moved.get("id") or old_file_id
    rows = ctx.session.scalars(select(DriveFile).where(DriveFile.video_id == video.id)).all()
    for row in rows:
        if row.drive_file_id == old_file_id:
            row.folder_id = folders[target].id
            row.drive_file_id = moved_file_id
        row.publishing_status = target.upper()
        if target == "published":
            row.published_at = utcnow()
    video.drive_file_id = moved_file_id
    ctx.session.commit()
    return {"archived": True, "folder": target}


def failure_upload(ctx):
    account = ctx.guard()
    from factory.services.drive_shared import enabled as shared_input
    if shared_input(account):
        return {"archived": False, "local_record_only": True, "source_file_unchanged": True}
    video = ctx.session.get(Video, ctx.job.video_id)
    if not video:
        return {"archived": False, "reason": "Video kaydı yok"}
    directory = Path(settings.data_dir).resolve() / "accounts" / account.id / "videos" / video.id
    path = Path(video.local_path) if video.local_path else directory / "source.mp4"
    if not path.is_file():
        return {"archived": False, "reason": "Arşivlenecek medya dosyası oluşmadı"}
    drive, _ = get_drive_for(ctx, account)
    folders = ensure_folders(ctx, account, drive)
    response = ctx.external(drive.upload, str(path), folders["failed"].external_id,
        ctx.job.idempotency_key, checkpoint=reveal_checkpoint(ctx.job.result.get("drive_checkpoint")),
        save_checkpoint=lambda cp: ctx.checkpoint(drive_checkpoint=secure_checkpoint(cp)))
    file_id = response.get("file_id") or response.get("id")
    if not file_id:
        raise RuntimeError("Başarısız medya arşiv kimliği alınamadı.")
    if not ctx.session.scalar(select(DriveFile.id).where(DriveFile.drive_file_id == file_id)):
        ctx.session.add(DriveFile(account_id=account.id, video_id=video.id, drive_file_id=file_id,
            filename=path.name, folder_id=folders["failed"].id, source_type=video.source_type,
            processing_status="FAILED", publishing_status="NOT_PUBLISHABLE"))
    ctx.session.commit()
    return {"archived": True, "drive_file_id": file_id, "folder": "failed"}


def rerender(ctx):
    session = ctx.session
    video = session.get(Video, ctx.job.video_id)
    if not video or video.account_id != ctx.job.account_id:
        raise PermanentFailure("Yeniden düzenlenecek video bulunamadı.")
    if not ctx.job.result.get("rerender_started"):
        if video.status not in ("FAILED", "DRAFT", "READY", "AWAITING_APPROVAL"):
            raise PermanentFailure("Yayına alınmış video değiştirilemez; ayrı bir içerik üretin.")
        previous = session.scalar(select(Job).where(Job.video_id == video.id, Job.id != ctx.job.id,
            Job.kind.in_(("AI_PIPELINE", "DRIVE_PIPELINE", "RERENDER"))).order_by(Job.created_at.desc()).limit(1))
        if not previous or not (previous.result or {}).get("raw_ready"):
            raise PermanentFailure("Yeniden düzenleme için kaynak medya hazır değil.")
        old = previous.result or {}
        video.status = "DRAFT"
        video.qa = {}
        video.drive_file_id = None
        video.metadata_json = {key: value for key, value in video.metadata_json.items() if key not in ("approved", "approved_at")}
        ctx.checkpoint(**{key: old[key] for key in ("raw_ready", "voice_path", "subtitle_path", "subtitle_timing", "metadata") if key in old},
                       rerender_started=True, render_revision=ctx.job.id)
    return production(ctx, video.source_type)


def analytics(ctx):
    from factory.providers import get_publisher
    from factory.security import decrypt_credentials

    session = ctx.session
    post = session.get(PublishedPost, ctx.job.payload["post_id"])
    if not post or post.account_id != ctx.job.account_id:
        raise PermanentFailure("Analitik gönderisi bu hesaba ait değil.")
    bucket = datetime.fromisoformat(ctx.job.payload["bucket_at"])
    if session.scalar(select(Analytics.id).where(Analytics.post_id == post.id, Analytics.bucket_at == bucket)):
        return {"duplicate_skipped": True}
    connection = session.get(Connection, post.connection_id)
    credentials = ctx.external(decrypt_credentials, connection.id, session, mark_expired=False)
    credentials = {**credentials, "external_id": connection.external_id}
    publisher = bind_provider(get_publisher(post.platform, settings, publish_via=(connection.settings or {}).get("publish_via")), ctx)
    metrics = ctx.external(publisher.analytics, post.remote_id, credentials,
                           start_date=post.published_at.date().isoformat(), end_date=utcnow().date().isoformat())
    # Missing API fields remain absent; unknown retention is never turned into zero.
    try:
        with session.begin_nested():
            session.add(Analytics(account_id=post.account_id, post_id=post.id, bucket_at=bucket, metrics=metrics))
            session.flush()
    except IntegrityError:
        return {"duplicate_skipped": True}
    session.commit()
    return {"post_id": post.id, "metrics": metrics}


def feature_strategy(account, samples):
    """Bounded observational optimization; explicit schedules/styles remain pinned."""
    config = dict(account.settings or {})
    suggestions: dict[str, Any] = {}
    supported = {"explainer", "comparison", "listicle", "story"}
    for feature in ("duration", "hour", "content_format"):
        buckets: dict[Any, list[float]] = defaultdict(list)
        for sample in samples:
            value = sample.get(feature)
            if value is not None:
                buckets[value].append(sample["score"])
        valid = {key: sum(values) / len(values) for key, values in buckets.items() if len(values) >= 3}
        if len(valid) < 2:
            continue
        winner = max(valid, key=lambda value: valid[value])
        baseline = sum(valid.values()) / len(valid)
        uplift = valid[winner] / baseline - 1 if baseline > 0 else 0
        suggestions[feature] = {"value": winner, "uplift": round(uplift, 4),
                                "samples": len(buckets[winner]), "bucket_means": {str(k): round(v, 4) for k, v in valid.items()}}
    enough = len(samples) >= 10
    applied = []
    recommended_schedule = []
    duration = suggestions.get("duration")
    if enough and duration and duration["uplift"] >= 0.15 and config.get("optimize_duration", True):
        maximum = min(float(config.get("duration_max", 60)), 10 if config.get("video_provider") == "runway" else 180)
        minimum = min(maximum, max(1, float(config.get("duration_min", 5))))
        requested = float(duration["value"])
        if minimum <= requested <= maximum:
            config["duration"] = requested
            applied.append("duration")
    hour = suggestions.get("hour")
    if enough and hour and hour["uplift"] >= 0.15 and config.get("optimize_schedule", True) and not config.get("schedule"):
        values = hour["bucket_means"]
        baseline = sum(values.values()) / len(values)
        ordered = sorted((key for key in values if values[key] >= baseline * 1.15), key=lambda key: values[key], reverse=True)
        recommended_schedule = [f"{int(value):02d}:00" for value in ordered[:max(1, account.daily_target)]]
        applied.append("schedule")
    content_format = suggestions.get("content_format")
    if (enough and content_format and content_format["uplift"] >= 0.15
            and config.get("optimize_format", True) and not account.content_style
            and content_format["value"] in supported):
        config["content_format"] = content_format["value"]
        applied.append("content_format")
    return config, {"status": "evaluated" if enough else "insufficient_data", "mature_real_posts": len(samples),
                    "suggestions": suggestions, "applied": applied, "posting_schedule": recommended_schedule,
                    "minimum_samples": 10, "minimum_bucket_samples": 3, "minimum_uplift": 0.15}


def optimize(ctx):
    session = ctx.session
    account = ctx.guard()
    now = utcnow()
    rows = session.scalars(select(Analytics).where(Analytics.account_id == account.id, Analytics.bucket_at >= now - timedelta(days=30))
                           .order_by(Analytics.bucket_at.desc()).limit(5000)).all()
    latest: dict[str, Analytics] = {}
    for row in rows:
        latest.setdefault(row.post_id, row)
    metrics_by_video: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    samples = []
    for post_id, row in latest.items():
        post = session.get(PublishedPost, post_id)
        if not post:
            continue
        if not row.metrics.get("mock") and now - timedelta(days=30) <= post.published_at <= now - timedelta(hours=24):
            views = row.metrics.get("views", 0)
            if isinstance(views, (int, float)) and views >= 100:
                video = session.get(Video, post.video_id)
                provenance = (video.metadata_json or {}).get("production", {}) if video else {}
                engagement = sum(float(row.metrics.get(key, 0) or 0) for key in ("likes", "comments", "shares", "saves")) / views
                retention = row.metrics.get("average_view_percentage")
                score = engagement + (min(100, max(0, float(retention))) / 100 if isinstance(retention, (int, float)) else 0)
                duration = provenance.get("duration")
                samples.append({"duration": max(1, round(float(duration) / 5) * 5) if isinstance(duration, (int, float)) else None,
                    "hour": post.published_at.astimezone(ZoneInfo(account.timezone)).hour,
                    "content_format": provenance.get("content_format"), "score": score, "platform": post.platform})
        for key in ("views", "likes", "comments", "shares", "saves", "watch_time_seconds"):
            value = row.metrics.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                metrics_by_video[post.video_id][key] += value
    topics = defaultdict(list)
    hooks = []
    for video_id, metrics in metrics_by_video.items():
        video = session.get(Video, video_id)
        idea = session.get(Idea, video.idea_id) if video.idea_id else None
        views = metrics.get("views", 0)
        engagement = sum(metrics.get(k, 0) for k in ("likes", "comments", "shares", "saves")) / max(1, views)
        score = math.log1p(views) * (1 + min(engagement, 1) * 5)
        topic = normalized(idea.topic if idea else "drive")
        topics[topic].append(score)
        row = session.scalar(select(ContentPerformance).where(ContentPerformance.video_id == video_id))
        if not row:
            row = ContentPerformance(account_id=account.id, video_id=video_id)
            session.add(row)
        row.score, row.topic, row.hook = score, topic, idea.hook if idea else ""
        row.metrics = {**metrics, "engagement_rate": engagement}
        if idea:
            hooks.append({"hook": idea.hook, "score": score, "topic": topic})
    topic_scores = {topic: sum(values) / len(values) for topic, values in topics.items()}
    maximum = max(topic_scores.values(), default=1) or 1
    # Exploration remains explicit: performance affects only a small ranking boost.
    # Re-read under lock so a simultaneous admin edit cannot be overwritten.
    account = session.scalar(select(Account).where(Account.id == account.id).with_for_update().execution_options(populate_existing=True))
    ctx.guard()
    by_platform = defaultdict(list)
    for sample in samples:
        by_platform[sample["platform"]].append(sample["score"])
    platform_means = {platform: sum(values) / len(values) for platform, values in by_platform.items()}
    for sample in samples:
        scale = platform_means[sample["platform"]]
        sample["score"] = sample["score"] / scale if scale > 0 else 0
    config, feature_decisions = feature_strategy(account, samples)
    config["optimization"] = {"updated_at": utcnow().isoformat(),
        "topic_scores": {k: round(v / maximum, 4) for k, v in topic_scores.items()},
        "successful_hooks": sorted(hooks, key=lambda x: x["score"], reverse=True)[:10],
        "underperforming_hooks": sorted(hooks, key=lambda x: x["score"])[:10],
        "exploration_share": 0.25, "sample_posts": len(latest),
        "features": feature_decisions, "posting_schedule": feature_decisions["posting_schedule"],
        "limitations": "API tarafından sağlanmayan retention/CTR metrikleri tahmin edilmez."}
    account.settings = config
    session.commit()
    return {"sample_posts": len(latest), "topics": len(topics)}


def run_pipeline(account_id):
    from factory.services.scheduler import inventory_counts, inventory_limits
    with SessionLocal() as session:
        account = session.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not account:
            raise PermanentFailure("Hesap bulunamadı.")
        assert_running(session, account.id)
        if inventory_counts(session, account.id) >= inventory_limits(account)[2]:
            raise PermanentFailure("Maksimum içerik envanterine ulaşıldı.")
        kind = "DRIVE_SYNC" if account.source_mode == "DRIVE_ONLY" else "AI_PIPELINE"
        job = enqueue_job(session, kind, account_id=account.id, priority=90)
        session.commit()
        return {"job_id": job.id, "status": job.status}


from factory.services.control import generate_ideas, connect_drive_library

from factory.services.instagram_insights import collect as collect_direct_instagram_insights

def collect_instagram_insights(ctx):
    connection = ctx.session.scalar(select(Connection).where(Connection.account_id == ctx.job.account_id, Connection.platform == 'instagram'))
    if connection and (connection.settings or {}).get('publish_via') == 'buffer':
        return {'skipped': 'buffer_instagram_has_no_direct_meta_analytics_grant'}
    return collect_direct_instagram_insights(ctx)


HANDLERS = {
    "INSTAGRAM_INSIGHTS": collect_instagram_insights,
    "AI_IDEAS": generate_ideas,
    "CONNECT_DRIVE_LIBRARY": connect_drive_library,
    "AI_PIPELINE": production,
    "DRIVE_PIPELINE": lambda ctx: production(ctx, "DRIVE"),
    "DRIVE_SYNC": drive_sync,
    "PUBLISH": publishing,
    "DRIVE_ARCHIVE": archive,
    "DRIVE_FAILURE_UPLOAD": failure_upload,
    "RERENDER": rerender,
    "ANALYTICS": analytics,
    "OPTIMIZE": optimize,
}
