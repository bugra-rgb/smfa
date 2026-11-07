#!/usr/bin/env python3
"""Instagram hesaplarını konu ve OAuth onayı bekleyen pasif kayıtlar olarak hazırlar.

Örnek (backend konteynerinde):
    python scripts/setup_accounts.py demo.bird demo.bear

Mevcut hesaplar ve tokenlar değiştirilmez. Instagram parolası kabul edilmez.
Üretim, yayın, sağlayıcı isteği veya iş kuyruğu başlatılmaz.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Existing REST adapters; documented on 2026-09-14. These are configuration
# defaults, not evidence that a particular project has access or quota.
OPENAI_MODEL_DEFAULTS = {
    "OPENAI_MODEL": "gpt-4.1-mini",
    "OPENAI_IMAGE_MODEL": "gpt-image-1.5",
    "OPENAI_VOICE_MODEL": "gpt-4o-mini-tts",
    "OPENAI_TRANSCRIPTION_MODEL": "whisper-1",
}


def normalize_username(value: str) -> str:
    username = value.strip().removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9_](?:[a-z0-9_.]{0,28}[a-z0-9_])?", username) or ".." in username:
        raise ValueError("Instagram kullanıcı adı 1–30 harf, rakam, alt çizgi veya nokta içermeli.")
    return username


def model_defaults(existing: dict[str, Any]) -> dict[str, str]:
    """Only return missing model values; caller owns secure environment updates."""
    return {key: value for key, value in OPENAI_MODEL_DEFAULTS.items() if not existing.get(key)}


def prepare_accounts(session, usernames: list[str]) -> list[dict[str, Any]]:
    """Caller owns transaction. Validate first, preserve matches, create only missing."""
    from sqlalchemy import select, text

    from factory.models import Account, Connection
    from factory.schemas import AccountCreate, DEFAULT_ACCOUNT_SETTINGS

    names = list(dict.fromkeys(normalize_username(value) for value in usernames))
    if not names:
        raise ValueError("En az bir Instagram kullanıcı adı gerekli.")
    # Serialize reruns of this setup command on PostgreSQL; never lock workers.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(73661353065001)"))
    existing_accounts = session.scalars(select(Account)).all()
    matched = {}
    for name in names:
        matches = [account for account in existing_accounts
                   if (account.username or "").strip().lstrip("@").lower() == name
                   or (not (account.username or "").strip()
                       and account.name.strip().lstrip("@").lower() == name)]
        if len(matches) > 1:
            raise ValueError(f"@{name} için birden fazla kayıt var; hiçbir hesap değiştirilmedi.")
        matched[name] = matches[0] if matches else None

    results = []
    for name in names:
        account = matched[name]
        if account is not None:
            results.append({"username": name, "account_id": account.id, "result": "existing_preserved"})
            continue
        settings = deepcopy(DEFAULT_ACCOUNT_SETTINGS)
        settings.update({
            "llm_provider": "openai", "image_provider": "openai", "video_provider": "image_motion",
            "voice": {"provider": "openai", "voice": "alloy", "speed": 1.0, "pitch": 0},
            "schedule": [],
            "setup_pending": ["content_topic", "instagram_oauth"],
        })
        payload = AccountCreate(
            name=f"@{name}", username=name, niche="İçerik konusu henüz belirlenmedi",
            active=False, daily_target=0, auto_publish=False, approval_mode="MANUAL_APPROVAL",
            settings=settings,
        )
        account = Account(**payload.model_dump())
        session.add(account)
        session.flush()
        session.add(Connection(
            account_id=account.id, platform="instagram", status="DISCONNECTED",
            settings={"username": name},
        ))
        results.append({"username": name, "account_id": account.id, "result": "created_inactive"})
    session.flush()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("usernames", nargs="*", help="Instagram kullanıcı adları; parola girmeyin")
    parser.add_argument("--model-defaults", action="store_true", help="Belgelenmiş model varsayılanlarını yazdır")
    args = parser.parse_args()
    if args.model_defaults:
        print(json.dumps(OPENAI_MODEL_DEFAULTS, ensure_ascii=False))
        return
    if not args.usernames:
        parser.error("En az bir kullanıcı adı gerekiyor.")
    from factory.db import session_scope
    try:
        with session_scope() as session:
            results = prepare_accounts(session, args.usernames)
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({"accounts": results, "production_started": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
