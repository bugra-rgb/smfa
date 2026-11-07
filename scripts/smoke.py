#!/usr/bin/env python3
"""Gerçek FFmpeg + sahte harici servislerle tek içeriklik izole uçtan uca kontrol."""
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
workspace = Path(tempfile.mkdtemp(prefix="factory-smoke-"))
os.environ["MODE"] = "mock"
os.environ["DATABASE_URL"] = f"sqlite:///{workspace / 'smoke.db'}"
os.environ["DATA_DIR"] = str(workspace)

from sqlalchemy import select  # noqa: E402
from factory.db import SessionLocal, init_db  # noqa: E402
from factory.models import Account, Connection, Job, PublishedPost, Video  # noqa: E402
from factory.security import now_utc  # noqa: E402
from factory.services.jobs import process_job  # noqa: E402
from factory.services.pipeline import run_pipeline  # noqa: E402
from factory.services.scheduler import tick  # noqa: E402


def main():
    init_db()
    with SessionLocal.begin() as session:
        account = Account(name="TEST – FutbolTR", niche="Futbol tarihi", language="tr", daily_target=1,
            timezone="UTC", auto_publish=True, approval_mode="AUTO_PUBLISH", source_mode="AI_ONLY",
            settings={"duration": 6, "width": 320, "height": 568, "schedule": [now_utc().strftime("%H:%M")],
                      "inventory_min": 1, "inventory_target": 1, "inventory_max": 1, "subtitle": {"enabled": True, "font_size": 22}})
        session.add(account)
        session.flush()
        account_id = account.id
        for platform in ("youtube", "instagram", "tiktok"):
            session.add(Connection(account_id=account_id, platform=platform, external_id=f"mock-{platform}", status="CONNECTED", settings={"mock": True}))
    queued = run_pipeline(account_id)
    production_result = process_job(queued["job_id"])
    if production_result["status"] != "SUCCEEDED":
        raise SystemExit(json.dumps(production_result, ensure_ascii=False))
    scheduler_result = tick()
    for _ in range(4):
        with SessionLocal() as session:
            ids = list(session.scalars(select(Job.id).where(Job.status.in_(["QUEUED", "RETRYING"]), Job.run_after <= now_utc())))
        if not ids:
            break
        for job_id in ids:
            process_job(job_id)
    with SessionLocal() as session:
        videos = list(session.scalars(select(Video)))
        posts = list(session.scalars(select(PublishedPost)))
        jobs = list(session.scalars(select(Job)))
        result = {"mode": "mock", "actual_ffmpeg_render": True,
                  "qa_passed": all(v.qa.get("passed") for v in videos),
                  "videos": len(videos), "simulated_platform_posts": len(posts),
                  "real_platform_posts": 0, "scheduler": scheduler_result,
                  "jobs": [{"kind": j.kind, "status": j.status} for j in jobs],
                  "video_path": videos[0].local_path if videos else None,
                  "workspace": str(workspace)}
    if not result["qa_passed"] or len(posts) != 3:
        raise SystemExit(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
