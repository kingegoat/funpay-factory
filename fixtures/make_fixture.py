# -*- coding: utf-8 -*-
"""Генератор синтетической фикстуры выдачи FunPay: fixtures/lots_1392_rows.html.

Фикстура НЕ содержит данных реальных продавцов: offer id, id пользователей,
ссылки на профили, аватары и названия лотов - выдуманные. Структура же HTML
(классы tc-item / offer-promo / lazyload-hidden, data-s у цены, rating-mini-count,
дублирование закреплённых лотов на границах «ленивых» блоков) повторяет
устройство настоящей выдачи funpay.com/lots/<node_id>/ - по ней тестируется
парсер плагина «Монитор позиций».

Перегенерация:  python3 fixtures/make_fixture.py
"""
from __future__ import annotations

import os

ROWS_TOTAL = 40          # как в исходной странице: 1 закреп + 34 обычных + 5 закрепов
PROMO_DUP_ID = 70000001  # лот-закреп: встречается в выдаче дважды (строки 1 и 36)
KNOWN_LOT_ID = 32500001  # «известный» лот из тестов: строка 2, первый органический

_F_TYPES = ["гайды", "аккаунты", "прочее", "ключи"]
_TITLES = [
    "СИНТЕТИЧЕСКИЙ ЛОТ {n} | ТЕСТОВАЯ ВЫДАЧА | АВТОВЫДАЧА, {ftype}",
    "Тестовый лот {n}: проверка парсера, {ftype}",
    "Синтетика {n} для фикстуры, {ftype}",
]


def _stars_html(stars: int) -> str:
    return ('<div class="rating-stars rating-%d">%s</div>'
            % (stars, "".join(['<i class="fas"></i>'] * stars)))


def _row(offer_id: int, seller: str, reviews: int, stars: int, years: int,
         price_s: float, online: bool, auto: bool, promo: bool, lazy: bool,
         title: str, ftype: str) -> str:
    user_id = 90000000 + (offer_id % 100000)  # выдуманный id пользователя
    classes = "tc-item"
    if promo:
        classes += " offer-promo offer-promoted"
    if lazy:
        classes += " lazyload-hidden"
    online_cls = " online" if online else ""
    price_disp = f"{price_s:.2f}"
    promo_icon = ('<div class="sc-offer-icons"> <i class="promo-offer-icon"></i></div>'
                  if promo else "")
    return f"""<a href="https://funpay.com/lots/offer?id={offer_id}" class="{classes}" data-online="{1 if online else 0}" data-user="{user_id}" data-auto="{1 if auto else 0}" data-f-type="{ftype}">
<div class="tc-desc">
<div class="tc-desc-text">{title}</div>
</div>
<div class="tc-user">
<div class="media media-user{online_cls} style-circle">
<div class="media-left">
<div class="avatar-photo" tabindex="0" data-href="https://funpay.com/users/{user_id}/" style="background-image: url(https://sfunpay.com/s/avatar/sy/n0/syn{user_id}.jpg);"></div>
</div>
<div class="media-body">
<div class="media-user-name">
{seller}</div>
<div class="media-user-reviews">
{_stars_html(stars)}<span class="rating-mini-count">{reviews}</span>
</div>
<div class="media-user-info">на сайте {years} лет</div>
</div>
</div>
</div><div class="tc-price" data-s="{price_s}">
<div>{price_disp} <span class="unit">₽</span></div>
{promo_icon}
</div>
</a>"""


def build_html() -> str:
    rows: list[str] = []
    # Строка 1: закреп-дубль (второе вхождение - строка 36), значения из тестов парсера.
    rows.append(_row(PROMO_DUP_ID, "seller11", 1938, 5, 4, 10.54107,
                     online=True, auto=False, promo=True, lazy=False,
                     title="🤖 СИНТЕТИЧЕСКИЙ ЛОТ 1 | ТЕСТОВАЯ ВЫДАЧА | АВТОВЫДАЧА, Гайды",
                     ftype="гайды"))
    # Строка 2: «известный» лот из тестов (автовыдача, цена/отзывы фиксированы тестами).
    rows.append(_row(KNOWN_LOT_ID, "seller9", 4217, 5, 6, 26.435845,
                     online=True, auto=True, promo=False, lazy=False,
                     title="Тестовый лот 2: проверка парсера, Аккаунты",
                     ftype="аккаунты"))
    # Строки 3-35: обычная органика.
    for i in range(3, 36):
        rows.append(_row(
            offer_id=33000000 + i,
            seller=f"seller{i % 20 + 1}",
            reviews=10 + i * 7 % 900,
            stars=3 + i % 3,
            years=1 + i % 7,
            price_s=round(5.0 + i * 1.37 % 90, 5),
            online=(i % 2 == 0),
            auto=(i % 3 == 0),
            promo=False,
            lazy=(i > 20),
            title=_TITLES[i % len(_TITLES)].format(
                n=i, ftype=_F_TYPES[i % len(_F_TYPES)].capitalize()),
            ftype=_F_TYPES[i % len(_F_TYPES)],
        ))
    # Строки 36-40: закрепы «второго блока»; строка 36 - дубль первого закрепа.
    rows.append(_row(PROMO_DUP_ID, "seller11", 1938, 5, 4, 10.54107,
                     online=True, auto=False, promo=True, lazy=True,
                     title="🤖 СИНТЕТИЧЕСКИЙ ЛОТ 1 | ТЕСТОВАЯ ВЫДАЧА | АВТОВЫДАЧА, Гайды",
                     ftype="гайды"))
    for i in range(37, 41):
        rows.append(_row(
            offer_id=34000000 + i,
            seller=f"seller{i % 20 + 1}",
            reviews=50 + i * 13 % 700,
            stars=4 + i % 2,
            years=2 + i % 5,
            price_s=round(7.0 + i * 2.11 % 80, 5),
            online=(i % 2 == 1),
            auto=(i % 2 == 0),
            promo=True,
            lazy=True,
            title=_TITLES[i % len(_TITLES)].format(
                n=i, ftype=_F_TYPES[i % len(_F_TYPES)].capitalize()),
            ftype=_F_TYPES[i % len(_F_TYPES)],
        ))
    assert len(rows) == ROWS_TOTAL, len(rows)

    head = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Синтетическая выдача funpay.com/lots/1392/ - тестовая фикстура</title>
</head>
<body>
<div class="content-without-header">
<div class="menu-list"></div>
<!-- СИНТЕТИЧЕСКИЙ снапшот структуры выдачи: все id, ники, аватары и названия
     выдуманы, реальных данных продавцов нет. Сгенерировано fixtures/make_fixture.py -->
<div class="tc">
"""
    tail = """
</div>
</div>
</body>
</html>
"""
    return head + "\n".join(rows) + tail


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lots_1392_rows.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(build_html())
    print(f"OK: {out} ({ROWS_TOTAL} строк, только синтетические данные)")
