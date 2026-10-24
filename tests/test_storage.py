"""Cùng một bộ test cho JsonStore và PostgresStore.

PostgresStore chỉ chạy khi có TEST_DATABASE_URL (CI dựng sẵn một Postgres, xem .github/workflows/tests.yml).
"""

import os
from datetime import timedelta

import pytest

import app.main as main
from app.storage import JsonStore, PostgresStore

from .factories import make_concept, make_doc, make_session

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def _reset_postgres():
    import psycopg

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS records")


@pytest.fixture(params=["json", "postgres"])
def open_store(request, tmp_path):
    """Trả về hàm mở một store mới trên cùng nơi lưu, để kiểm tra dữ liệu còn sau khi khởi động lại."""
    opened = []
    if request.param == "postgres":
        if not TEST_DATABASE_URL:
            pytest.skip("Chưa đặt TEST_DATABASE_URL")
        _reset_postgres()

        def factory():
            store = PostgresStore(TEST_DATABASE_URL)
            opened.append(store)
            return store
    else:

        def factory():
            store = JsonStore(tmp_path)
            opened.append(store)
            return store

    yield factory
    for store in opened:
        store.close()


def _persist(store):
    if isinstance(store, PostgresStore):
        store.flush()


def test_document_and_session_survive_restart(open_store):
    store = open_store()
    doc = make_doc(make_concept("c1"))
    session = make_session(doc)
    store.save_document(doc)
    store.save_session(session)
    _persist(store)

    reopened = open_store()
    assert reopened.get_document(doc.id) == doc
    assert reopened.get_session(session.id) == session


def test_session_updates_overwrite_previous_version(open_store):
    store = open_store()
    doc = make_doc(make_concept("c1"))
    session = make_session(doc)
    store.save_session(session)
    session.status = "finished"
    store.save_session(session)
    _persist(store)

    assert open_store().get_session(session.id).status == "finished"


def test_list_documents_newest_first_and_find_by_hash(open_store):
    store = open_store()
    older = make_doc(make_concept("c1")).model_copy(update={"id": "111111111111", "content_hash": "aa"})
    newer = older.model_copy(update={"id": "222222222222", "content_hash": "bb", "created_at": older.created_at + timedelta(minutes=1)})
    store.save_document(older)
    store.save_document(newer)
    _persist(store)

    reopened = open_store()
    assert [d.id for d in reopened.list_documents()] == ["222222222222", "111111111111"]
    assert reopened.find_document_by_hash("aa").id == "111111111111"
    assert reopened.find_document_by_hash("zz") is None


def test_unknown_or_invalid_ids_return_none(open_store):
    store = open_store()
    assert store.get_document("000000000000") is None
    assert store.get_session("../../etc/passwd") is None


def test_uploading_the_same_file_twice_reuses_knowledge_map(client, monkeypatch):
    calls = []
    build = main.build_knowledge_map

    async def counting_build(filename, chunks):
        calls.append(filename)
        return await build(filename, chunks)

    monkeypatch.setattr(main, "build_knowledge_map", counting_build)
    content = "Nội dung bài học.\n\nĐoạn hai.".encode()

    first = client.post("/api/documents", files={"file": ("bai.txt", content)}).json()
    second = client.post("/api/documents", files={"file": ("ban-sao.txt", content)}).json()
    other = client.post("/api/documents", files={"file": ("khac.txt", "Bài khác.".encode())}).json()

    assert second["id"] == first["id"]
    assert other["id"] != first["id"]
    assert calls == ["bai.txt", "khac.txt"]
