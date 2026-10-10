"""Smoke-тест без сети: импорт bot.py, сборка Dispatcher, рендер всех экранов.

Запуск: .venv/bin/python tests/smoke_import.py
Токен не нужен: проверяется только конструирование Bot с форматно-валидным
фейковым значением, собранным из частей (нигде не хранится).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot as botmod  # noqa: E402
import steps  # noqa: E402
from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402


def main() -> int:
    # 1) импорт без BOT_TOKEN не падает (токен проверяется только в main())
    router = botmod.create_router()
    print(
        "import ok; handlers:",
        len(router.callback_query.handlers), "callback,",
        len(router.message.handlers), "message,",
        len(router.errors.handlers), "errors",
    )

    # 2) сборка диспетчера и подключение анти-флуда
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.update.outer_middleware(botmod.ThrottlingMiddleware())
    print("dispatcher ok")

    # 3) конструирование Bot с форматно-валидным значением (частями, не константой)
    fake = f"{10 ** 9}:{'AA' + 'x' * 34}"
    bot = Bot(token=fake, default=None)
    print("bot constructed ok")

    # 4) все экраны рендерятся и влезают в лимит Telegram (4096)
    worst = 0
    for os_key, flow in steps.FLOWS.items():
        for i in range(len(flow)):
            screen = botmod._render_step(os_key, i)
            trouble = botmod._render_trouble(os_key, i)
            assert 0 < len(screen) < 4096, (os_key, i, len(screen))
            assert 0 < len(trouble) < 4096, (os_key, i, len(trouble))
            worst = max(worst, len(screen), len(trouble))
            for c in range(len(flow[i].causes)):
                rendered = botmod._render_cause(os_key, i, c)
                assert 0 < len(rendered) < 4096, (os_key, i, c)
                worst = max(worst, len(rendered))
    for extra in (steps.human_text("support"), steps.final_text("support")):
        assert 0 < len(extra) < 4096
        worst = max(worst, len(extra))
    print("renders ok, max screen length:", worst)

    # 5) клавиатуры: длина текста кнопок и callback_data
    for flow in steps.FLOWS.values():
        for step in flow:
            kb = botmod.trouble_keyboard(step)
            flat = [btn for row in kb.inline_keyboard for btn in row]
            assert all(len(btn.callback_data) <= 64 for btn in flat)
            assert all(len(btn.text) <= 60 for btn in flat)
    print("keyboards ok")

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
