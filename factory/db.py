"""SQLAlchemy bağlantıları ve işlem sınırları."""
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from factory.config import settings
from factory.models import Base


def make_engine(url: str):
    options: dict[str, Any] = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url in {"sqlite://", "sqlite+pysqlite://"}:
            options["poolclass"] = StaticPool
    else:
        options.update(pool_size=5, max_overflow=5, pool_recycle=1800)
    db_engine = create_engine(url, **options)
    if url.startswith("sqlite"):
        @event.listens_for(db_engine, "connect")
        def _sqlite_pragmas(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return db_engine


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    """Yalnız SQLite test/demo kurulumunda; production şeması Alembic ile taşınır."""
    if engine.dialect.name != "sqlite":
        raise RuntimeError("PostgreSQL kurulumu için 'alembic upgrade head' kullanın.")
    Base.metadata.create_all(engine)
