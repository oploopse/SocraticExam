import logging
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from .config import settings
from .schemas import DocumentRecord, SessionRecord

log = logging.getLogger(__name__)

_ID_PATTERN = re.compile(r"[a-f0-9]{12}")

Kind = Literal["documents", "sessions"]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def is_valid_id(value: str) -> bool:
    return bool(_ID_PATTERN.fullmatch(value))


def _write_atomic(path: Path, content: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


class Store:
    """Lưu tài liệu và phiên phỏng vấn, có cache trong bộ nhớ.

    Cache là bản chính khi ứng dụng đang chạy (một instance, xem render.yaml); lớp con chỉ lo
    ghi JSON xuống nơi lưu lâu dài và đọc lại khi khởi động lại.
    """

    def __init__(self) -> None:
        self._docs: dict[str, DocumentRecord] = {}
        self._sessions: dict[str, SessionRecord] = {}

    def _write(self, kind: Kind, record_id: str, content: str) -> None:
        raise NotImplementedError

    def _read(self, kind: Kind, record_id: str) -> str | None:
        raise NotImplementedError

    def _ids(self, kind: Kind) -> list[str]:
        raise NotImplementedError

    def save_document(self, doc: DocumentRecord) -> None:
        self._docs[doc.id] = doc
        self._write("documents", doc.id, doc.model_dump_json(indent=2))

    def get_document(self, doc_id: str) -> DocumentRecord | None:
        if not is_valid_id(doc_id):
            return None
        if doc_id not in self._docs:
            content = self._read("documents", doc_id)
            if content is None:
                return None
            self._docs[doc_id] = DocumentRecord.model_validate_json(content)
        return self._docs[doc_id]

    def list_documents(self) -> list[DocumentRecord]:
        # Gộp cả cache: với PostgresStore, tài liệu vừa lưu có thể chưa kịp ghi xuống database.
        doc_ids = set(self._ids("documents")) | set(self._docs)
        docs = [self.get_document(doc_id) for doc_id in doc_ids]
        return sorted((d for d in docs if d), key=lambda d: d.created_at, reverse=True)

    def find_document_by_hash(self, content_hash: str) -> DocumentRecord | None:
        """Tài liệu đã soạn bản đồ kiến thức từ đúng file này (cùng SHA-256), nếu có."""
        return next((d for d in self.list_documents() if d.content_hash == content_hash), None)

    def save_session(self, session: SessionRecord) -> None:
        self._sessions[session.id] = session
        self._write("sessions", session.id, session.model_dump_json(indent=2))

    def get_session(self, session_id: str) -> SessionRecord | None:
        if not is_valid_id(session_id):
            return None
        if session_id not in self._sessions:
            content = self._read("sessions", session_id)
            if content is None:
                return None
            self._sessions[session_id] = SessionRecord.model_validate_json(content)
        return self._sessions[session_id]

    def close(self) -> None:
        """Ghi nốt những gì còn chờ trước khi tắt ứng dụng."""


class JsonStore(Store):
    """Lưu mỗi tài liệu, mỗi phiên thành một file JSON trong data/. Dùng khi chạy local."""

    def __init__(self, root: Path):
        super().__init__()
        self._dirs = {"documents": root / "documents", "sessions": root / "sessions"}
        for directory in self._dirs.values():
            directory.mkdir(parents=True, exist_ok=True)

    def _write(self, kind: Kind, record_id: str, content: str) -> None:
        _write_atomic(self._dirs[kind] / f"{record_id}.json", content)

    def _read(self, kind: Kind, record_id: str) -> str | None:
        path = self._dirs[kind] / f"{record_id}.json"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def _ids(self, kind: Kind) -> list[str]:
        return [p.stem for p in self._dirs[kind].glob("*.json")]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    kind text NOT NULL,
    id text NOT NULL,
    data jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS records_content_hash
    ON records ((data->>'content_hash')) WHERE kind = 'documents';
"""


class PostgresStore(Store):
    """Lưu vào PostgreSQL (ví dụ Neon), để dữ liệu còn nguyên khi Render deploy lại hoặc ngủ dậy.

    Ghi theo kiểu write-behind: lệnh ghi được đẩy sang một luồng riêng theo đúng thứ tự, nên
    cầu nối giọng nói không phải chờ database (Neon free có thể mất vài giây để thức dậy).
    """

    def __init__(self, conninfo: str):
        from psycopg_pool import ConnectionPool

        super().__init__()
        self._pool = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=3,
            kwargs={"autocommit": True},
            # Neon đóng kết nối khi compute ngủ; kiểm tra trước khi dùng để tự mở lại.
            check=ConnectionPool.check_connection,
            open=True,
        )
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pg-writer")
        with self._pool.connection() as conn:
            conn.execute(_SCHEMA)

    def _write(self, kind: Kind, record_id: str, content: str) -> None:
        self._writer.submit(self._upsert, kind, record_id, content)

    def _upsert(self, kind: Kind, record_id: str, content: str) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "INSERT INTO records (kind, id, data) VALUES (%s, %s, %s::jsonb) "
                    "ON CONFLICT (kind, id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                    (kind, record_id, content),
                )
        except Exception:
            log.exception("Không ghi được %s/%s vào PostgreSQL", kind, record_id)

    def _read(self, kind: Kind, record_id: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT data::text FROM records WHERE kind = %s AND id = %s", (kind, record_id)).fetchone()
        return row[0] if row else None

    def _ids(self, kind: Kind) -> list[str]:
        with self._pool.connection() as conn:
            return [row[0] for row in conn.execute("SELECT id FROM records WHERE kind = %s", (kind,))]

    def find_document_by_hash(self, content_hash: str) -> DocumentRecord | None:
        cached = next((d for d in self._docs.values() if d.content_hash == content_hash), None)
        if cached:
            return cached
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT id FROM records WHERE kind = 'documents' AND data->>'content_hash' = %s LIMIT 1",
                (content_hash,),
            ).fetchone()
        return self.get_document(row[0]) if row else None

    def flush(self) -> None:
        """Chờ các lệnh ghi đang xếp hàng chạy xong (dùng trong test)."""
        self._writer.submit(lambda: None).result()

    def close(self) -> None:
        self._writer.shutdown(wait=True)
        self._pool.close()


def create_store() -> Store:
    if settings.database_url:
        log.info("Lưu dữ liệu vào PostgreSQL")
        return PostgresStore(settings.database_url)
    return JsonStore(settings.data_dir)


store = create_store()
