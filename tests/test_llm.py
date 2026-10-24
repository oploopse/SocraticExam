import asyncio

import pytest
from google.genai import errors

import google.genai.live as live_module

from app.llm import _generate, prefer_reachable_ip


class _Response:
    def __init__(self, text: str):
        self.text = text


class _Models:
    def __init__(self, results: dict):
        self.results = results
        self.calls: list[str] = []

    async def generate_content(self, model, contents, config):
        self.calls.append(model)
        result = self.results[model]
        if isinstance(result, Exception):
            raise result
        return result


class _Client:
    def __init__(self, models):
        self.aio = self
        self.models = models


def _unavailable(model: str) -> errors.APIError:
    return errors.APIError(503, {"error": {"message": f"{model} high demand", "status": "UNAVAILABLE"}})


def test_generate_falls_back_when_model_is_overloaded(monkeypatch):
    models = _Models(
        {
            "gemini-3.8-flash": _unavailable("gemini-3.8-flash"),
            "gemini-3.7-flash": _Response('{"ok": true}'),
        }
    )
    monkeypatch.setattr("app.llm.get_client", lambda: _Client(models))
    monkeypatch.setattr("app.llm._cooldown_until", {})

    text = asyncio.run(_generate("gemini-3.8-flash", "prompt", config=_config()))

    assert text == '{"ok": true}'
    assert models.calls == ["gemini-3.8-flash", "gemini-3.7-flash"]

    # Model vừa quá tải được bỏ qua ở lượt sau, không phải chờ thêm một lỗi 503 nữa.
    models.calls.clear()
    asyncio.run(_generate("gemini-3.8-flash", "prompt", config=_config()))
    assert models.calls == ["gemini-3.7-flash"]


def test_generate_moves_on_when_model_is_too_slow(monkeypatch):
    class SlowModels(_Models):
        async def generate_content(self, model, contents, config):
            self.calls.append(model)
            if model == "gemini-3.8-flash":
                await asyncio.sleep(5)
            return _Response("ok")

    models = SlowModels({})
    monkeypatch.setattr("app.llm.get_client", lambda: _Client(models))
    monkeypatch.setattr("app.llm._cooldown_until", {})

    text = asyncio.run(_generate("gemini-3.8-flash", "prompt", config=_config(), timeout_s=0.05))

    assert text == "ok"
    assert models.calls == ["gemini-3.8-flash", "gemini-3.7-flash"]


def test_live_websocket_tries_ipv4_in_parallel(monkeypatch):
    captured = {}

    def fake_connect(uri, **kwargs):
        captured["uri"] = uri
        captured.update(kwargs)
        return "session"

    monkeypatch.setattr(live_module, "ws_connect", fake_connect)
    monkeypatch.setattr(live_module, "_happy_eyeballs_patched", False, raising=False)

    async def connect():
        return live_module.ws_connect("wss://generativelanguage.googleapis.com/ws")

    prefer_reachable_ip()
    assert asyncio.run(connect()) == "session"
    assert captured["happy_eyeballs_delay"] == 0.25


def test_live_websocket_skips_happy_eyeballs_on_uvloop(monkeypatch):
    """uvloop.Loop không kế thừa asyncio.BaseEventLoop và không nhận happy_eyeballs_delay."""
    captured = {}

    def fake_connect(uri, **kwargs):
        captured.update(kwargs)
        return "session"

    class UvloopLikeLoop(asyncio.AbstractEventLoop):
        pass

    monkeypatch.setattr(live_module, "ws_connect", fake_connect)
    monkeypatch.setattr(live_module, "_happy_eyeballs_patched", False, raising=False)
    monkeypatch.setattr("app.llm.asyncio.get_running_loop", lambda: UvloopLikeLoop())

    prefer_reachable_ip()
    assert live_module.ws_connect("wss://generativelanguage.googleapis.com/ws") == "session"
    assert "happy_eyeballs_delay" not in captured


def test_live_websocket_connects_under_real_uvloop(monkeypatch):
    """Tái hiện lỗi trên Render: uvloop.create_connection() báo TypeError với happy_eyeballs_delay."""
    uvloop = pytest.importorskip("uvloop")  # Không có trên Windows; CI chạy Linux giống Render.
    captured = {}

    def fake_connect(uri, **kwargs):
        captured.update(kwargs)
        return "session"

    async def run():
        session = live_module.ws_connect("wss://generativelanguage.googleapis.com/ws")
        # Gọi đúng hàm đã gây lỗi trên Render, với các tham số bản vá vừa truyền xuống.
        loop = asyncio.get_running_loop()
        server = await loop.create_server(asyncio.Protocol, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport, _ = await loop.create_connection(asyncio.Protocol, "127.0.0.1", port, **captured)
        transport.close()
        server.close()
        return session

    monkeypatch.setattr(live_module, "ws_connect", fake_connect)
    monkeypatch.setattr(live_module, "_happy_eyeballs_patched", False, raising=False)
    prefer_reachable_ip()

    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
        assert runner.run(run()) == "session"
    assert "happy_eyeballs_delay" not in captured


def _config():
    from google.genai import types

    return types.GenerateContentConfig()
