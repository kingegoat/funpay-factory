# -*- coding: utf-8 -*-
"""Тесты парсера выдачи FunPay для плагина «Монитор позиций».

Прогон по реальной фикстуре: fixtures/lots_1392_rows.html (путь от корня репозитория)
(снимок страницы funpay.com/lots/1392/ от 10.10.2026, 40 строк).

Запуск: python3 -m pytest tests/ -q   (нужны beautifulsoup4 и pytest).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import monitor_positions as mp  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "lots_1392_rows.html"

LAZYLOAD_ROW = (
    '<a href="https://funpay.com/lots/offer?id=111222" class="tc-item lazyload-hidden" '
    'data-online="0" data-f-type="прочее">'
    '<div class="tc-desc"><div class="tc-desc-text">Тестовый лот, Прочее</div></div>'
    '<div class="tc-user"><div class="media-body"><div class="media-user-name">testseller'
    "</div></div></div>"
    '<div class="tc-price" data-s="5.0"><div>5.00 <span class="unit">₽</span></div></div>'
    "</a>"
)


def _rows() -> list[mp.OfferRow]:
    return mp.parse_lots_html(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_exists() -> None:
    assert FIXTURE.is_file(), f"нет фикстуры: {FIXTURE}"


def test_parses_all_40_rows() -> None:
    rows = _rows()
    assert len(rows) == 40


def test_positions_are_sequential() -> None:
    rows = _rows()
    assert [r.position for r in rows] == list(range(1, 41))


def test_first_row_is_promo() -> None:
    first = _rows()[0]
    assert first.offer_id == 70000001
    assert first.promo is True          # класс offer-promo offer-promoted
    assert first.position_organic is None
    assert first.seller == "seller11"
    assert first.reviews == 1938
    assert first.price == 10.54107      # сравнение по data-s
    assert first.currency == "₽"
    assert first.online is True
    assert first.auto is False
    assert "СИНТЕТИЧЕСКИЙ ЛОТ" in first.title


def test_known_lot_rayproject() -> None:
    row = mp.find_offer(_rows(), 32500001)
    assert row is not None
    assert row.position == 2
    assert row.position_organic == 1   # первый среди строк без закрепа
    assert row.seller == "seller9"
    assert row.reviews == 4217
    assert row.price == 26.435845
    assert row.auto is True
    assert row.promo is False


def test_six_promo_rows_and_34_organic() -> None:
    rows = _rows()
    assert sum(1 for r in rows if r.promo) == 6
    organic = [r for r in rows if not r.promo]
    assert len(organic) == 34
    assert [r.position_organic for r in organic] == list(range(1, 35))


def test_all_prices_parsed_from_data_s() -> None:
    for row in _rows():
        assert row.price is not None, f"нет цены у {row.offer_id}"
        assert isinstance(row.price, float)


def test_duplicate_offer_returns_first_position() -> None:
    rows = _rows()
    positions = [r.position for r in rows if r.offer_id == 70000001]
    assert positions == [1, 36]        # лот в закрепе дублируется в выдаче
    assert mp.find_offer(rows, 70000001).position == 1


def test_find_offer_missing_returns_none() -> None:
    assert mp.find_offer(_rows(), 999999) is None


def test_competitors_above_known_lot() -> None:
    rows = _rows()
    row = mp.find_offer(rows, 32500001)
    above = mp.competitors_above(rows, row)
    assert len(above) == 1
    assert above[0].offer_id == 70000001


def test_dumping_detection() -> None:
    rows = _rows()
    row = mp.find_offer(rows, 32500001)
    dumpers = mp.cheaper_competitors_above(rows, row)
    assert len(dumpers) == 1           # 10.54 < 26.44
    assert dumpers[0].offer_id == 70000001
    # у первого строки (закреп) конкурентов выше нет
    assert mp.cheaper_competitors_above(rows, rows[0]) == []


def test_lazyload_hidden_row_is_counted() -> None:
    html_text = FIXTURE.read_text(encoding="utf-8").replace("</body>", LAZYLOAD_ROW + "</body>")
    rows = mp.parse_lots_html(html_text)
    assert len(rows) == 41
    last = rows[-1]
    assert last.offer_id == 111222
    assert last.position == 41
    assert last.position_organic == 35
    assert last.price == 5.0
    assert last.online is False


def test_offer_id_from_href_variants() -> None:
    assert mp._offer_id_from_href("https://funpay.com/lots/offer?id=123") == 123
    assert mp._offer_id_from_href("https://funpay.com/lots/offer?id=123&x=1") == 123
    assert mp._offer_id_from_href("/lots/offer?id=99") == 99
    assert mp._offer_id_from_href("https://funpay.com/") is None
    assert mp._offer_id_from_href("") is None


def test_price_from_text_fallback() -> None:
    assert mp._price_from_text("1 000") == 1000.0
    assert mp._price_from_text("26,44") == 26.44
    assert mp._price_from_text("abc") is None
    assert mp._price_from_text(None) is None


def test_rows_from_api_lots() -> None:
    def lot(offer_id: int, price: float, promo: bool, seller: str):
        return types.SimpleNamespace(
            id=offer_id, description=f"Лот {offer_id}", price=price, promo=promo,
            auto=True, currency="₽",
            seller=types.SimpleNamespace(username=seller, online=True, reviews=10),
        )

    rows = mp.rows_from_api_lots([lot(1, 9.9, True, "a"), lot(2, 19.9, False, "b")])
    assert [r.position for r in rows] == [1, 2]
    assert rows[0].promo is True and rows[0].position_organic is None
    assert rows[1].promo is False and rows[1].position_organic == 1
    assert rows[1].price == 19.9 and rows[1].seller == "b"


def test_cooldown_allowed() -> None:
    state = mp.LotState(lot_id=1, last_alerts={"drop": 100.0})
    assert mp.cooldown_allowed(state, "drop", 100.0 + 899.0, 900.0) is False
    assert mp.cooldown_allowed(state, "drop", 100.0 + 900.0, 900.0) is True
    assert mp.cooldown_allowed(state, "dumping", 100.0, 900.0) is True  # другой тип


def test_format_position_delta() -> None:
    assert mp.format_position_delta(3, 5) == "3 → 5 (↓2)"
    assert mp.format_position_delta(5, 3) == "5 → 3 (↑2)"
    assert mp.format_position_delta(4, 4) == "4 → 4 (=)"
    assert mp.format_position_delta(None, 2) == "нет данных"


def test_module_metadata_for_cardinal() -> None:
    # обязательные атрибуты плагина Cardinal (иначе FieldNotExistsError)
    for attr in ("NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE",
                 "UUID", "BIND_TO_DELETE"):
        assert hasattr(mp, attr), attr
    # хуки-списки вызываются перебором элементов...
    for hook in ("BIND_TO_PRE_INIT", "BIND_TO_POST_START", "BIND_TO_PRE_STOP"):
        assert isinstance(getattr(mp, hook), list), hook
    # ...а BIND_TO_DELETE Cardinal хранит как одиночный callable (delete_handler)
    assert callable(mp.BIND_TO_DELETE)
    import uuid as _uuid

    parsed = _uuid.UUID(mp.UUID)
    assert parsed.version == 4
