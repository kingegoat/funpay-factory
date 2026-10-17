# -*- coding: utf-8 -*-
"""
Тесты плагина «ИИ-автоответчик»: fallback-логика, кулдауны, защита от цикла.

Запуск:  pytest tests/test_fallback.py -v
         (или просто: python3 tests/test_fallback.py)

Тесты не требуют установленного Cardinal: модуль плагина импортируется
напрямую из файла, а объекты cardinal/event подменяются фейками.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import uuid as uuid_module
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import pytest
except ImportError:  # автономный запуск: python3 tests/test_fallback.py
    pytest = None  # type: ignore[assignment]

PRODUCT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PRODUCT_DIR)

import ai_responder  # noqa: E402


# ------------------------------------ Фейковые объекты ------------------------------------


class FakeAccount:
    def __init__(self, account_id: int = 100500) -> None:
        self.id = account_id


class FakeCardinal:
    """Фейковый Cardinal: запоминает отправленные сообщения."""

    def __init__(self, account_id: int = 100500) -> None:
        self.account = FakeAccount(account_id)
        self.telegram = None
        self.sent: list[tuple] = []
        self._lock = threading.Lock()

    def send_message(self, chat_id, text, chat_name=None, *args, **kwargs):
        with self._lock:
            self.sent.append((chat_id, text, chat_name))


class FakeMessage:
    def __init__(
        self,
        text: str = "Здравствуйте! Сколько стоит?",
        chat_id: int = 2001,
        author_id: int = 777,
        by_bot: bool = False,
        by_vertex: bool = False,
        badge=None,
        is_support: bool = False,
        message_type=None,
    ) -> None:
        self.text = text
        self.chat_id = chat_id
        self.chat_name = f"chat-{chat_id}"
        self.author_id = author_id
        self.by_bot = by_bot
        self.by_vertex = by_vertex
        self.badge = badge
        self.is_support = is_support
        self.is_moderation = False
        self.is_employee = False
        self.type = message_type


class FakeEvent:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message


# ------------------------------------ Локальные LLM-серверы ------------------------------------


def _make_server(handler_class) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class _OkHandler(BaseHTTPRequestHandler):
    """Всегда валидный OpenAI-совместимый ответ; запоминает тело запроса."""

    last_body: dict = {}

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            _OkHandler.last_body = json.loads(raw.decode("utf-8"))
        except ValueError:  # pragma: no cover - мусор в тесте, а не в коде
            _OkHandler.last_body = {}
        body = json.dumps({
            "id": "chatcmpl-test",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Здравствуйте! Аккаунт в наличии, 300 руб."},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class _Error500Handler(BaseHTTPRequestHandler):
    """Сервер, всегда отдающий HTTP 500."""

    def do_POST(self) -> None:  # noqa: N802
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


class _EmptyChoicesHandler(BaseHTTPRequestHandler):
    """HTTP 200, но choices пустой - повреждённый ответ."""

    def do_POST(self) -> None:  # noqa: N802
        body = json.dumps({"id": "chatcmpl-broken", "choices": []}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


# ------------------------------------ Фикстуры ------------------------------------


def _build_module(workdir: str):
    """Перезагружает модуль плагина с временными путями и синхронным spawn."""
    spec = importlib.util.spec_from_file_location("ai_responder", os.path.join(PRODUCT_DIR, "ai_responder.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._CONFIG_PATH = os.path.join(workdir, "config.json")
    module._PERSONA_PATH = os.path.join(workdir, "persona.txt")
    module._CONFIG_DIR = workdir
    # Тесты детерминированы: воркеры выполняются синхронно, ожидание кулдаунов отключено.
    module._spawn = lambda target: target()  # noqa: E731
    module._interruptible_sleep = lambda seconds: None  # noqa: E731
    module._STATE_STOP.clear()
    module._COOLDOWNS.clear()
    with module._PENDING_LOCK:
        module._PENDING.clear()
        module._PENDING_NAMES.clear()
        module._INFLIGHT.clear()
    return module


if pytest is not None:

    @pytest.fixture()
    def plugin(tmp_path, monkeypatch):
        """Модуль плагина с временными конфигом/персоной."""
        return _build_module(str(tmp_path))


def _configured(plugin_module, base_url: str, api_key: str = "test-key", retries: int = 0) -> dict:
    cfg = plugin_module._get_config()
    cfg["enabled"] = True
    cfg["retries"] = retries
    cfg["cooldown"] = 60
    cfg["llm"]["base_url"] = base_url
    cfg["llm"]["api_key"] = api_key
    return cfg


# ------------------------------------ Тесты ------------------------------------


def test_module_attributes(plugin) -> None:
    """Все обязательные атрибуты плагина на месте, UUID - валидный UUID4."""
    for field in ("NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"):
        assert hasattr(plugin, field), f"нет атрибута {field}"
    parsed = uuid_module.UUID(plugin.UUID)
    assert parsed.version == 4, "UUID должен быть версии 4"
    assert isinstance(plugin.BIND_TO_NEW_MESSAGE, list) and plugin.BIND_TO_NEW_MESSAGE
    assert isinstance(plugin.BIND_TO_PRE_INIT, list) and plugin.BIND_TO_PRE_INIT


def test_default_config_is_safe(plugin) -> None:
    """Дефолтный конфиг: выключен, ключ пустой, кулдаун 60с."""
    cfg = plugin._get_config()
    assert cfg["enabled"] is False
    assert cfg["llm"]["api_key"] == ""
    assert cfg["cooldown"] == 60
    assert not plugin._is_configured(cfg), "дефолтный конфиг не должен считаться настроенным"


def test_build_prompt_contains_blocks(plugin) -> None:
    """Промпт содержит персону, прайс, FAQ и вопрос покупателя."""
    cfg = plugin._get_config()
    cfg["price"] = "Аккаунт X - 300 руб."
    cfg["faq"] = "В: Есть гарантия?\nО: 30 дней."
    prompt = plugin._build_system_prompt(cfg)
    assert "консультант" in prompt.lower()
    assert "300 руб." in prompt
    assert "30 дней" in prompt


def test_llm_success(plugin) -> None:
    """Успешный запрос: возвращается текст ответа модели."""
    server = _make_server(_OkHandler)
    try:
        _configured(plugin, f"http://127.0.0.1:{server.server_port}/v1")
        answer = plugin._llm_request(plugin._get_config(), "system", "вопрос")
        assert answer == "Здравствуйте! Аккаунт в наличии, 300 руб."
    finally:
        server.shutdown()


def test_new_message_sends_llm_answer(plugin) -> None:
    """Полный путь: сообщение покупателя -> ответ LLM -> send_message."""
    server = _make_server(_OkHandler)
    try:
        _configured(plugin, f"http://127.0.0.1:{server.server_port}/v1")
        cardinal = FakeCardinal()
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(text="Сколько стоит?")))
        assert len(cardinal.sent) == 1
        chat_id, text, chat_name = cardinal.sent[0]
        assert chat_id == 2001
        assert text == "Здравствуйте! Аккаунт в наличии, 300 руб."
        assert chat_name == "chat-2001"
    finally:
        server.shutdown()


def test_fallback_on_http_error(plugin) -> None:
    """Ошибка API (HTTP 500) -> покупателю уходит статичный fallback-текст."""
    server = _make_server(_Error500Handler)
    try:
        _configured(plugin, f"http://127.0.0.1:{server.server_port}/v1", retries=1)
        answer = plugin._llm_request(plugin._get_config(), "system", "вопрос")
        assert answer is None, "при HTTP 500 должен возвращаться None"

        cardinal = FakeCardinal()
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage()))
        assert len(cardinal.sent) == 1
        _, text, _ = cardinal.sent[0]
        assert text == plugin._get_config()["fallback_text"]
        assert "продавец" in text.lower()
    finally:
        server.shutdown()


def test_fallback_on_empty_choices(plugin) -> None:
    """HTTP 200 с пустым choices (битый шлюз) -> fallback."""
    server = _make_server(_EmptyChoicesHandler)
    try:
        _configured(plugin, f"http://127.0.0.1:{server.server_port}/v1", retries=0)
        assert plugin._llm_request(plugin._get_config(), "system", "вопрос") is None

        cardinal = FakeCardinal()
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage()))
        assert len(cardinal.sent) == 1
        assert cardinal.sent[0][1] == plugin._get_config()["fallback_text"]
    finally:
        server.shutdown()


def test_fallback_on_connection_error(plugin) -> None:
    """Недоступный endpoint (нет сервера) -> fallback, не исключение."""
    _configured(plugin, "http://127.0.0.1:1/v1", retries=0)
    assert plugin._llm_request(plugin._get_config(), "system", "вопрос") is None

    cardinal = FakeCardinal()
    plugin.new_message_handler(cardinal, FakeEvent(FakeMessage()))
    assert len(cardinal.sent) == 1
    assert cardinal.sent[0][1] == plugin._get_config()["fallback_text"]


def test_second_message_in_cooldown_not_dropped(plugin) -> None:
    """Второе сообщение в кулдауне не выбрасывается: на него тоже приходит ответ."""
    server = _make_server(_OkHandler)
    try:
        _configured(plugin, f"http://127.0.0.1:{server.server_port}/v1")
        cardinal = FakeCardinal()
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(text="Вопрос 1")))
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(text="Вопрос 2")))
        assert len(cardinal.sent) == 2, "второе сообщение не должно теряться"
        assert "300 руб." in cardinal.sent[0][1]
    finally:
        server.shutdown()


def test_messages_accumulated_inflight_answered_as_one(plugin) -> None:
    """Пока воркер готовит ответ, новые сообщения копятся и уходят в ОДИН ответ."""
    server = _make_server(_OkHandler)
    try:
        _configured(plugin, f"http://127.0.0.1:{server.server_port}/v1")
        plugin._spawn = lambda target: None  # воркер не запускаем - копим вручную
        cardinal = FakeCardinal()
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(text="Вопрос 1")))
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(text="Вопрос 2")))
        assert cardinal.sent == []
        assert plugin._PENDING[2001] == ["Вопрос 1", "Вопрос 2"]

        plugin._chat_task(cardinal, 2001)  # запускаем воркер вручную
        assert len(cardinal.sent) == 1, "накопленное уходит одним ответом"
        user_msg = _OkHandler.last_body["messages"][1]["content"]
        assert "Вопрос 1" in user_msg and "Вопрос 2" in user_msg
    finally:
        server.shutdown()


def test_different_chats_not_blocked_by_cooldown(plugin) -> None:
    """Кулдаун индивидуален для каждого чата (глобальный кулдаун отключаем)."""
    server = _make_server(_OkHandler)
    try:
        cfg = _configured(plugin, f"http://127.0.0.1:{server.server_port}/v1")
        cfg["global_cooldown"] = 0
        cardinal = FakeCardinal()
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(chat_id=1)))
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(chat_id=2)))
        assert len(cardinal.sent) == 2
    finally:
        server.shutdown()


def test_global_cooldown_defers_instead_of_dropping(plugin) -> None:
    """Всплеск из разных чатов выдерживает паузу между LLM-запросами, не теряя чатов."""
    server = _make_server(_OkHandler)
    try:
        cfg = _configured(plugin, f"http://127.0.0.1:{server.server_port}/v1")
        cfg["global_cooldown"] = 3600
        sleeps: list[float] = []
        plugin._interruptible_sleep = lambda seconds: sleeps.append(seconds)
        cardinal = FakeCardinal()
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(chat_id=1)))
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(chat_id=2)))
        assert len(cardinal.sent) == 2, "глобальный кулдаун не выбрасывает чаты"
        assert max(sleeps) >= 3599, "между запросами должна выдерживаться пауза"
    finally:
        server.shutdown()


def test_send_failure_keeps_texts_and_skips_cooldown(plugin) -> None:
    """Ошибка отправки не замораживает чат: тексты в очереди, кулдауна нет."""
    server = _make_server(_OkHandler)
    try:
        _configured(plugin, f"http://127.0.0.1:{server.server_port}/v1")

        class FlakyCardinal(FakeCardinal):
            fail_first = True

            def send_message(self, chat_id, text, chat_name=None, *args, **kwargs):
                if FlakyCardinal.fail_first:
                    FlakyCardinal.fail_first = False
                    raise RuntimeError("chat unavailable")
                super().send_message(chat_id, text, chat_name)

        cardinal = FlakyCardinal()
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(text="Вопрос 1")))
        assert cardinal.sent == [], "отправка упала - ответа нет"
        assert plugin._PENDING[2001] == ["Вопрос 1"], "текст вернулся в очередь"
        assert plugin._COOLDOWNS.remaining(2001, 60) == 0, "кулдаун не ставится"

        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(text="Вопрос 2")))
        assert len(cardinal.sent) == 1, "следующее сообщение повторяет попытку"
        assert cardinal.sent[0][0] == 2001
    finally:
        server.shutdown()


def test_owner_message_ignored(plugin) -> None:
    """Сообщение владельца аккаунта не вызывает ответ (защита от цикла)."""
    server = _make_server(_OkHandler)
    try:
        _configured(plugin, f"http://127.0.0.1:{server.server_port}/v1")
        cardinal = FakeCardinal(account_id=100500)
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(author_id=100500)))
        assert cardinal.sent == []
    finally:
        server.shutdown()


def test_own_bot_and_vertex_messages_ignored(plugin) -> None:
    """by_bot / by_vertex / бэйдж / поддержка - не обрабатываются."""
    server = _make_server(_OkHandler)
    try:
        _configured(plugin, f"http://127.0.0.1:{server.server_port}/v1")
        cardinal = FakeCardinal()
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(by_bot=True)))
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(by_vertex=True)))
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(badge="Автовыдача")))
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage(is_support=True)))
        assert cardinal.sent == []
    finally:
        server.shutdown()


def test_disabled_plugin_ignores_messages(plugin) -> None:
    """Выключенный автоответчик молчит."""
    server = _make_server(_OkHandler)
    try:
        _configured(plugin, f"http://127.0.0.1:{server.server_port}/v1")
        plugin._get_config()["enabled"] = False
        cardinal = FakeCardinal()
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage()))
        assert cardinal.sent == []
    finally:
        server.shutdown()


def test_unconfigured_plugin_silent(plugin) -> None:
    """Пустой api_key + дефолтный base_url -> тишина (а не fallback-спам)."""
    server = _make_server(_OkHandler)
    try:
        cfg = plugin._get_config()
        cfg["enabled"] = True
        cfg["llm"]["api_key"] = ""
        cardinal = FakeCardinal()
        plugin.new_message_handler(cardinal, FakeEvent(FakeMessage()))
        assert cardinal.sent == []
    finally:
        server.shutdown()


def test_config_roundtrip(plugin) -> None:
    """Сохранение конфига переживает перечитывание с диска."""
    cfg = plugin._get_config()
    cfg["fallback_text"] = "Свой запасной текст"
    cfg["price"] = "Товар А - 100 руб."
    plugin._save_config(cfg)

    plugin._CONFIG_CACHE = None  # сброс кэша: следующее чтение с диска
    reloaded = plugin._get_config()
    assert reloaded["fallback_text"] == "Свой запасной текст"
    assert reloaded["price"] == "Товар А - 100 руб."
    # merge с дефолтами не теряет новые поля и не затирает сохранённые
    assert reloaded["cooldown"] == 60


def test_persona_file_extracted(plugin) -> None:
    """Персона автоматически создаётся из встроенного дефолта."""
    plugin._ensure_persona_file()
    assert os.path.isfile(plugin._PERSONA_PATH)
    with open(plugin._PERSONA_PATH, "r", encoding="utf-8") as fh:
        content = fh.read()
    assert content.strip() == plugin.DEFAULT_PERSONA.strip()


if __name__ == "__main__":
    # Автономный запуск без pytest: python3 tests/test_fallback.py
    import inspect

    failures = 0
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and inspect.isfunction(fn)]
    for name, fn in tests:
        workdir = tempfile.mkdtemp(prefix="ar_test_")
        module = _build_module(workdir)
        try:
            fn(module)
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
