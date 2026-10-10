"""Проверки целостности контента steps.py.

Запуск любым способом:
    pytest tests/
или без pytest:
    python3 tests/test_steps.py

Модуль использует только стандартную библиотеку.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import steps  # noqa: E402

ALLOWED_TAGS = {"<b>", "</b>", "<i>", "</i>", "<u>", "</u>", "<code>", "</code>", "<pre>", "</pre>"}
ANY_TAG = re.compile(r"<[^>]+>")
BARE_AMPERSAND = re.compile(r"&(?!amp;|lt;|gt;|quot;|#)")
EM_DASH = "—"

MIN_STEPS_PER_FLOW = 5
MAX_STEPS_PER_FLOW = 15
MIN_CAUSES_PER_STEP = 3
MAX_CAUSES_PER_STEP = 5


def _all_steps() -> Iterable[tuple[str, steps.Step]]:
    for os_key, flow in steps.FLOWS.items():
        for step in flow:
            yield os_key, step


def _all_texts() -> Iterable[str]:
    for _, step in _all_steps():
        yield step.title
        yield step.body
        yield step.tip
        for cause in step.causes:
            yield cause.title
            yield cause.fix
    yield steps.GREETING
    yield steps.HINT_CHOOSE_OS
    yield steps.HINT_BUTTONS
    yield steps.ERROR_TEXT
    yield steps.THROTTLE_TEXT
    yield steps.FINAL_TITLE
    yield steps.FINAL_UPSELL_HEAD
    yield steps.FINAL_UPSELL
    for item in steps.FINAL_CHECKLIST:
        yield item
    yield steps.human_text("")
    yield steps.human_text("support_nick")
    yield steps.final_text("")
    yield steps.final_text("support_nick")


def test_flows_present() -> None:
    assert set(steps.FLOWS) == {"windows", "docker"}
    assert all(isinstance(flow, tuple) and flow for flow in steps.FLOWS.values())


def test_flow_sizes_sane() -> None:
    for os_key, flow in steps.FLOWS.items():
        assert MIN_STEPS_PER_FLOW <= len(flow) <= MAX_STEPS_PER_FLOW, os_key


def test_step_ids_unique_and_clean() -> None:
    ids: list[str] = [step.id for _, step in _all_steps()]
    assert len(ids) == len(set(ids)), "дубликаты id шагов"
    pattern = re.compile(r"^[a-z0-9_]+$")
    for step_id in ids:
        assert pattern.match(step_id), f"некорректный id: {step_id}"


def test_step_fields_non_empty() -> None:
    for _, step in _all_steps():
        assert step.title.strip(), step.id
        assert step.body.strip(), step.id


def test_trouble_causes_count() -> None:
    """По спеке: на каждый шаг - 3-5 причин с решениями."""
    for _, step in _all_steps():
        count = len(step.causes)
        assert MIN_CAUSES_PER_STEP <= count <= MAX_CAUSES_PER_STEP, f"{step.id}: {count} причин"
        for cause in step.causes:
            assert cause.title.strip(), step.id
            assert cause.fix.strip(), step.id


def test_texts_html_safe() -> None:
    """Только разрешённые теги и ни одного «голого» амперсанда."""
    for text in _all_texts():
        for tag in ANY_TAG.findall(text):
            assert tag in ALLOWED_TAGS, f"недопустимый тег {tag} в: {text[:80]}"
        assert not BARE_AMPERSAND.search(text), f"голый амперсанд в: {text[:80]}"


def test_no_em_dash() -> None:
    """Стиль проекта: длинное тире запрещено, используем « - »."""
    for text in _all_texts():
        assert EM_DASH not in text, f"длинное тире в: {text[:80]}"


def test_human_text_mentions_contact() -> None:
    without = steps.human_text("")
    with_nick = steps.human_text("support_nick")
    assert "напишите" in without.lower()
    assert "@support_nick" in with_nick
    assert "golden_key" in without  # напоминание про безопасность


def test_final_text_structure() -> None:
    text = steps.final_text("support_nick")
    assert "Чеклист финальной проверки" in text
    assert "Монитор позиций" in text
    assert "ИИ-автоответчик" in text
    assert "@support_nick" in text
    assert "необязательно" in text  # мягкость апселла


if __name__ == "__main__":
    failed = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"OK   {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    print("Итог:", "все проверки пройдены" if not failed else f"{failed} провалено")
    sys.exit(1 if failed else 0)
