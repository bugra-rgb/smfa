#!/usr/bin/env python3
"""İlk kurulum için gizli ayarları oluşturur; günlük kullanımda gerekmez."""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def generate_env(email: str, base_url: str, production: bool) -> None:
    target = ROOT / ".env"
    if target.exists():
        print(".env zaten var; mevcut ayarlar ve anahtarlar korundu.")
        return
    if any(c in email for c in "\r\n") or "@" not in email or len(email) > 320:
        raise SystemExit("Geçerli bir --email gerekiyor.")
    base_url = base_url.rstrip("/")
    parsed = urlparse(base_url)
    if (not parsed.hostname or parsed.scheme not in {"http", "https"}
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or any(c in base_url for c in "\r\n")):
        raise SystemExit("Geçerli --base-url gerekiyor.")
    if production and parsed.scheme != "https":
        raise SystemExit("Production için --base-url https:// ile başlamalı.")
    postgres_password = secrets.token_hex(24)
    redis_password = secrets.token_hex(24)
    values = {
        "MODE": "production" if production else "mock",
        "POSTGRES_PASSWORD": postgres_password,
        "REDIS_PASSWORD": redis_password,
        "DATABASE_URL": f"postgresql+psycopg://factory:{postgres_password}@postgres:5432/factory",
        "REDIS_URL": f"redis://:{redis_password}@redis:6379/0",
        "DATA_DIR": "./data",
        "SECRET_KEY": secrets.token_hex(32),
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "PUBLIC_BASE_URL": base_url,
        "FRONTEND_ORIGIN": base_url,
        "ALLOWED_HOSTS": f"{parsed.hostname},localhost,127.0.0.1,backend",
        "SITE_DOMAIN": parsed.hostname if production else "",
        "ADMIN_EMAIL": email,
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "WORKER_CONCURRENCY": "2",
        "BIND_HOST": "127.0.0.1",
        "WEB_PORT": "3000",
    }
    template = ROOT / ".env.example"
    lines = template.read_text(encoding="utf-8").splitlines() if template.exists() else []
    written = set()
    rendered = ["# Otomatik oluşturuldu. Bu dosyayı paylaşmayın veya Git'e eklemeyin."]
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in values:
                rendered.append(f"{key}={values[key]}")
                written.add(key)
                continue
        rendered.append(line)
    rendered.extend(f"{key}={value}" for key, value in values.items() if key not in written)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("\n".join(rendered) + "\n")
    print(".env oluşturuldu. Yönetici giriş bilgileri ADMIN_EMAIL ve ADMIN_PASSWORD satırlarında.")
    print("Başlatmak için: docker compose up -d --build")


def init_admin() -> None:
    from sqlalchemy import select
    from factory.config import settings
    from factory.db import session_scope
    from factory.models import SystemState, User
    from factory.security import hash_password

    with session_scope() as session:
        existing = session.scalar(select(User).where(User.email == settings.admin_email.lower()))
        if existing is None:
            active_admin = session.scalar(select(User).where(User.role == "admin", User.active.is_(True)))
            if active_admin and not settings.admin_password:
                print("Etkin yönetici mevcut; yeni yönetici oluşturulmadı.")
            elif not settings.admin_email or len(settings.admin_password) < 12:
                raise SystemExit("İlk kurulum için ADMIN_EMAIL ve en az 12 karakter ADMIN_PASSWORD gerekli.")
            else:
                session.add(User(email=settings.admin_email.lower(), password_hash=hash_password(settings.admin_password),
                                 role="admin"))
                print("Yönetici hesabı oluşturuldu.")
        else:
            print("Yönetici hesabı zaten var; parola değiştirilmedi.")
        if session.get(SystemState, "global") is None:
            session.add(SystemState(id="global", paused=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default="admin@example.com", help="İlk yöneticinin e-posta adresi")
    parser.add_argument("--base-url", default="http://localhost:3000")
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--init-admin", action="store_true", help="Migration sonrasında yönetici oluşturur")
    args = parser.parse_args()
    if args.init_admin:
        init_admin()
    else:
        generate_env(args.email, args.base_url, args.production)


if __name__ == "__main__":
    main()
