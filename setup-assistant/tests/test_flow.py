"""Интеграционный тест сценария без сети: dp.feed_update + фейковая сессия.

Проверяет весь путь покупателя: /start, выбор ОС, шаги, траблшутинг,
«Позвать человека», финал с апселлом, рестарт и анти-флуд.

Запуск: .venv/bin/python -m pytest tests/test_flow.py
Требует установленный aiogram (см. requirements.txt).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

aiogram = pytest.importorskip("aiogram")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot as botmod  # noqa: E402
import steps  # noqa: E402
from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery,
    Chat,
    Message,
    Update,
    User,
)

NOW = datetime.now(timezone.utc)


class FakeSession:
    """Собирает вызовы Telegram API и отвечает правдоподобными объектами.

    Для aiogram сессия обязана быть вызываемой: Bot обращается к ней как
    session(method, timeout=...).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._next_id = 100

    async def __call__(self, bot, method, timeout=None):  # type: ignore[no-untyped-def]
        name = type(method).__name__
        text = getattr(method, "text", "") or ""
        self.calls.append((name, text))
        if name == "GetMe":
            return User(id=42, is_bot=True, first_name="assistant", username="assistant_bot")
        if name in {"SetMyCommands", "AnswerCallbackQuery"}:
            return True
        return Message(
            message_id=self._next_id,
            date=NOW,
            chat=getattr(method, "chat", None) or Chat(id=1, type="private"),
        )

    @property
    def api_texts(self) -> list[str]:
        return [text for _, text in self.calls]

    def last(self, method_name: str) -> tuple[str, str]:
        for name, text in reversed(self.calls):
            if name == method_name:
                return name, text
        raise AssertionError(f"не было вызова {method_name}")


def build_bot(limit: int | None = None) -> tuple[Bot, Dispatcher, FakeSession]:
    """Собирает диспетчер с фейковой сессией.

    `limit` переопределяет порог анти-флуда: сценарные тесты шлют апдейты
    быстрее человека, поэтому им нужен запас; тест троттлинга использует
    боевой порог (None).
    """
    session = FakeSession()
    fake_token = f"{10 ** 9}:{'AA' + 'y' * 34}"
    bot = Bot(token=fake_token, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(botmod.create_router())
    middleware = (
        botmod.ThrottlingMiddleware(limit=limit)
        if limit is not None
        else botmod.ThrottlingMiddleware()
    )
    dp.update.outer_middleware(middleware)
    return bot, dp, session


def text_update(update_id: int, text: str, user_id: int = 1) -> Update:
    user = User(id=user_id, is_bot=False, first_name="Покупатель")
    chat = Chat(id=user_id, type="private")
    message = Message(message_id=update_id, date=NOW, chat=chat, from_user=user, text=text)
    return Update(update_id=update_id, message=message)


def cb_update(update_id: int, data: str, message_id: int, user_id: int = 1) -> Update:
    user = User(id=user_id, is_bot=False, first_name="Покупатель")
    chat = Chat(id=user_id, type="private")
    message = Message(message_id=message_id, date=NOW, chat=chat, from_user=user, text="screen")
    cb = CallbackQuery(id=str(update_id), from_user=user, chat_instance="ci", data=data, message=message)
    return Update(update_id=update_id, callback_query=cb)


async def press(dp: Dispatcher, bot: Bot, seq: list[str], start_id: int = 500) -> None:
    for i, data in enumerate(seq):
        await dp.feed_update(bot, cb_update(start_id + i, data, message_id=77))


@pytest.mark.asyncio
async def test_full_happy_path() -> None:
    bot, dp, session = build_bot(limit=1000)
    await dp.feed_update(bot, text_update(1, "/start"))
    assert steps.GREETING[:40] in session.last("SendMessage")[1]

    await press(dp, bot, ["os:windows"])
    assert "Шаг 1 из 8" in session.last("EditMessageText")[1]

    # траблшутинг на первом шаге
    await press(dp, bot, ["step:fail"])
    assert "Не получилось" in session.last("EditMessageText")[1]
    await press(dp, bot, ["tr:1"])
    assert session.last("EditMessageText")[1].startswith("<b>1. ")
    await press(dp, bot, ["tr:list"])
    assert "Не получилось" in session.last("EditMessageText")[1]
    await press(dp, bot, ["tr:1", "tr:fixed"])
    assert "Шаг 2 из 8" in session.last("EditMessageText")[1]

    # «Позвать человека» и возврат в поток
    await press(dp, bot, ["human"])
    assert "Звать человека" in session.last("EditMessageText")[1]
    await press(dp, bot, ["human:back"])
    assert "Шаг 2 из 8" in session.last("EditMessageText")[1]

    # возврат из траблшутинга к тексту шага
    await press(dp, bot, ["step:fail", "tr:tostep"])
    assert "Шаг 2 из 8" in session.last("EditMessageText")[1]

    # пропуск и проход до финала
    await press(dp, bot, ["step:skip"])
    assert "Шаг 3 из 8" in session.last("EditMessageText")[1]
    for _ in range(5):
        await press(dp, bot, ["step:ok"])
    assert "Шаг 8 из 8" in session.last("EditMessageText")[1]
    await press(dp, bot, ["step:ok"])
    final = session.last("EditMessageText")[1]
    assert "Поздравляем" in final
    assert "Монитор позиций" in final
    assert "ИИ-автоответчик" in final

    # рестарт
    await press(dp, bot, ["flow:restart"])
    assert steps.GREETING[:40] in session.last("EditMessageText")[1]


@pytest.mark.asyncio
async def test_docker_branch_and_stale_buttons() -> None:
    bot, dp, session = build_bot(limit=1000)
    await dp.feed_update(bot, text_update(1, "/start"))
    await press(dp, bot, ["os:docker"])
    assert "Шаг 1 из 8" in session.last("EditMessageText")[1]

    # несуществующий номер причины - бот не падает и остаётся в ветке
    await press(dp, bot, ["step:fail", "tr:99"])
    await press(dp, bot, ["tr:1"])
    assert session.last("EditMessageText")[1].startswith("<b>1. ")

    # после рестарта состояния старые кнопки ведут к выбору ОС
    await press(dp, bot, ["flow:restart", "step:ok"])
    assert steps.GREETING[:40] in session.last("EditMessageText")[1]


@pytest.mark.asyncio
async def test_throttle_drops_extra_updates() -> None:
    bot, dp, session = build_bot()
    for i in range(8):
        await dp.feed_update(bot, text_update(10 + i, "/start"))
    sent = [1 for name, _ in session.calls if name == "SendMessage"]
    assert len(sent) == botmod.THROTTLE_LIMIT, "анти-флуд должен пропустить ровно лимит"


@pytest.mark.asyncio
async def test_free_text_gets_hint() -> None:
    bot, dp, session = build_bot(limit=1000)
    await dp.feed_update(bot, text_update(1, "помогите установить"))
    assert steps.HINT_CHOOSE_OS in session.last("SendMessage")[1]
    await dp.feed_update(bot, text_update(2, "/start"))
    await press(dp, bot, ["os:windows"])
    await dp.feed_update(bot, text_update(3, "что дальше?"))
    assert steps.HINT_BUTTONS in session.last("SendMessage")[1]
