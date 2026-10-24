import asyncio
import logging
import time
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from .config import settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_RETRYABLE_CODES = {429, 500, 502, 503, 504}
# 429/503 là quá tải hoặc hết quota của đúng model đó; gọi lại cùng model thường vẫn lỗi.
_IMMEDIATE_FAILOVER = {429, 503}
_MAX_ATTEMPTS = 2
# Dùng khi model đang chọn không phản hồi. Thứ tự gần chất lượng của model não nhất.
_FALLBACK_MODELS = (
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
)

# Model vừa quá tải hoặc quá chậm bị bỏ qua trong một khoảng ngắn, để các lượt sau đi thẳng tới model dự phòng
# thay vì lần nào cũng chờ vài giây mới nhận lỗi 503.
COOLDOWN_S = 60.0
_cooldown_until: dict[str, float] = {}

_client: genai.Client | None = None


class LLMNotConfigured(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


def _loop_supports_happy_eyeballs() -> bool:
    """Chỉ event loop chuẩn của asyncio nhận happy_eyeballs_delay.

    uvloop (uvicorn[standard] dùng trên Linux, ví dụ trên Render) báo TypeError với tham số này.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    return isinstance(loop, asyncio.BaseEventLoop)


def prefer_reachable_ip() -> None:
    """Bật Happy Eyeballs cho WebSocket Gemini Live.

    Trên một số mạng, IPv6 tới Google bị đen (gói tin không đi) trong khi IPv4 vẫn thông.
    Thư viện websockets thử địa chỉ đầu tiên và hết giờ trước khi tới IPv4.
    """
    import google.genai.live as live_module

    if getattr(live_module, "_happy_eyeballs_patched", False):
        return
    original = live_module.ws_connect

    def ws_connect(uri, **kwargs):
        if _loop_supports_happy_eyeballs():
            kwargs.setdefault("happy_eyeballs_delay", 0.25)
        return original(uri, **kwargs)

    live_module.ws_connect = ws_connect
    live_module._happy_eyeballs_patched = True


def get_client() -> genai.Client:
    global _client
    prefer_reachable_ip()
    if _client is None:
        if not settings.gemini_api_key:
            raise LLMNotConfigured(
                "Chưa có GEMINI_API_KEY. Hãy tạo file .env từ .env.example và điền key lấy từ Google AI Studio."
            )
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _build_config(
    system: str, thinking_level: str | None, schema: type[BaseModel] | None
) -> types.GenerateContentConfig:
    # Không dùng gọi hàm tự động (AFC); tắt để SDK không làm thêm bước thừa cho mỗi lượt.
    kwargs: dict = {
        "system_instruction": system,
        "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
    }
    if thinking_level:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
    if schema is not None:
        kwargs["response_mime_type"] = "application/json"
        kwargs["response_json_schema"] = schema.model_json_schema()
    return types.GenerateContentConfig(**kwargs)


def _model_chain(model: str) -> list[str]:
    chain = [model]
    for candidate in _FALLBACK_MODELS:
        if candidate not in chain:
            chain.append(candidate)
    now = time.monotonic()
    ready = [m for m in chain if _cooldown_until.get(m, 0) <= now]
    cooling = [m for m in chain if m not in ready]
    # Model đang nghỉ vẫn được thử sau cùng, phòng khi mọi model khác cũng lỗi.
    return ready + cooling


def _cool_down(model: str) -> None:
    _cooldown_until[model] = time.monotonic() + COOLDOWN_S


async def _generate(
    model: str, prompt: str, config: types.GenerateContentConfig, timeout_s: float | None = None
) -> str:
    last_error: LLMError | None = None
    chain = _model_chain(model)
    timeout_s = timeout_s or settings.llm_timeout_s
    for index, current in enumerate(chain):
        delay = 1.0
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await asyncio.wait_for(
                    get_client().aio.models.generate_content(model=current, contents=prompt, config=config),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError as exc:
                # Model đang chậm bất thường: chuyển ngay sang model dự phòng thay vì để người dùng chờ tiếp.
                last_error = LLMError(f"Gemini {current} không phản hồi sau {timeout_s:.0f} giây")
                _cool_down(current)
                if index < len(chain) - 1:
                    log.warning("%s, chuyển sang %s", last_error, chain[index + 1])
                    break
                raise last_error from exc
            except errors.APIError as exc:
                # Một số model (ví dụ dòng 2.x) không nhận thinking_level; thử lại không kèm cấu hình thinking.
                if exc.code == 400 and config.thinking_config and "think" in str(exc.message).lower():
                    config = config.model_copy(update={"thinking_config": None})
                    continue
                last_error = LLMError(f"Gemini API lỗi {exc.code}: {exc.message}")
                overloaded = exc.code in _IMMEDIATE_FAILOVER
                if overloaded:
                    _cool_down(current)
                if exc.code in _RETRYABLE_CODES and not overloaded and attempt < _MAX_ATTEMPTS:
                    log.warning("Gemini %s trả lỗi %s, thử lại sau %.0fs", current, exc.code, delay)
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                if exc.code in _RETRYABLE_CODES and index < len(chain) - 1:
                    log.warning(
                        "Gemini %s lỗi %s, chuyển sang %s", current, exc.code, chain[index + 1]
                    )
                    break
                raise last_error from exc
            if not response.text:
                last_error = LLMError(f"Model {current} trả về nội dung rỗng")
                if index < len(chain) - 1:
                    log.warning("%s, chuyển sang %s", last_error, chain[index + 1])
                    break
                raise last_error
            if current != model:
                log.info("Dùng model dự phòng %s thay cho %s", current, model)
            return response.text
    raise last_error or LLMError(f"Gemini {model} không phản hồi")


async def generate_json(
    model: str,
    system: str,
    prompt: str,
    schema: type[T],
    thinking_level: str | None = "low",
    timeout_s: float | None = None,
) -> T:
    text = await _generate(model, prompt, _build_config(system, thinking_level, schema), timeout_s)
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise LLMError(f"Model {model} trả về JSON không đúng schema {schema.__name__}: {exc}") from exc


async def generate_text(
    model: str, system: str, prompt: str, thinking_level: str | None = "minimal"
) -> str:
    text = await _generate(model, prompt, _build_config(system, thinking_level, None))
    return text.strip()
