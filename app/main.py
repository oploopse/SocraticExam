import asyncio
import logging
import math
import secrets
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .config import settings
from .ingest import SUPPORTED_EXTENSIONS, UnsupportedFile, chunk_blocks, parse_document
from .interview import director
from .interview.engine import InterviewEngine, NoConceptsInRange, SessionFinished, SessionNotFound
from .knowledge import build_knowledge_map
from .llm import LLMError, LLMNotConfigured
from .ratelimit import limiter
from .schemas import DocumentRecord, Report, SessionRecord
from .storage import new_id, store
from .voice import VoiceBridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.gemini_api_key:
        log.info(
            "GEMINI_API_KEY đã có. Não=%s, nhanh=%s, live=%s",
            settings.brain_model,
            settings.fast_model,
            settings.live_model,
        )
    else:
        log.warning("Chưa có GEMINI_API_KEY. Hãy điền key vào file .env rồi chạy lại .\\.venv\\Scripts\\python.exe -m app")
    yield


app = FastAPI(title="AI Interviewer", lifespan=lifespan)
engine = InterviewEngine(store)


@app.exception_handler(LLMNotConfigured)
async def _llm_not_configured(_: Request, exc: LLMNotConfigured) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(LLMError)
async def _llm_error(_: Request, exc: LLMError) -> JSONResponse:
    log.warning("LLM error: %s", exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


MAX_PAGE_SPAN = 100


class SessionCreate(BaseModel):
    document_id: str
    learner_name: str = Field(default="bạn", max_length=60)
    mode: Literal["text", "voice"] = "voice"
    # Các tuỳ chọn của màn Thiết lập (web/API_CONTRACT.md mục 2); bỏ trống thì dùng mặc định.
    page_from: int | None = Field(default=None, ge=1)
    page_to: int | None = Field(default=None, ge=1)
    question_count: int | None = Field(default=None, ge=5, le=10)
    duration_minutes: int | None = Field(default=None, ge=5, le=15)

    @model_validator(mode="after")
    def _check_pages(self) -> "SessionCreate":
        if self.page_to is not None:
            start = self.page_from or 1
            if self.page_to < start:
                raise ValueError("Trang kết thúc phải lớn hơn hoặc bằng trang bắt đầu")
            if self.page_to - start + 1 > MAX_PAGE_SPAN:
                raise ValueError(f"Tối đa {MAX_PAGE_SPAN} trang mỗi lần")
        return self

    def options(self) -> dict:
        return {
            "time_limit_minutes": self.duration_minutes,
            "question_limit": self.question_count,
            "page_from": self.page_from,
            "page_to": self.page_to,
        }


class AnswerIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class LoginIn(BaseModel):
    token: str = Field(min_length=1, max_length=256)


def _require_access(request: Request) -> None:
    """Require the configured bearer token when one is configured."""
    expected = settings.app_access_token
    if not expected:
        return
    authorization = request.headers.get("authorization", "")
    supplied = authorization.removeprefix("Bearer ").strip() or request.cookies.get("app_access_token", "")
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "Cần nhập mật khẩu để sử dụng ứng dụng")


def _access_dependency(request: Request) -> None:
    _require_access(request)


def _rate_limited(bucket: str, label: str):
    """Dependency giới hạn số lượt mỗi giờ theo IP; ngưỡng đọc từ settings.rate_limit_<bucket>_per_hour."""

    def dependency(request: Request) -> None:
        limit = getattr(settings, f"rate_limit_{bucket}_per_hour")
        if limit <= 0:
            return
        client_ip = request.client.host if request.client else "unknown"
        wait_s = limiter.hit(bucket, client_ip, limit)
        if wait_s is not None:
            minutes = max(1, math.ceil(wait_s / 60))
            raise HTTPException(
                429,
                f"Bạn đã dùng hết {limit} lượt {label} trong một giờ. Thử lại sau khoảng {minutes} phút.",
                headers={"Retry-After": str(math.ceil(wait_s))},
            )

    return dependency


_upload_limit = Depends(_rate_limited("uploads", "tải tài liệu"))
_session_limit = Depends(_rate_limited("sessions", "tạo buổi phỏng vấn"))
_message_limit = Depends(_rate_limited("messages", "gửi câu trả lời"))


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    if not settings.app_access_token or not secrets.compare_digest(body.token, settings.app_access_token):
        raise HTTPException(401, "Mật khẩu không đúng")
    response.set_cookie(
        "app_access_token",
        body.token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return {"ok": True}


def _document_or_404(doc_id: str) -> DocumentRecord:
    doc = store.get_document(doc_id)
    if doc is None:
        raise HTTPException(404, "Không tìm thấy tài liệu")
    return doc


def _session_or_404(session_id: str) -> SessionRecord:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "Không tìm thấy phiên phỏng vấn")
    return session


def document_summary(doc: DocumentRecord) -> dict:
    km = doc.knowledge_map
    return {
        "id": doc.id,
        "filename": doc.filename,
        "created_at": doc.created_at.isoformat(),
        "title": km.title,
        "summary": km.summary,
        "truncated": doc.truncated,
        "chunk_count": len(doc.chunks),
        "page_count": max((c.page for c in doc.chunks if c.page), default=None),
        "concepts": [
            {"id": c.id, "name": c.name, "summary": c.summary, "importance": c.importance, "question_count": len(c.questions)}
            for c in km.concepts
        ],
    }


def session_public(session: SessionRecord, doc: DocumentRecord) -> dict:
    """Thông tin phiên an toàn để hiển thị cho người học (không chứa kết quả chấm)."""
    return {
        "id": session.id,
        "document_id": session.document_id,
        "document_title": doc.knowledge_map.title,
        "learner_name": session.learner_name,
        "mode": session.mode,
        "status": session.status,
        "created_at": session.created_at.isoformat(),
        "time_limit_seconds": director.time_limit_minutes(session) * 60,
        "question_limit": session.question_limit,
        "page_from": session.page_from,
        "page_to": session.page_to,
        "turns": [{"role": t.role, "text": t.text, "at": t.at.isoformat()} for t in session.turns],
        "has_report": session.report is not None,
    }


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "llm_configured": bool(settings.gemini_api_key),
        "models": {"brain": settings.brain_model, "fast": settings.fast_model, "live": settings.live_model},
        "interview_minutes": settings.interview_minutes,
        "session_options": True,
    }


@app.post("/api/documents")
async def upload_document(
    file: UploadFile = File(...), _: None = Depends(_access_dependency), __: None = _upload_limit
) -> dict:
    filename = file.filename or "tai-lieu.txt"
    if not any(filename.lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS):
        raise HTTPException(400, "Chỉ hỗ trợ file PDF, DOCX, TXT hoặc MD")
    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File lớn hơn {settings.max_upload_mb} MB")
    try:
        blocks = await asyncio.to_thread(parse_document, filename, data)
    except UnsupportedFile as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        log.exception("Không đọc được file %s", filename)
        raise HTTPException(400, "Không đọc được file. File có thể bị hỏng hoặc được bảo vệ bằng mật khẩu.") from exc

    chunks = chunk_blocks(blocks)
    if not chunks:
        raise HTTPException(
            400, "Không tìm thấy chữ trong tài liệu. Nếu là PDF scan từ ảnh, hãy chạy OCR trước khi tải lên."
        )
    try:
        knowledge_map, truncated = await build_knowledge_map(filename, chunks)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    doc = DocumentRecord(
        id=new_id(),
        filename=filename,
        char_count=sum(len(c.text) for c in chunks),
        truncated=truncated,
        chunks=chunks,
        knowledge_map=knowledge_map,
    )
    store.save_document(doc)
    return document_summary(doc)


@app.get("/api/documents")
def list_documents(_: None = Depends(_access_dependency)) -> list[dict]:
    return [document_summary(doc) for doc in store.list_documents()]


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, _: None = Depends(_access_dependency)) -> dict:
    doc = _document_or_404(doc_id)
    return {**document_summary(doc), "knowledge_map": doc.knowledge_map.model_dump()}


@app.post("/api/sessions")
async def create_session(body: SessionCreate, _: None = Depends(_access_dependency), __: None = _session_limit) -> dict:
    doc = _document_or_404(body.document_id)
    learner_name = body.learner_name.strip() or "bạn"
    page_count = max((c.page for c in doc.chunks if c.page), default=None)
    if page_count and body.page_from and body.page_from > page_count:
        raise HTTPException(422, f"File chỉ có {page_count} trang")
    try:
        if body.mode == "text":
            session, message = await engine.start_text_session(doc, learner_name, body.options())
            return {"session": session_public(session, doc), "message": message}
        session = engine.start_voice_session(doc, learner_name, body.options())
    except NoConceptsInRange as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"session": session_public(session, doc), "message": None}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, _: None = Depends(_access_dependency)) -> dict:
    session = _session_or_404(session_id)
    return session_public(session, _document_or_404(session.document_id))


@app.post("/api/sessions/{session_id}/messages")
async def post_message(
    session_id: str, body: AnswerIn, _: None = Depends(_access_dependency), __: None = _message_limit
) -> dict:
    session = _session_or_404(session_id)
    if session.mode != "text":
        raise HTTPException(409, "Phiên này dùng chế độ giọng nói")
    doc = _document_or_404(session.document_id)
    try:
        session, reply = await engine.answer_text(session_id, doc, body.text.strip())
    except SessionFinished as exc:
        raise HTTPException(409, "Buổi phỏng vấn đã kết thúc") from exc
    except SessionNotFound as exc:
        raise HTTPException(404, "Không tìm thấy phiên phỏng vấn") from exc
    return {"message": reply, "finished": session.status == "finished"}


@app.get("/api/sessions/{session_id}/insights")
def get_insights(session_id: str, _: None = Depends(_access_dependency)) -> dict:
    session = _session_or_404(session_id)
    return engine.insights(session, _document_or_404(session.document_id))


@app.post("/api/sessions/{session_id}/finish")
async def finish_session(session_id: str, _: None = Depends(_access_dependency)) -> Report:
    session = _session_or_404(session_id)
    if session.report is not None:
        return session.report
    return await engine.build_report(session_id, _document_or_404(session.document_id))


@app.get("/api/sessions/{session_id}/report")
def get_report(session_id: str, _: None = Depends(_access_dependency)) -> Report:
    session = _session_or_404(session_id)
    if session.report is None:
        raise HTTPException(404, "Phiên này chưa có báo cáo")
    return session.report


@app.websocket("/api/sessions/{session_id}/voice")
async def voice_socket(websocket: WebSocket, session_id: str) -> None:
    if settings.app_access_token:
        authorization = websocket.headers.get("authorization", "")
        supplied = authorization.removeprefix("Bearer ").strip() or websocket.cookies.get("app_access_token", "")
        if not secrets.compare_digest(supplied, settings.app_access_token):
            await websocket.close(code=4401)
            return
    session = store.get_session(session_id)
    doc = store.get_document(session.document_id) if session else None
    if session is None or doc is None or session.mode != "voice":
        await websocket.close(code=4404)
        return
    if session.status != "active" or session_id in engine.active_voice:
        await websocket.close(code=4409)
        return
    await websocket.accept()
    await VoiceBridge(websocket, engine, session, doc).run()


app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
