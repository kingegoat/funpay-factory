# -*- coding: utf-8 -*-
"""
ИИ-автоответчик для FunPay Cardinal.

Отвечает покупателям в чатах FunPay через любой OpenAI-совместимый API
(endpoint /chat/completions: OpenAI, OpenRouter, DeepSeek, локальный шлюз и т.д.).

Как работает:
    * BIND_TO_NEW_MESSAGE: если автор сообщения - не владелец аккаунта,
      чат не в кулдауне и автоответчик включён - собрать промпт
      (persona + FAQ/прайс из CONFIG + текст сообщения покупателя),
      запросить ответ у LLM и отправить его через cardinal.send_message().
    * Любая ошибка API - отправка статичного fallback-текста + логирование.
    * Команды Telegram: /ar_on, /ar_off, /ar_test.

Безопасность:
    * golden_key и любые данные аккаунта никуда не отправляются;
    * исходящих запросов ровно один тип - POST на LLM-endpoint из конфига;
    * api_key хранится только локально в файле конфига;
    * телеметрии нет, код открытый.

Конфиг: storage/plugins/ai_responder_config.json (создаётся автоматически).
Персона: storage/plugins/ai_responder_persona.txt (создаётся автоматически).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

import requests

if TYPE_CHECKING:  # типы для аннотаций, реального импорта в рантайме нет
    from cardinal import Cardinal

# Опционально: enum типов сообщений FunPay (для отсечения системных сообщений).
try:
    from FunPayAPI.common.enums import MessageTypes
except Exception:  # старые версии Cardinal / запуск вне Cardinal
    MessageTypes = None  # type: ignore[assignment]

# ============================== ОБЯЗАТЕЛЬНЫЕ АТРИБУТЫ ПЛАГИНА ==============================

NAME: str = "ИИ-автоответчик"
VERSION: str = "1.0.0"
DESCRIPTION: str = (
    "Автоответчик на сообщения покупателей через любой OpenAI-совместимый API "
    "(OpenAI, OpenRouter, DeepSeek, локальный шлюз). Настройка - в файле "
    "storage/plugins/ai_responder_config.json, команды: /ar_on, /ar_off, /ar_test. "
    "Ключи хранятся только локально, golden_key никуда не отправляется."
)
CREDITS: str = "funpay-factory"
SETTINGS_PAGE: bool = False
UUID: str = "f33a521b-9a2e-4728-a9f6-ad020ae5b0b0"
BIND_TO_DELETE: Optional[Callable[..., None]] = None

# ==================================== КОНСТАНТЫ ====================================

DEFAULT_LLM_BASE_URL: str = "https://api.openai.com/v1"

_CONFIG_DIR: str = os.path.join("storage", "plugins")
_CONFIG_PATH: str = os.path.join(_CONFIG_DIR, "ai_responder_config.json")
_PERSONA_PATH: str = os.path.join(_CONFIG_DIR, "ai_responder_persona.txt")

DEFAULT_PERSONA: str = """\
Ты - вежливый консультант продавца на бирже FunPay. Ты отвечаешь покупателям в чате магазина.

Правила ответов:
- Отвечай на русском языке.
- Отвечай коротко и по делу: 1-3 предложения, без воды.
- Будь вежлив и доброжелателен, обращайся на «вы».
- Цены, наличие и условия бери ТОЛЬКО из блоков «ПРАЙС-ЛИСТ» и «FAQ» ниже. Не выдумывай цены и сроки.
- Если ответа нет в прайсе и FAQ - вежливо скажи, что продавец скоро ответит лично, и предложите подождать.
- Не обещай того, чего нет в условиях. Не обсуждай темы, не связанные с покупкой.
- Отвечай простым текстом: без HTML-разметки, без markdown, без списков и эмодзи.
"""

DEFAULT_FALLBACK_TEXT: str = (
    "Здравствуйте! Продавец сейчас не в сети, но ответит вам в ближайшее время. "
    "Если вопрос срочный - напишите его здесь, ответ придёт в этот чат."
)

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "cooldown": 60,
    "request_timeout": 30,
    "retries": 1,
    "llm": {
        "base_url": DEFAULT_LLM_BASE_URL,
        "api_key": "",
        "model": "gpt-4o-mini",
        "temperature": 0.4,
        "max_tokens": 300,
    },
    "fallback_text": DEFAULT_FALLBACK_TEXT,
    "price": "",
    "faq": "",
}

_MAX_COOLDOWN_ENTRIES: int = 4096

logger = logging.getLogger("FPC.plugins.ai_responder")

# ================================ СОСТОЯНИЕ ПЛАГИНА ================================


class _CooldownMap:
    """Потокобезопасная карта кулдаунов «чат -> время последнего ответа»."""

    def __init__(self, max_entries: int = _MAX_COOLDOWN_ENTRIES) -> None:
        self._lock = threading.Lock()
        self._map: dict[Any, float] = {}
        self._max_entries = max_entries

    def check_and_set(self, key: Any, cooldown_seconds: float) -> bool:
        """Возвращает True, если ключ не в кулдауне, и фиксирует время проверки."""
        now = time.time()
        with self._lock:
            last = self._map.get(key)
            if last is not None and (now - last) < cooldown_seconds:
                return False
            self._map[key] = now
            if len(self._map) > self._max_entries:
                self._prune_locked()
            return True

    def clear(self) -> None:
        with self._lock:
            self._map.clear()

    def _prune_locked(self) -> None:
        """Оставляет самую свежую половину записей (вызывается под локом)."""
        newest = sorted(self._map.items(), key=lambda item: item[1], reverse=True)
        self._map = dict(newest[: self._max_entries // 2])


_STATE_STOP = threading.Event()
_COOLDOWNS = _CooldownMap()
_CONFIG_LOCK = threading.RLock()
_CONFIG_CACHE: Optional[dict[str, Any]] = None


def _spawn(target: Callable[[], None]) -> None:
    """Запускает фоновую задачу в daemon-потоке (не блокирует цикл Cardinal)."""
    thread = threading.Thread(target=target, daemon=True)
    thread.start()


def _as_float(value: Any, default: float) -> float:
    """float(value) с защитой: мусор в конфиге не роняет запрос, берём дефолт."""
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Некорректное число в конфиге (%r), использую %.1f", value, default)
        return default


def _as_int(value: Any, default: int) -> int:
    """int(value) с защитой: мусор в конфиге не роняет запрос, берём дефолт."""
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Некорректное число в конфиге (%r), использую %d", value, default)
        return default


# ==================================== КОНФИГ ====================================


def _merge_defaults(stored: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Дополняет сохранённый конфиг дефолтами (устойчивость к новым версиям)."""
    merged = dict(defaults)
    for key, value in stored.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_defaults(value, merged[key])
        else:
            merged[key] = value
    return merged


def _load_config() -> dict[str, Any]:
    """Читает конфиг с диска (или создаёт дефолтный файл) и кэширует его."""
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        stored: dict[str, Any] = {}
        existed = os.path.isfile(_CONFIG_PATH)
        if existed:
            try:
                with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    stored = loaded
            except (OSError, ValueError) as exc:
                logger.warning("Не удалось прочитать конфиг (%s): %s", _CONFIG_PATH, exc)
        _CONFIG_CACHE = _merge_defaults(stored, DEFAULT_CONFIG)
        if not existed:
            # Первым запуском создаём файл конфига, чтобы его можно было править.
            try:
                os.makedirs(_CONFIG_DIR, exist_ok=True)
                with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
                    json.dump(_CONFIG_CACHE, fh, ensure_ascii=False, indent=2)
                logger.info("Создан файл конфига: %s", _CONFIG_PATH)
            except OSError as exc:
                logger.error("Не удалось создать файл конфига (%s): %s", _CONFIG_PATH, exc)
        return _CONFIG_CACHE


def _get_config() -> dict[str, Any]:
    """Возвращает кэшированный конфиг (грузит при первом обращении)."""
    with _CONFIG_LOCK:
        if _CONFIG_CACHE is None:
            return _load_config()
        return _CONFIG_CACHE


def _save_config(cfg: dict[str, Any]) -> None:
    """Атомарно сохраняет конфиг на диск и обновляет кэш."""
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        _CONFIG_CACHE = _merge_defaults(cfg, DEFAULT_CONFIG)
        try:
            os.makedirs(_CONFIG_DIR, exist_ok=True)
            tmp_path = _CONFIG_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(_CONFIG_CACHE, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, _CONFIG_PATH)
        except OSError as exc:
            logger.error("Не удалось сохранить конфиг (%s): %s", _CONFIG_PATH, exc)


def _is_configured(cfg: dict[str, Any]) -> bool:
    """ True, если LLM-endpoint реально настроен (ключ указан или шлюз не дефолтный)."""
    llm = cfg.get("llm") or {}
    api_key = str(llm.get("api_key") or "").strip()
    base_url = str(llm.get("base_url") or "").strip()
    if not base_url:
        return False
    return bool(api_key) or base_url.rstrip("/") != DEFAULT_LLM_BASE_URL


# ==================================== ПЕРСОНА ====================================


def _ensure_persona_file() -> None:
    """Создаёт файл персоны с дефолтным текстом, если его ещё нет."""
    try:
        if not os.path.isfile(_PERSONA_PATH):
            os.makedirs(_CONFIG_DIR, exist_ok=True)
            with open(_PERSONA_PATH, "w", encoding="utf-8") as fh:
                fh.write(DEFAULT_PERSONA)
            logger.info("Создан файл персоны: %s", _PERSONA_PATH)
    except OSError as exc:
        logger.error("Не удалось создать файл персоны (%s): %s", _PERSONA_PATH, exc)


def _load_persona() -> str:
    """Читает текст персоны (при ошибке - встроенный дефолт)."""
    try:
        if os.path.isfile(_PERSONA_PATH):
            with open(_PERSONA_PATH, "r", encoding="utf-8") as fh:
                content = fh.read().strip()
            if content:
                return content
    except OSError as exc:
        logger.warning("Не удалось прочитать файл персоны (%s): %s", _PERSONA_PATH, exc)
    return DEFAULT_PERSONA.strip()


# ==================================== ПРОМПТ ====================================


def _build_system_prompt(cfg: dict[str, Any]) -> str:
    """Собирает системный промпт: персона + прайс + FAQ + правила."""
    price = str(cfg.get("price") or "").strip()
    faq = str(cfg.get("faq") or "").strip()
    parts: list[str] = [_load_persona()]
    parts.append(
        "ПРАЙС-ЛИСТ МАГАЗИНА (цены и условия бери только отсюда):\n"
        + (price if price else "(прайс не заполнен продавцом - конкретные цены не называй)")
    )
    parts.append(
        "ЧАСТЫЕ ВОПРОСЫ И ОТВЕТЫ (FAQ):\n"
        + (faq if faq else "(FAQ не заполнен продавцом)")
    )
    return "\n\n".join(parts)


# ==================================== LLM-ЗАПРОС ====================================


def _llm_request(cfg: dict[str, Any], system_prompt: str, user_text: str) -> Optional[str]:
    """
    Запрос к OpenAI-совместимому /chat/completions.

    :return: текст ответа модели или None при исчерпании попыток.
    """
    llm = cfg.get("llm") or {}
    base_url = str(llm.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        logger.warning("LLM base_url не задан - запрос пропущен")
        return None
    url = base_url + "/chat/completions"

    headers = {"Content-Type": "application/json"}
    api_key = str(llm.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    payload = {
        "model": str(llm.get("model") or "gpt-4o-mini"),
        "temperature": _as_float(llm.get("temperature", 0.4), 0.4),
        "max_tokens": _as_int(llm.get("max_tokens", 300), 300),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
    }

    timeout = _as_float(cfg.get("request_timeout", 30), 30.0)
    retries = max(0, _as_int(cfg.get("retries", 1), 1))
    attempts = 1 + retries

    for attempt in range(1, attempts + 1):
        if _STATE_STOP.is_set():
            return None
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("пустой choices в ответе LLM")
            content = (choices[0].get("message") or {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("пустой content в ответе LLM")
            return content.strip()
        except Exception as exc:  # requests.RequestException, ValueError, RuntimeError
            logger.warning(
                "LLM-запрос не удался (попытка %d/%d, url=%s): %s",
                attempt, attempts, url, exc,
            )
            if attempt < attempts and not _STATE_STOP.is_set():
                time.sleep(1.0)
    return None


# ==================================== ХУКИ ====================================


def new_message_handler(cardinal: Any, event: Any) -> None:
    """
    BIND_TO_NEW_MESSAGE: точка входа обработки сообщения покупателя.

    Быстрые проверки выполняются синхронно (микросекунды), сетевой запрос -
    в отдельном daemon-потоке, основной цикл Cardinal не блокируется.
    """
    message = getattr(event, "message", None)
    if message is None:
        return

    cfg = _get_config()
    if not bool(cfg.get("enabled", False)):
        return
    if not _is_configured(cfg):
        logger.debug("Автоответчик не настроен (пустой api_key/base_url) - сообщение пропущено")
        return

    # Защита от цикла: не отвечаем на свои сообщения, сообщения через
    # account.send_message (by_bot), FunPay Vertex и системные бэйджи.
    if getattr(message, "by_bot", False) or getattr(message, "by_vertex", False):
        return
    if getattr(message, "badge", None) is not None:
        return
    if getattr(message, "is_support", False) or getattr(message, "is_moderation", False) \
            or getattr(message, "is_employee", False):
        return

    account = getattr(cardinal, "account", None)
    account_id = getattr(account, "id", None)
    if account_id is not None and getattr(message, "author_id", None) == account_id:
        return

    # Только обычные сообщения покупателей (не системные уведомления FunPay).
    message_type = getattr(message, "type", None)
    if MessageTypes is not None and message_type is not None \
            and message_type is not MessageTypes.NON_SYSTEM:
        return

    buyer_text = (getattr(message, "text", None) or "").strip()
    if not buyer_text:
        return

    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        return

    try:
        cooldown = float(cfg.get("cooldown", 60))
    except (TypeError, ValueError):
        cooldown = 60.0
    if cooldown < 0:
        cooldown = 0.0

    if not _COOLDOWNS.check_and_set(chat_id, cooldown):
        logger.debug("Чат %s в кулдауне (%.0f c) - пропускаю", chat_id, cooldown)
        return

    chat_name = getattr(message, "chat_name", None)
    logger.info(
        "ИИ-автоответчик: сообщение от покупателя в чате %s (%s), запрашиваю ответ LLM",
        chat_name or chat_id, chat_id,
    )
    _spawn(lambda: _reply_worker(cardinal, chat_id, chat_name, buyer_text))


def _reply_worker(cardinal: Any, chat_id: Any, chat_name: Optional[str], buyer_text: str) -> None:
    """Фоновый поток: LLM-запрос и отправка ответа (или fallback)."""
    if _STATE_STOP.is_set():
        return
    cfg = _get_config()
    system_prompt = _build_system_prompt(cfg)

    answer: Optional[str] = None
    try:
        answer = _llm_request(cfg, system_prompt, buyer_text)
    except Exception as exc:  # на всякий случай: _llm_request не должен бросать
        logger.error("Неожиданная ошибка LLM-запроса: %s", exc)
    if answer is None:
        answer = str(cfg.get("fallback_text") or DEFAULT_FALLBACK_TEXT).strip()
        logger.warning("ИИ-автоответчик: LLM недоступен, отправлен fallback-текст в чат %s", chat_id)
    try:
        cardinal.send_message(chat_id, answer, chat_name)
        logger.info("ИИ-автоответчик: ответ отправлен в чат %s", chat_id)
    except Exception as exc:
        logger.error("ИИ-автоответчик: не удалось отправить сообщение в чат %s: %s", chat_id, exc)


def init_plugin(cardinal: Any, *args: Any) -> None:
    """BIND_TO_PRE_INIT: грузит конфиг, создаёт файлы, регистрирует TG-команды."""
    _STATE_STOP.clear()  # на случай повторной инициализации без рестарта процесса
    _load_config()
    _ensure_persona_file()
    _register_telegram_commands(cardinal)
    enabled = bool(_get_config().get("enabled", False))
    logger.info(
        "Плагин «%s» v%s загружен (автоответчик %s)",
        NAME, VERSION, "включён" if enabled else "выключен - /ar_on для включения",
    )


def stop_plugin(cardinal: Any, *args: Any) -> None:
    """BIND_TO_PRE_STOP: чистая остановка фоновых потоков."""
    _STATE_STOP.set()
    _COOLDOWNS.clear()
    logger.info("Плагин «%s» остановлен", NAME)


# ================================ TELEGRAM-КОМАНДЫ ================================


def _register_telegram_commands(cardinal: Any) -> None:
    """Регистрирует /ar_on, /ar_off, /ar_test, если Telegram-бот включён."""
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        logger.info("Telegram-бот отключён: команды /ar_on, /ar_off, /ar_test недоступны, "
                    "включайте автоответчик правкой конфига")
        return
    bot = tg.bot

    def _authorized(message: Any) -> bool:
        from_user = getattr(message, "from_user", None)
        authorized = getattr(tg, "authorized_users", None) or {}
        return from_user is not None and getattr(from_user, "id", None) in authorized

    def _cmd_ar_on(message: Any) -> None:
        if not _authorized(message):
            return
        cfg = _get_config()
        cfg["enabled"] = True
        _save_config(cfg)
        hint = "" if _is_configured(cfg) else \
            "\n\n⚠️ Не настроен LLM: впишите api_key в storage/plugins/ai_responder_config.json."
        try:
            bot.reply_to(message, "✅ ИИ-автоответчик включён." + hint)
        except Exception as exc:
            logger.error("Не удалось ответить на /ar_on: %s", exc)

    def _cmd_ar_off(message: Any) -> None:
        if not _authorized(message):
            return
        cfg = _get_config()
        cfg["enabled"] = False
        _save_config(cfg)
        try:
            bot.reply_to(message, "⛔ ИИ-автоответчик выключен.")
        except Exception as exc:
            logger.error("Не удалось ответить на /ar_off: %s", exc)

    def _cmd_ar_test(message: Any) -> None:
        if not _authorized(message):
            return
        parts = (getattr(message, "text", "") or "").split(maxsplit=1)
        test_text = parts[1].strip() if len(parts) > 1 else "Здравствуйте! Есть в наличии? Сколько стоит?"

        def _worker() -> None:
            cfg = _get_config()
            if not _is_configured(cfg):
                _reply(message, "⚠️ LLM не настроен: впишите api_key и model в "
                                "storage/plugins/ai_responder_config.json.")
                return
            started = time.time()
            answer = _llm_request(cfg, _build_system_prompt(cfg), test_text)
            elapsed = time.time() - started
            if answer is None:
                _reply(
                    message,
                    f"❌ LLM не ответил за {elapsed:.1f} c. Покупателю в этом случае "
                    f"уходит fallback-текст:\n\n{cfg.get('fallback_text')}",
                )
            else:
                _reply(message, f"🤖 Ответ модели ({elapsed:.1f} c):\n\n{answer}")

        def _reply(target: Any, text: str) -> None:
            try:
                bot.reply_to(target, text)
            except Exception as exc:
                logger.error("Не удалось ответить на /ar_test: %s", exc)

        _spawn(_worker)

    try:
        tg.msg_handler(_cmd_ar_on, commands=["ar_on"])
        tg.msg_handler(_cmd_ar_off, commands=["ar_off"])
        tg.msg_handler(_cmd_ar_test, commands=["ar_test"])
    except Exception as exc:
        logger.error("Не удалось зарегистрировать TG-команды автоответчика: %s", exc)
        return

    try:
        cardinal.add_telegram_commands(UUID, [
            ("ar_on", "Включить ИИ-автоответчик", True),
            ("ar_off", "Выключить ИИ-автоответчик", True),
            ("ar_test", "Тестовый диалог с ИИ-автоответчиком", True),
        ])
    except Exception as exc:
        logger.error("Не удалось добавить команды в меню плагина: %s", exc)


# ==================================== СВЯЗЫВАНИЕ ХУКОВ ====================================

BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_NEW_MESSAGE = [new_message_handler]
BIND_TO_PRE_STOP = [stop_plugin]
