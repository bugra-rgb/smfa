"""CI PostgreSQL bağlantısında gerçek constraint ve transaction denemesi.

Yalnız test verisi oluşturur, test sonunda tüm işlemi geri alır.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect, select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from factory.db import engine  # noqa: E402
from factory.models import Account, Connection, DriveFile, Job, PublishJob, Video  # noqa: E402


def main():
    if engine.dialect.name != "postgresql":
        raise SystemExit("Bu kontrol gerçek PostgreSQL gerektirir.")
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                account = Account(name="CI transaction test")
                session.add(account)
                session.flush()
                video = Video(account_id=account.id, title="CI video")
                platform = Connection(account_id=account.id, platform="youtube")
                session.add_all([video, platform])
                session.flush()
                session.add(PublishJob(account_id=account.id, video_id=video.id, connection_id=platform.id,
                                       platform="youtube", idempotency_key=f"ci-first:{video.id}"))
                session.flush()
                duplicate_rejected = False
                try:
                    with session.begin_nested():
                        session.add(PublishJob(account_id=account.id, video_id=video.id, connection_id=platform.id,
                                               platform="youtube", idempotency_key=f"ci-second:{video.id}"))
                        session.flush()
                except IntegrityError:
                    duplicate_rejected = True
                assert duplicate_rejected, "PostgreSQL yinelenen yayını engellemedi."
                job = Job(account_id=account.id, kind="test", idempotency_key=f"ci-job:{video.id}")
                session.add(job)
                session.flush()
                claimed = session.scalar(select(Job).where(Job.id == job.id).with_for_update(skip_locked=True))
                assert claimed is not None and claimed.id == job.id
                session.add(DriveFile(account_id=account.id, drive_file_id=f"ci-drive:{video.id}",
                                      filename="ci.mp4", video_id=video.id))
                session.flush()
                session.refresh(video)
                assert video.created_at.tzinfo is not None
                assert any(fk["referred_table"] == "videos" for fk in inspect(connection).get_foreign_keys("drive_files"))
        finally:
            transaction.rollback()
    print("PostgreSQL: duplicate yayın, row lock, Drive FK ve UTC kontrolleri başarılı.")


if __name__ == "__main__":
    main()
