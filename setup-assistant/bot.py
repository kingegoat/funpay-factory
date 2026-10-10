"""TG-бот «Настройка Cardinal с ассистентом».

Ведёт продавца FunPay по установке FunPayCardinal (ветки Windows и Docker)
пошагово: экраны шагов с кнопками [Получилось] [Не получилось] [Пропустить]
[Позвать человека], ветка траблшутинга по текущему шагу, финальный чеклист
с мягким апселлом плагинов.

Контент шагов лежит в steps.py - тексты правятся без знания программирования.

Запуск: токен берётся из переменной окружения BOT_TOKEN (.env).
Бот ничего не знает о golden_key и аккаунтах FunPay: все данные покупатель
вводит в консоли Cardinal на своей машине.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from aiogram.utils.token import TokenValidationError
from dotenv import load_dotenv

import steps

logger = logging.getLogger("setup_assistant")

BOT_TOKEN_ENV = "BOT_TOKEN"
SUPPORT_USERNAME_ENV = "SUPPORT_USERNAME"
LOG_LEVEL_ENV = "LOG_LEVEL"

# Анти-флуд: не более THROTTLE_LIMIT апдейтов от пользователя за THROTTLE_WINDOW секунд.
THROTTLE_LIMIT = 5
THROTTLE_WINDOW_SEC = 3.0


class Flow(StatesGroup):
    choosing_os = State()
    in_step = State()
    in_trouble = State()
    finished = State()


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def os_keyboard() -> InlineKeyboardMarkup:
    return _kb(
        [
            [InlineKeyboardButton(text="Windows", callback_data="os:windows")],
            [InlineKeyboardButton(text="Docker", callback_data="os:docker")],
        ]
    )


def step_keyboard() -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                InlineKeyboardButton(text="Получилось", callback_data="step:ok"),
                InlineKeyboardButton(text="Не получилось", callback_data="step:fail"),
            ],
            [
                InlineKeyboardButton(text="Пропустить", callback_data="step:skip"),
                InlineKeyboardButton(text="Позвать человека", callback_data="human"),
            ],
        ]
    )


def trouble_keyboard(step: steps.Step) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{i}. {cause.title}"[:60],
                callback_data=f"tr:{i}",
            )
        ]
        for i, cause in enumerate(step.causes, start=1)
    ]
    rows.append(
        [
            InlineKeyboardButton(text="Вернуться к шагу", callback_data="tr:tostep"),
            InlineKeyboardButton(text="Позвать человека", callback_data="human"),
        ]
    )
    return _kb(rows)


def cause_keyboard() -> InlineKeyboardMarkup:
    return _kb(
        [
            [InlineKeyboardButton(text="Решилось - дальше", callback_data="tr:fixed")],
            [InlineKeyboardButton(text="Другая причина", callback_data="tr:list")],
            [
                InlineKeyboardButton(text="Вернуться к шагу", callback_data="tr:tostep"),
                InlineKeyboardButton(text="Позвать человека", callback_data="human"),
            ],
        ]
    )


def human_keyboard() -> InlineKeyboardMarkup:
    return _kb(
        [[InlineKeyboardButton(text="Вернуться назад", callback_data="human:back")]]
    )


def final_keyboard() -> InlineKeyboardMarkup:
    return _kb(
        [
            [InlineKeyboardButton(text="Пройти заново", callback_data="flow:restart")],
            [InlineKeyboardButton(text="Позвать человека", callback_data="human")],
        ]
    )


# ---------------------------------------------------------------------------
# Рендер экранов
# ---------------------------------------------------------------------------


def _support_username() -> str:
    name = os.getenv(SUPPORT_USERNAME_ENV, "").strip().lstrip("@")
    # Значение из .env вставляется в HTML-экраны: пропускаем только ник-подобное,
    # чтобы опечатка с & или < не ломала рендер (и не тащила произвольный HTML).
    return name if re.fullmatch(r"[A-Za-z0-9_]{5,32}", name) else ""


def _render_greeting() -> str:
    return steps.GREETING


def _render_step(os_key: str, index: int) -> str:
    flow = steps.FLOWS[os_key]
    step = flow[index]
    lines = [
        f"<b>Шаг {index + 1} из {len(flow)} · {steps.OS_TITLES[os_key]}</b>",
        "",
        f"<b>{step.title}</b>",
        "",
        step.body,
    ]
    if step.tip:
        lines += ["", f"Подсказка: {step.tip}"]
    return "\n".join(lines)


def _render_trouble(os_key: str, index: int) -> str:
    step = steps.FLOWS[os_key][index]
    return (
        "<b>Не получилось? Разберёмся.</b>\n"
        f"Шаг: {step.title}\n\n"
        "Выберите свою ситуацию - покажу решение:"
    )


def _render_cause(os_key: str, index: int, cause_index: int) -> str:
    step = steps.FLOWS[os_key][index]
    cause = step.causes[cause_index]
    return (
        f"<b>{cause_index + 1}. {cause.title}</b>\n"
        "\n"
        f"{cause.fix}\n"
        "\n"
        "Помогло - жмите «Решилось - дальше»."
    )


def _render_final() -> str:
    return steps.final_text(_support_username())


async def _show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """Аккуратно заменяет экран: редактирует сообщение, а при сбое шлёт новое."""
    if cb.message is None:
        await cb.answer()
        return
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        if "message to edit not found" in str(exc):
            await cb.message.answer(text, reply_markup=kb)
            return
        raise


async def _read_flow(state: FSMContext) -> tuple[str | None, int]:
    data = await state.get_data()
    os_key = data.get("os")
    if os_key not in steps.FLOWS:
        return None, 0
    try:
        index = int(data.get("index", 0))
    except (TypeError, ValueError):
        index = 0
    if not 0 <= index < len(steps.FLOWS[os_key]):
        index = 0
    return os_key, index


async def _advance(cb: CallbackQuery, state: FSMContext) -> None:
    """Сдвигает пользователя на следующий шаг либо на финальный экран."""
    os_key, index = await _read_flow(state)
    if os_key is None:
        await state.set_state(Flow.choosing_os)
        await _show(cb, _render_greeting(), os_keyboard())
        return
    flow = steps.FLOWS[os_key]
    if index + 1 >= len(flow):
        await state.set_state(Flow.finished)
        await _show(cb, _render_final(), final_keyboard())
        return
    await state.set_state(Flow.in_step)
    await state.update_data(index=index + 1)
    await _show(cb, _render_step(os_key, index + 1), step_keyboard())


async def _show_trouble(cb: CallbackQuery, state: FSMContext) -> None:
    """Показывает список частых причин для текущего шага."""
    os_key, index = await _read_flow(state)
    if os_key is None:
        await state.set_state(Flow.choosing_os)
        await _show(cb, _render_greeting(), os_keyboard())
        return
    await state.set_state(Flow.in_trouble)
    await _show(cb, _render_trouble(os_key, index), trouble_keyboard(steps.FLOWS[os_key][index]))


async def _enter_flow(cb: CallbackQuery, state: FSMContext, os_key: str) -> None:
    await state.update_data(os=os_key, index=0)
    await state.set_state(Flow.in_step)
    await _show(cb, _render_step(os_key, 0), step_keyboard())


# ---------------------------------------------------------------------------
# Роутер с обработчиками
# ---------------------------------------------------------------------------


def create_router() -> Router:
    """Собирает роутер со всеми обработчиками.

    Фабрика, а не модульный роутер: один Router можно подключить только
    к одному Dispatcher (удобно для тестов, где диспетчер создаётся на каждый кейс).
    """
    router = Router()

    # Бот - личный ассистент покупателя: в группах и каналах не отвечаем,
    # иначе любой чат, куда его добавят, превращается в спам-усилитель.
    @router.message(CommandStart(), F.chat.type == "private")
    async def cmd_start(message: Message, state: FSMContext) -> None:
        logger.info(
            "Пользователь начал прохождение: user_id=%s",
            message.from_user and message.from_user.id,
        )
        await state.clear()
        await state.set_state(Flow.choosing_os)
        await message.answer(_render_greeting(), reply_markup=os_keyboard())

    @router.message(F.chat.type == "private")
    async def fallback_text(message: Message, state: FSMContext) -> None:
        current = await state.get_state()
        # Пользователь ещё не начинал (state=None) или выбирает ОС - зовём на кнопки выбора.
        text = steps.HINT_CHOOSE_OS if current in (None, Flow.choosing_os) else steps.HINT_BUTTONS
        await message.answer(text)
        logger.debug(
            "Текст вместо кнопки: user_id=%s, state=%s",
            message.from_user and message.from_user.id,
            current,
        )

    @router.callback_query(F.data == "os:windows")
    async def choose_windows(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        logger.info("Выбрана ветка windows: user_id=%s", cb.from_user.id)
        await _enter_flow(cb, state, "windows")

    @router.callback_query(F.data == "os:docker")
    async def choose_docker(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        logger.info("Выбрана ветка docker: user_id=%s", cb.from_user.id)
        await _enter_flow(cb, state, "docker")

    @router.callback_query(F.data == "step:ok")
    async def on_step_ok(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        await _advance(cb, state)

    @router.callback_query(F.data == "step:skip")
    async def on_step_skip(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer("Шаг пропущен")
        os_key, index = await _read_flow(state)
        logger.info("Пропущен шаг %s[%s]: user_id=%s", os_key, index, cb.from_user.id)
        await _advance(cb, state)

    @router.callback_query(F.data == "step:fail")
    async def on_step_fail(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        await _show_trouble(cb, state)

    # Траблшутинг: точные колбэки регистрируем ДО шаблонного "tr:<номер>".
    @router.callback_query(F.data == "tr:list")
    async def on_trouble_list(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        await _show_trouble(cb, state)

    @router.callback_query(F.data == "tr:fixed")
    async def on_trouble_fixed(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer("Отлично!")
        await _advance(cb, state)

    @router.callback_query(F.data == "tr:tostep")
    async def on_trouble_to_step(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        os_key, index = await _read_flow(state)
        if os_key is None:
            await state.set_state(Flow.choosing_os)
            await _show(cb, _render_greeting(), os_keyboard())
            return
        await state.set_state(Flow.in_step)
        await _show(cb, _render_step(os_key, index), step_keyboard())

    @router.callback_query(F.data.startswith("tr:"))
    async def on_trouble_cause(cb: CallbackQuery, state: FSMContext) -> None:
        raw = (cb.data or "").split(":", 1)[1]
        try:
            cause_index = int(raw) - 1
        except ValueError:
            await cb.answer()
            return
        os_key, index = await _read_flow(state)
        if os_key is None:
            await state.set_state(Flow.choosing_os)
            await _show(cb, _render_greeting(), os_keyboard())
            return
        causes = steps.FLOWS[os_key][index].causes
        if not 0 <= cause_index < len(causes):
            await cb.answer("Кнопка устарела, вернитесь к шагу")
            return
        await cb.answer()
        await _show(cb, _render_cause(os_key, index, cause_index), cause_keyboard())

    @router.callback_query(F.data == "human")
    async def on_human(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        logger.info("Запрос помощи: user_id=%s", cb.from_user.id)
        await _show(cb, steps.human_text(_support_username()), human_keyboard())

    @router.callback_query(F.data == "human:back")
    async def on_human_back(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        current = await state.get_state()
        os_key, index = await _read_flow(state)
        if os_key is None or current == Flow.choosing_os:
            await _show(cb, _render_greeting(), os_keyboard())
            return
        if current == Flow.in_trouble:
            await _show(
                cb, _render_trouble(os_key, index), trouble_keyboard(steps.FLOWS[os_key][index])
            )
            return
        if current == Flow.finished:
            await _show(cb, _render_final(), final_keyboard())
            return
        await _show(cb, _render_step(os_key, index), step_keyboard())

    @router.callback_query(F.data == "flow:restart")
    async def on_restart(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        await state.clear()
        await state.set_state(Flow.choosing_os)
        await _show(cb, _render_greeting(), os_keyboard())

    @router.errors()
    async def on_error(event: ErrorEvent) -> bool:
        """Ловит любые необработанные исключения: бот отвечает и продолжает жить."""
        logger.exception("Необработанное исключение", exc_info=event.exception)
        target: Message | None = None
        update = event.update
        if update is not None:
            if update.message is not None:
                target = update.message
            elif update.callback_query is not None and update.callback_query.message is not None:
                target = update.callback_query.message
        if target is not None:
            try:
                await target.answer(steps.ERROR_TEXT)
            except Exception:  # noqa: BLE001 - отправка не должна маскировать исходную ошибку
                logger.warning(
                    "Не удалось отправить сообщение об ошибке пользователю", exc_info=True
                )
        return True

    @router.startup()
    async def on_startup(bot: Bot) -> None:
        await bot.set_my_commands(
            [BotCommand(command="start", description="Начать настройку Cardinal")]
        )
        me = await bot.get_me()
        logger.info("Бот @%s запущен и опрашивает Telegram", me.username)

    @router.shutdown()
    async def on_shutdown(bot: Bot) -> None:
        logger.info("Бот останавливается")

    return router


# ---------------------------------------------------------------------------
# Анти-флуд
# ---------------------------------------------------------------------------


class ThrottlingMiddleware(BaseMiddleware):
    """Пропускает не более `limit` апдейтов от пользователя за `window` секунд.

    Лишние апдейты отбрасываются: бот не отвечает на спам кнопками и текстом.
    """

    def __init__(self, limit: int = THROTTLE_LIMIT, window: float = THROTTLE_WINDOW_SEC) -> None:
        super().__init__()
        self._limit = limit
        self._window = window
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._last_warn: dict[int, float] = {}
        self._last_cleanup = 0.0

    @staticmethod
    def _extract_user_id(event: Update) -> int | None:
        if event.callback_query:
            return event.callback_query.from_user.id
        if event.message and event.message.from_user:
            return event.message.from_user.id
        if event.edited_message and event.edited_message.from_user:
            return event.edited_message.from_user.id
        return None

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        user_id = self._extract_user_id(event)
        if user_id is None:
            # Анонимные события (посты каналов, from_user=None) - молча игнорируем:
            # троттлинг к ним неприменим, и отвечать на них бот не должен.
            return None
        now = time.monotonic()
        hits = self._hits[user_id]
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            logger.info("Анти-флуд: user_id=%s, апдейт отброшен", user_id)
            if event.callback_query:
                # Тост с текстом шлём максимум раз в секунду на пользователя:
                # отвечать на КАЖДЫЙ спам-коллбэк - самому стать источником флуда
                # и выедать общую квоту бота.
                if now - self._last_warn.get(user_id, 0.0) >= 1.0:
                    self._last_warn[user_id] = now
                    try:
                        await event.callback_query.answer(steps.THROTTLE_TEXT)
                    except Exception:  # noqa: BLE001 - не роняем цикл из-за тоста
                        pass
            return None
        hits.append(now)
        if len(self._hits) > 10_000 and now - self._last_cleanup >= self._window:
            # Мягкая защита от роста словаря на очень долгом аптайме. Скан O(n)
            # запускаем не чаще раза за окно анти-флуда, а не на каждом апдейте.
            self._last_cleanup = now
            for stale_id in [
                uid
                for uid, h in self._hits.items()
                if not h or now - h[-1] > self._window
            ]:
                self._hits.pop(stale_id, None)
                self._last_warn.pop(stale_id, None)
        return await handler(event, data)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    level_name = os.getenv(LOG_LEVEL_ENV, "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    if level > logging.DEBUG:
        logging.getLogger("aiogram").setLevel(logging.WARNING)


def main() -> None:
    load_dotenv()
    _configure_logging()

    token = os.getenv(BOT_TOKEN_ENV, "").strip()
    if not token:
        logger.critical(
            "BOT_TOKEN не задан. Скопируйте .env.example в .env и впишите токен "
            "нового бота (создаётся у @BotFather командой /newbot)."
        )
        sys.exit(2)

    try:
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    except TokenValidationError:
        logger.critical(
            "BOT_TOKEN имеет неверный формат. Проверьте токен: он целиком вида "
            "1234567890:AAe...xA и получен у @BotFather."
        )
        sys.exit(2)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(create_router())
    dp.update.outer_middleware(ThrottlingMiddleware())

    logger.info("Запускаю long polling...")
    # drop_pending_updates: не разгребаем хвост апдейтов, накопившийся за простой,
    # чтобы старые нажатия кнопок не сыпались против свежего FSM-состояния.
    # Живучесть при обрывах сети обеспечивает НЕ этот процесс (повторный run_polling
    # в одном процессе невозможен - event loop закрывается), а systemd:
    # Restart=on-failure поднимает свежий процесс каждые RestartSec секунд.
    try:
        dp.run_polling(bot, drop_pending_updates=True)
    except TelegramNetworkError as exc:
        logger.error("Сеть до Telegram недоступна (%s), отдаю процесс на перезапуск "
                     "systemd-у.", exc)
        sys.exit(1)
    except TokenValidationError:
        logger.critical("Токен стал невалиден (бот удалён или перевыпущен?).")
        sys.exit(2)


if __name__ == "__main__":
    main()
