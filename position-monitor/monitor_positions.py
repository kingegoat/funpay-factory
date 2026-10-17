# -*- coding: utf-8 -*-
"""
Плагин Cardinal «Монитор позиций».

Следит за позициями ваших лотов в выдаче FunPay (funpay.com/lots/<node_id>/):
- позиция среди всех строк и среди строк без платного закрепа (offer-promo);
- цена лота и конкуренты выше;
- алерты в Telegram: падение позиции, демпинг конкурентов, исчезновение лота;
- утренняя сводка и команда мгновенного отчёта /positions.

Плагин только читает публичную выдачу FunPay через сессию аккаунта.
Ничего не отправляет наружу, кроме запросов к funpay.com и уведомлений
вашего Telegram-бота Cardinal. Ключ golden_key никуда не передаётся.

Требования: Cardinal (python 3.10+), bs4, requests - всё уже в зависимостях Cardinal.
"""

from __future__ import annotations

import html as _html
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from bs4 import BeautifulSoup

try:  # lxml быстрее, но не обязателен: html.parser есть всегда
    import lxml  # noqa: F401

    _BEST_PARSER = "lxml"
except ImportError:
    _BEST_PARSER = "html.parser"

# ============================================================================
# ОБЯЗАТЕЛЬНЫЕ АТРИБУТЫ ПЛАГИНА CARDINAL
# ============================================================================

NAME: str = "Монитор позиций"
VERSION: str = "1.1.0"
DESCRIPTION: str = ("Следит за позициями лотов в выдаче FunPay: алерты о падении, "
                    "демпинге и исчезновении лота, утренняя сводка, команда /positions.")
CREDITS: str = "funpay-factory"
SETTINGS_PAGE: bool = False
UUID: str = "05026a8e-292a-46f3-b5b8-e485a87768b1"
BIND_TO_DELETE: Callable[..., Any] | None = None  # одиночный callable, заполняется ниже

logger = logging.getLogger("position_monitor")

# ============================================================================
# НАСТРОЙКИ (отредактируйте под себя и перезапустите Cardinal)
# ============================================================================

CONFIG: dict[str, Any] = {
    # Лоты для мониторинга. Можно указать несколько лотов, в том числе из разных разделов.
    #   node_id - ID раздела выдачи: funpay.com/lots/<node_id>/
    #   lot_id  - ID вашего лота: число из ссылки funpay.com/lots/offer?id=<lot_id>
    #             (открыть свою выдачу в браузере и скопировать id своей строки)
    #   name    - короткое имя лота для сводок и алертов
    # ПУСТО по умолчанию: мониторинг молчит, пока не впишете СВОИ лоты.
    "lots": [],

    # Интервал проверки выдачи, минут (минимум 1).
    "interval_min": 10,

    # Порог падения для алерта: если позиция ухудшилась на это число мест или больше.
    "drop_threshold": 5,

    # Час утренней сводки (локальное время, 0-23). Сводка приходит один раз в день.
    "summary_hour": 9,

    # Кулдаун повторных алертов одного типа на один лот, минут.
    "alert_cooldown_min": 15,

    # Сколько конкурентов выше показывать в отчётах и алертах.
    "competitors_in_report": 3,

    # Таймаут HTTP-запросов, секунд.
    "request_timeout": 30,

    # Присылать алерт о недоступности выдачи после N ошибок подряд (0 - не присылать).
    "fetch_fail_notify_after": 3,
}

_FUNPAY_NODE_URL = "https://funpay.com/lots/{node_id}/"

# ============================================================================
# МОДЕЛЬ ДАННЫХ ВЫДАЧИ
# ============================================================================


@dataclass
class OfferRow:
    """Одна строка выдачи (лот) с вычисленной позицией."""

    offer_id: int
    position: int                    # позиция среди всех строк выдачи (с 1)
    position_organic: int | None     # позиция среди строк без платного закрепа (с 1)
    title: str                       # текст из tc-desc-text
    seller: str                      # ник продавца
    reviews: int | None              # количество отзывов
    price: float | None              # цена по атрибуту data-s (для сравнения)
    currency: str                    # символ валюты из span.unit
    promo: bool                      # платное продвижение (offer-promo)
    online: bool                     # продавец онлайн
    auto: bool                       # автовыдача


@dataclass
class LotState:
    """Накопленное состояние одного отслеживаемого лота."""

    lot_id: int
    last_position: int | None = None
    last_organic: int | None = None
    last_price: float | None = None
    last_promo: bool | None = None   # был ли лот в закрепе на прошлой проверке
    seen: bool = False               # был найден на предыдущей проверке
    ever_seen: bool = False          # находился хотя бы раз с момента запуска
    missing_announced: bool = False  # уже сообщили, что лота нет с момента запуска
    first_position: int | None = None
    summary_position: int | None = None  # позиция на момент прошлой утренней сводки
    last_alerts: dict[str, float] = field(default_factory=dict)
    last_dumpers: dict[int, float] = field(default_factory=dict)  # демпингеры прошлого цикла: id -> цена


# ============================================================================
# ПАРСЕР HTML ВЫДАЧИ (чистые функции, тестируются по фикстуре)
# ============================================================================


def _offer_id_from_href(href: str) -> int | None:
    """Достаёт числовой offer id из href вида .../offer?id=32500000."""
    if not href:
        return None
    try:
        query = urllib.parse.urlparse(href).query
        params = urllib.parse.parse_qs(query)
        raw = params.get("id", [None])[0]
        if raw is None and "id=" in href:
            raw = href.split("id=", 1)[1].split("&", 1)[0]
        return int(raw) if raw is not None else None
    except (ValueError, TypeError):
        return None


def _price_from_text(text: str | None) -> float | None:
    """Парсит цену из текста вида '26.44', '1 000', '1000' (fallback, если нет data-s)."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").strip().split("\n")[0]
    cleaned = cleaned.replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_lots_html(html_text: str, parser: str | None = None) -> list[OfferRow]:
    """
    Парсит HTML страницы выдачи подкатегории (funpay.com/lots/<id>/).

    Строки с классом lazyload-hidden тоже учитываются: в HTML они присутствуют
    сервер-сайд, скрываются только ленивой загрузкой на клиенте.

    :param html_text: HTML страницы выдачи.
    :param parser: имя парсера bs4 (по умолчанию lxml, если установлен).
    :return: список строк выдачи в порядке следования в HTML.
    """
    soup = BeautifulSoup(html_text, parser or _BEST_PARSER)
    rows: list[OfferRow] = []
    organic_counter = 0
    for position, tag in enumerate(soup.find_all("a", class_="tc-item"), start=1):
        classes = tag.get("class") or []
        promo = "offer-promo" in classes or "offer-promoted" in classes
        if not promo:
            organic_counter += 1

        offer_id = _offer_id_from_href(tag.get("href", ""))

        desc_div = tag.find("div", class_="tc-desc-text")
        title = desc_div.get_text(strip=True) if desc_div else ""

        seller_div = tag.find("div", class_="media-user-name")
        seller = seller_div.get_text(strip=True) if seller_div else ""

        reviews_span = tag.find("span", class_="rating-mini-count")
        reviews: int | None = None
        if reviews_span is not None:
            digits = "".join(ch for ch in reviews_span.get_text() if ch.isdigit())
            reviews = int(digits) if digits else None

        price_div = tag.find("div", class_="tc-price")
        price: float | None = None
        currency = ""
        if price_div is not None:
            raw_s = price_div.get("data-s")
            if raw_s:
                try:
                    price = float(str(raw_s).strip())
                except ValueError:
                    price = None
            if price is None:
                first = price_div.find("div")
                price = _price_from_text(first.get_text(" ", strip=True) if first else None)
            unit = price_div.find("span", class_="unit")
            currency = unit.get_text(strip=True) if unit else ""

        rows.append(OfferRow(
            offer_id=offer_id if offer_id is not None else -1,
            position=position,
            position_organic=None if promo else organic_counter,
            title=title,
            seller=seller,
            reviews=reviews,
            price=price,
            currency=currency,
            promo=promo,
            online=tag.get("data-online") == "1",
            auto=tag.get("data-auto") == "1",
        ))
    return rows


def find_offer(rows: Sequence[OfferRow], lot_id: int) -> OfferRow | None:
    """Ищет строку выдачи с указанным offer id.

    Лот с платным продвижением встречается в выдаче дважды: закреп сверху и
    своя органическая строка ниже. Для алертов нужна именно органическая копия:
    у закрепа позиция всегда топовая, из-за чего падение и демпинг не видны.
    Если лот есть только в закрепе - возвращаем закреп (лучше, чем ничего).
    """
    promo_row: OfferRow | None = None
    for row in rows:
        if row.offer_id != int(lot_id):
            continue
        if not row.promo:
            return row
        if promo_row is None:
            promo_row = row
    return promo_row


def competitors_above(rows: Sequence[OfferRow], row: OfferRow) -> list[OfferRow]:
    """Все строки выдачи, находящиеся выше указанной.

    Собственный закреп-дубль лота (тот же offer id) не считается конкурентом.
    """
    return [r for r in rows[:row.position - 1] if r.offer_id != row.offer_id]


def cheaper_competitors_above(rows: Sequence[OfferRow], row: OfferRow) -> list[OfferRow]:
    """Конкуренты выше указанного лота, чья цена строго ниже (сравнение по data-s)."""
    if row.price is None:
        return []
    return [c for c in competitors_above(rows, row)
            if c.price is not None and c.price < row.price]


def cooldown_allowed(state: LotState, kind: str, now: float, cooldown_sec: float) -> bool:
    """True, если алерт типа kind для лота можно отправить (кулдаун истёк)."""
    last = state.last_alerts.get(kind)
    return last is None or (now - last) >= cooldown_sec


def mark_alert(state: LotState, kind: str, now: float) -> None:
    """Фиксирует время отправки алерта (снаружи вызывать под блокировкой _LOCK)."""
    state.last_alerts[kind] = now


def format_position_delta(old: int | None, new: int | None) -> str:
    """Форматирует изменение позиции: '3 → 5 (↓2)'. Меньший номер = выше = лучше."""
    if old is None:
        return "нет данных"
    if new == old:
        return f"{old} → {new} (=)"
    if new < old:
        return f"{old} → {new} (↑{old - new})"
    return f"{old} → {new} (↓{new - old})"


def esc(text: Any) -> str:
    """Экранирует текст для Telegram (parse_mode=HTML)."""
    return _html.escape(str(text if text is not None else ""))


def fmt_price(price: float | None, currency: str) -> str:
    """Форматирует цену: '26.44 ₽'."""
    if price is None:
        return "?"
    # currency приходит из чужой строки выдачи - экранируем как весь остальной текст
    suffix = f" {esc(currency)}" if currency else ""
    return f"{price:.2f}{suffix}"


def _format_competitor(row: OfferRow) -> str:
    flags = []
    if row.promo:
        flags.append("закреп")
    if row.auto:
        flags.append("авто")
    flag_text = f" [{'/'.join(flags)}]" if flags else ""
    return f"#{row.position} {esc(row.seller)} · {fmt_price(row.price, row.currency)}{flag_text}"


# ============================================================================
# ПОЛУЧЕНИЕ ВЫДАЧИ (через FunPayAPI, с fallback на собственный парсер)
# ============================================================================


_CURRENCY_SYMBOLS: dict[str, str] = {
    "RUB": "₽", "USD": "$", "EUR": "€", "UAH": "₴",
    "KZT": "₸", "GBP": "£", "BYN": "Br",
}


def _currency_symbol(value: Any) -> str:
    """Конвертирует валюту FunPayAPI (enum Currency или строка) в короткий символ.

    str(enum) у FunPayAPI даёт 'Currency.RUB' - вместо этого берём .name и
    подставляем символ; неизвестные валюты остаются трёхбуквенным кодом.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return _CURRENCY_SYMBOLS.get(name, name)
    text = str(value)
    return text if 0 < len(text) <= 3 else ""


def rows_from_api_lots(lots: Iterable[Any]) -> list[OfferRow]:
    """Конвертирует list[FunPayAPI.types.LotShortcut] в list[OfferRow].

    Порядок элементов списка FunPayAPI совпадает с порядком строк выдачи.
    """
    result: list[OfferRow] = []
    organic_counter = 0
    for position, lot in enumerate(lots, start=1):
        promo = bool(getattr(lot, "promo", False))
        if not promo:
            organic_counter += 1
        seller = getattr(lot, "seller", None)
        try:
            price = float(lot.price) if getattr(lot, "price", None) is not None else None
        except (TypeError, ValueError):
            price = None
        result.append(OfferRow(
            offer_id=int(getattr(lot, "id", -1)),
            position=position,
            position_organic=None if promo else organic_counter,
            title=(getattr(lot, "description", None) or "").strip(),
            seller=(getattr(seller, "username", None) or "").strip(),
            reviews=getattr(seller, "reviews", None),
            price=price,
            currency=_currency_symbol(getattr(lot, "currency", None)),
            promo=promo,
            online=bool(getattr(seller, "online", False)),
            auto=bool(getattr(lot, "auto", False)),
        ))
    return result


def _fetch_node_html(cardinal: Any, node_id: int) -> str:
    """Загружает HTML страницы выдачи через сессию аккаунта FunPay."""
    timeout = max(int(float(CONFIG.get("request_timeout") or 30)), 1)
    response = None
    # Основной путь: сессия аккаунта (cookies, user-agent, прокси) с явным
    # timeout - account.method() таймаут не передаёт, зависший сокет убил бы
    # поток мониторинга навсегда.
    session = getattr(cardinal.account, "session", None)
    if session is not None:
        response = session.get(_FUNPAY_NODE_URL.format(node_id=int(node_id)),
                               headers={"accept": "*/*"}, timeout=timeout)
    else:
        import requests

        response = requests.get(_FUNPAY_NODE_URL.format(node_id=int(node_id)),
                                headers={"User-Agent": "Mozilla/5.0",
                                         "accept": "*/*"},
                                timeout=timeout)
    response.raise_for_status()
    if len(response.content) > 2_000_000:
        raise RuntimeError(f"ответ раздела {node_id} подозрительно большой "
                           f"({len(response.content)} байт)")
    if not response.encoding:
        response.encoding = "utf-8"
    return response.text


def fetch_node_rows(cardinal: Any, node_id: int) -> tuple[list[OfferRow], str]:
    """
    Возвращает строки выдачи раздела.

    Сначала пробует cardinal.account.get_subcategory_public_lots() из FunPayAPI
    (позиции = порядок списка, цены, продавцы, закрепы). Если метод недоступен
    или упал - скачивает HTML страницы и парсит собственным парсером
    (структура tc-item / tc-price / data-s совпадает с выдачей FunPay).

    :return: (строки выдачи, источник: "api" | "html")
    """
    api_error: Exception | None = None
    try:
        from FunPayAPI.common.enums import SubCategoryTypes  # noqa: WPS433

        lots = cardinal.account.get_subcategory_public_lots(
            SubCategoryTypes.COMMON, int(node_id))
        if lots:
            return rows_from_api_lots(lots), "api"
        if lots == []:  # пустой ответ FUNPAYAPI - проверим HTML-ом, вдруг парсер библиотеки отстал
            logger.warning("FunPayAPI вернул пустую выдачу для раздела %s, "
                           "проверяю HTML вручную", node_id)
    except Exception as ex:  # pylint: disable=broad-except
        api_error = ex
        logger.debug("get_subcategory_public_lots не сработал, перехожу на HTML-парсер",
                     exc_info=True)

    html_text = _fetch_node_html(cardinal, node_id)
    rows = parse_lots_html(html_text)
    if api_error is not None and rows:
        logger.info("Раздел %s разобран собственным парсером (%d строк): %s",
                    node_id, len(rows), api_error.__class__.__name__)
    return rows, "html"


# ============================================================================
# СОСТОЯНИЕ И БЛОКИРОВКА
# ============================================================================

_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_CARDINAL: Any = None
_STATES: dict[int, LotState] = {}
_LAST_SUMMARY_DATE: str | None = None
_EMPTY_CONFIG_NOTIFIED: bool = False


def _configured_lots() -> list[dict[str, Any]]:
    """Валидирует CONFIG['lots']: приводит типы, отбрасывает битые записи."""
    validated: list[dict[str, Any]] = []
    for index, raw in enumerate(CONFIG.get("lots") or []):
        try:
            lot = {
                "node_id": int(raw["node_id"]),
                "lot_id": int(raw["lot_id"]),
                "name": str(raw.get("name") or f"лот {int(raw['lot_id'])}"),
            }
        except (KeyError, TypeError, ValueError):
            logger.warning("Некорректная запись lots[%s] в конфиге, пропускаю: %r",
                           index, raw)
            continue
        validated.append(lot)
    return validated


def _interval_seconds() -> float:
    try:
        minutes = float(CONFIG.get("interval_min", 10))
    except (TypeError, ValueError):
        minutes = 10.0
    return max(minutes, 1.0) * 60.0


def _cooldown_seconds() -> float:
    try:
        minutes = float(CONFIG.get("alert_cooldown_min", 15))
    except (TypeError, ValueError):
        minutes = 15.0
    return max(minutes, 0.0) * 60.0


# ============================================================================
# ОТПРАВКА УВЕДОМЛЕНИЙ TELEGRAM
# ============================================================================


def _send_tg(cardinal: Any, text: str) -> None:
    """Отправляет уведомление во все чаты уведомлений владельца."""
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        logger.info("Telegram отключён, уведомление не отправлено: %s",
                    _html.unescape(text)[:200])
        return
    try:
        tg.send_notification(text)
    except Exception:  # pylint: disable=broad-except
        logger.error("Не удалось отправить Telegram-уведомление")
        logger.debug("TRACEBACK", exc_info=True)


def _reply_tg(cardinal: Any, message: Any, text: str) -> None:
    """Отвечает в тот чат, откуда пришла команда."""
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    try:
        chat_id = message.chat.id
    except AttributeError:
        _send_tg(cardinal, text)
        return
    try:
        tg.bot.send_message(chat_id, text, disable_web_page_preview=True,
                            parse_mode="HTML")
    except Exception:  # pylint: disable=broad-except
        logger.error("Не удалось ответить в чат %s", chat_id)
        logger.debug("TRACEBACK", exc_info=True)
        _send_tg(cardinal, text)


# ============================================================================
# АЛЕРТЫ
# ============================================================================


def _top_competitors_text(rows: Sequence[OfferRow], row: OfferRow,
                          limit: int) -> str:
    """Ближайшие конкуренты выше указанного лота (последние limit строк сверху)."""
    above = competitors_above(rows, row)[-limit:]
    if not above:
        return "выше никого"
    return "; ".join(_format_competitor(c) for c in above)


def _process_lot(cardinal: Any, lot_cfg: dict[str, Any], rows: Sequence[OfferRow],
                 now: float) -> None:
    """Сравнивает текущее состояние лота с прошлым и шлёт алерты.

    Сообщения собираются под блокировкой, а отправляются после её снятия:
    сетевой вызов Telegram не должен задерживать обработчики команд.
    """
    lot_id = lot_cfg["lot_id"]
    name = lot_cfg["name"]
    threshold = max(int(float(CONFIG.get("drop_threshold", 5) or 5)), 1)
    limit = max(int(float(CONFIG.get("competitors_in_report", 3) or 3)), 0)
    cooldown_sec = _cooldown_seconds()
    row = find_offer(rows, lot_id)
    alerts: list[str] = []

    with _LOCK:
        state = _STATES.setdefault(lot_id, LotState(lot_id=lot_id))

        if row is None:
            state.last_dumpers = {}  # при повторном появлении демпинг пересчитается заново
            if state.seen:
                state.seen = False
                if cooldown_allowed(state, "missing", now, cooldown_sec):
                    mark_alert(state, "missing", now)
                    alerts.append(
                        f"❌ <b>Монитор позиций</b>\n"
                        f"Лот «{esc(name)}» (id {lot_id}) пропал из выдачи раздела "
                        f"{lot_cfg['node_id']}.\nПроверьте: снят с публикации, "
                        f"заблокирован или изменился ID.")
            elif not state.ever_seen and not state.missing_announced:
                state.missing_announced = True
                alerts.append(
                    f"⚠️ <b>Монитор позиций</b>\n"
                    f"Лот «{esc(name)}» (id {lot_id}) не найден в выдаче раздела "
                    f"{lot_cfg['node_id']} с момента запуска. Проверьте lot_id в конфиге.")
        else:
            state.seen = True
            state.ever_seen = True
            state.missing_announced = False
            if state.first_position is None:
                state.first_position = row.position

            # (0) конец платного продвижения: лот был в закрепе и выпал из него.
            # Это главный алерт для платящих: без закрепа поток покупателей
            # резко падает, а обычный «drop» его часто глотает порогом.
            if state.last_promo and not row.promo:
                if cooldown_allowed(state, "promo_end", now, cooldown_sec):
                    mark_alert(state, "promo_end", now)
                    alerts.append(
                        f"🏁 <b>Продвижение закончилось</b>\n"
                        f"Лот «{esc(name)}» выпал из закрепа и вернулся в обычную выдачу: "
                        f"{format_position_delta(state.last_position, row.position)}\n"
                        f"Цена: {fmt_price(row.price, row.currency)}\n"
                        f"Если продвижение окупалось - пора продлевать.")

            # (1) падение позиции
            if (state.last_position is not None
                    and row.position - state.last_position >= threshold
                    and cooldown_allowed(state, "drop", now, cooldown_sec)):
                mark_alert(state, "drop", now)
                alerts.append(
                    f"📉 <b>Монитор позиций</b>\n"
                    f"Лот «{esc(name)}» упал на {row.position - state.last_position} мест: "
                    f"{format_position_delta(state.last_position, row.position)}\n"
                    f"Цена: {fmt_price(row.price, row.currency)}\n"
                    f"Выше вас: {_top_competitors_text(rows, row, limit)}")

            # (2) демпинг: конкурент выше стал дешевле нашей цены (сравнение по data-s).
            # Алерт только по ИЗМЕНЕНИЮ (новый демпингер или снижение чужой цены ниже
            # нашей): при стабильной картине не спамим каждый цикл.
            dumpers = cheaper_competitors_above(rows, row)
            current_dumpers = {c.offer_id: c.price for c in dumpers if c.price is not None}
            fresh_dumpers = [c for c in dumpers if c.offer_id not in state.last_dumpers]
            if fresh_dumpers and cooldown_allowed(state, "dumping", now, cooldown_sec):
                mark_alert(state, "dumping", now)
                shown = sorted(fresh_dumpers, key=lambda c: (c.price is None, c.price))[:limit]
                listing = "\n".join(f"  {_format_competitor(c)}" for c in shown)
                more = (f"\n  ...и ещё {len(fresh_dumpers) - len(shown)}"
                        if len(fresh_dumpers) > len(shown) else "")
                total_note = (f"\nВсего конкурентов выше дешевле вас: {len(dumpers)}."
                              if len(dumpers) > len(fresh_dumpers) else "")
                alerts.append(
                    f"⚠️ <b>Демпинг</b>\n"
                    f"Лот «{esc(name)}»: ваша цена {fmt_price(row.price, row.currency)}, "
                    f"конкуренты выше стали продавать дешевле ({len(fresh_dumpers)} новых):\n"
                    f"{listing}{more}{total_note}")
            state.last_dumpers = current_dumpers

            state.last_position = row.position
            state.last_organic = row.position_organic
            state.last_price = row.price
            state.last_promo = row.promo

    for text in alerts:
        _send_tg(cardinal, text)


# ============================================================================
# УТРЕННЯЯ СВОДКА
# ============================================================================


def _maybe_morning_summary(cardinal: Any) -> None:
    """Присылает сводку один раз в день, начиная с часа summary_hour.

    Дельта «за ночь» считается от позиции на прошлой утренней сводке;
    если сводка первая с момента запуска - от первого замера после запуска.
    """
    global _LAST_SUMMARY_DATE
    lots = _configured_lots()
    if not lots:
        return
    try:
        summary_hour = int(CONFIG.get("summary_hour", 9))
    except (TypeError, ValueError):
        summary_hour = 9
    local = time.localtime()
    if local.tm_hour < summary_hour:
        return
    today = time.strftime("%Y-%m-%d", local)

    lines = [f"🌅 <b>Утренняя сводка по позициям</b> ({time.strftime('%d.%m.%Y', local)})"]
    with _LOCK:
        if _LAST_SUMMARY_DATE == today:
            return
        _LAST_SUMMARY_DATE = today
        for lot in lots:
            state = _STATES.get(lot["lot_id"])
            if state is None or state.last_position is None:
                lines.append(f"• {esc(lot['name'])}: нет данных")
                continue
            baseline = state.summary_position if state.summary_position is not None \
                else state.first_position
            delta = format_position_delta(baseline, state.last_position)
            organic = (f", без закрепа #{state.last_organic}"
                       if state.last_organic else "")
            pin = " (в закрепе)" if state.last_promo else ""
            lines.append(f"• {esc(lot['name'])}: #{state.last_position}{organic}{pin} | "
                         f"за ночь: {delta}")
            state.summary_position = state.last_position
    _send_tg(cardinal, "\n".join(lines))


# ============================================================================
# ЦИКЛ МОНИТОРИНГА
# ============================================================================


def _run_check_cycle(cardinal: Any, failures: dict[int, int]) -> None:
    """Один проход: загрузить выдачу каждого раздела и обработать лоты."""
    now = time.time()
    by_node: dict[int, list[dict[str, Any]]] = {}
    for lot in _configured_lots():
        by_node.setdefault(lot["node_id"], []).append(lot)

    notify_after = max(int(float(CONFIG.get("fetch_fail_notify_after", 3) or 3)), 0)
    for node_id, lots in by_node.items():
        try:
            rows, source = fetch_node_rows(cardinal, node_id)
            if not rows:
                # Пустая выдача - почти всегда сбой, а не «все лоты пропали»:
                # сменилась вёрстка (парсер не нашёл ни одной строки) или раздел
                # недоступен. Считаем это ошибкой запроса, а не пропажей лотов,
                # иначе смена вёрстки даёт лавину ложных алертов «лот пропал».
                raise RuntimeError(
                    "выдача пуста (0 строк): сменилась вёрстка или раздел недоступен")
            failures[node_id] = 0
        except Exception as ex:  # pylint: disable=broad-except
            failures[node_id] = failures.get(node_id, 0) + 1
            count = failures[node_id]
            logger.error("Не удалось получить выдачу раздела %s (попытка %d): %s",
                         node_id, count, ex)
            logger.debug("TRACEBACK", exc_info=True)
            if notify_after and count == notify_after:
                _send_tg(cardinal,
                         f"⚠️ <b>Монитор позиций</b>\n"
                         f"Не удалось получить выдачу раздела {node_id} "
                         f"{count} раз подряд: {esc(str(ex) or ex.__class__.__name__)[:160]}. "
                         f"Проверьте соединение и golden_key; если раздел открывается "
                         f"в браузере - вероятно, FunPay сменил вёрстку, обновите плагин.")
            continue

        logger.info("Раздел %s: %d строк (источник: %s)", node_id, len(rows), source)
        for lot in lots:
            try:
                _process_lot(cardinal, lot, rows, now)
            except Exception:  # pylint: disable=broad-except
                logger.error("Ошибка обработки лота %s", lot.get("lot_id"))
                logger.debug("TRACEBACK", exc_info=True)


def _monitor_loop(cardinal: Any) -> None:
    """Рабочий цикл потока мониторинга (daemon)."""
    global _EMPTY_CONFIG_NOTIFIED
    lots = _configured_lots()
    logger.info("Монитор позиций запущен: %d лот(ов), интервал %.0f мин",
                len(lots), _interval_seconds() / 60.0)
    if not lots:
        logger.warning("В CONFIG['lots'] нет корректных записей - мониторинг не выполняется")
        if not _EMPTY_CONFIG_NOTIFIED:
            _EMPTY_CONFIG_NOTIFIED = True
            _send_tg(cardinal,
                     "⚠️ <b>Монитор позиций</b>\n"
                     "В конфиге плагина не задано ни одного лота.\n"
                     "Откройте plugins/monitor_positions.py, в словаре CONFIG впишите "
                     "свои лоты: node_id раздела (число из адреса funpay.com/lots/&lt;node_id&gt;/), "
                     "lot_id (число из ссылки на ваш лот funpay.com/lots/offer?id=...), "
                     "короткое имя. Затем перезапустите Cardinal.\n"
                     "Справка: /pm_help")
    failures: dict[int, int] = {}
    while not _STOP.is_set():
        try:
            _run_check_cycle(cardinal, failures)
        except Exception:  # pylint: disable=broad-except
            logger.error("Цикл мониторинга упал с ошибкой")
            logger.debug("TRACEBACK", exc_info=True)
        try:
            _maybe_morning_summary(cardinal)
        except Exception:  # pylint: disable=broad-except
            logger.error("Не удалось отправить утреннюю сводку")
            logger.debug("TRACEBACK", exc_info=True)
        # Backoff: если раздел стабильно падает (403/429 FunPay, сеть),
        # увеличиваем паузу до x16, чтобы не долбить сессию аккаунта тем же темпом.
        delay = _interval_seconds()
        if failures:
            delay *= 2 ** min(max(failures.values()), 4)
        _STOP.wait(delay)
    logger.info("Монитор позиций остановлен")


# ============================================================================
# ОТЧЁТЫ ПО КОМАНДАМ
# ============================================================================


def build_positions_report(cardinal: Any) -> str:
    """Формирует мгновенный отчёт по всем лотам (команда /positions)."""
    lots = _configured_lots()
    header = f"📍 <b>Позиции</b> ({time.strftime('%d.%m.%Y %H:%M')})"
    if not lots:
        return header + "\nВ конфиге плагина не задано ни одного лота."
    limit = max(int(float(CONFIG.get("competitors_in_report", 3) or 3)), 0)
    lines = [header]
    by_node: dict[int, list[dict[str, Any]]] = {}
    for lot in lots:
        by_node.setdefault(lot["node_id"], []).append(lot)
    for node_id, node_lots in by_node.items():
        try:
            rows, _source = fetch_node_rows(cardinal, node_id)
        except Exception as ex:  # pylint: disable=broad-except
            lines.append(f"• Раздел {node_id}: ошибка получения выдачи "
                         f"({esc(ex.__class__.__name__)})")
            continue
        for lot in node_lots:
            row = find_offer(rows, lot["lot_id"])
            if row is None:
                lines.append(f"• {esc(lot['name'])} (id {lot['lot_id']}): "
                             f"❌ не найден в выдаче раздела {node_id}")
                continue
            organic = f", без закрепа #{row.position_organic}" if row.position_organic else ""
            lines.append(f"• {esc(lot['name'])} (id {lot['lot_id']}): "
                         f"#{row.position} из {len(rows)}{organic} · "
                         f"{fmt_price(row.price, row.currency)}")
            above = competitors_above(rows, row)[-limit:]
            if above:
                lines.append("  выше: " + "; ".join(_format_competitor(c) for c in above))
    return "\n".join(lines)


def _authorized_message(message: Any) -> bool:
    """Команды плагина - только владельцу бота (authorized_users Cardinal)."""
    cardinal = _CARDINAL
    allowed = set(getattr(getattr(cardinal, "telegram", None),
                          "authorized_users", {}) or {})
    user = getattr(message, "from_user", None)
    return user is not None and user.id in allowed


def _cmd_positions(message: Any) -> None:
    """Обработчик команды /positions: отвечает сразу, отчёт собирает в фоне.

    Сетевые запросы не занимают поток telebot: несколько одновременных
    /positions не затормозят остальные команды TG-бота.
    """
    cardinal = _CARDINAL
    if cardinal is None or not _authorized_message(message):
        return
    _reply_tg(cardinal, message, "Собираю отчёт по позициям...")

    def _worker() -> None:
        try:
            text = build_positions_report(cardinal)
        except Exception:  # pylint: disable=broad-except
            logger.error("Не удалось построить отчёт /positions")
            logger.debug("TRACEBACK", exc_info=True)
            text = "Ошибка при построении отчёта, подробности в логах Cardinal."
        _reply_tg(cardinal, message, text)

    threading.Thread(target=_worker, name="positions-report", daemon=True).start()


HELP_TEXT = (
    "<b>Монитор позиций</b> v" + VERSION + "\n"
    "Следит за вашими лотами в выдаче FunPay.\n\n"
    "<b>Команды</b>\n"
    "/positions - мгновенный отчёт по позициям и конкурентам\n"
    "/pm_help - эта справка\n\n"
    "<b>Алерты</b>\n"
    "- падение позиции на N мест и больше (порог drop_threshold);\n"
    "- демпинг: конкурент выше вас продаёт дешевле вашей цены;\n"
    "- лот пропал из выдачи;\n"
    "- утренняя сводка раз в день (summary_hour).\n\n"
    "<b>Настройка</b>\n"
    "Список лотов, интервал, пороги и кулдауны - в словаре CONFIG "
    "в начале файла monitor_positions.py. После правки перезапустите Cardinal.\n\n"
    "Плагин только читает публичную выдачу и никуда не передаёт данные аккаунта."
)


def _cmd_help(message: Any) -> None:
    """Обработчик команды /pm_help."""
    if _CARDINAL is None or not _authorized_message(message):
        return
    _reply_tg(_CARDINAL, message, HELP_TEXT)


# ============================================================================
# LIFECYCLE CARDINAL
# ============================================================================


def _register_telegram(cardinal: Any) -> None:
    """Регистрирует команды плагина (вызывается в BIND_TO_PRE_INIT,
    чтобы команды успели попасть в меню команд Telegram этого же запуска)."""
    try:
        cardinal.add_telegram_commands(UUID, [
            ("positions", "Монитор позиций: отчёт по позициям лотов", True),
            ("pm_help", "Монитор позиций: справка", True),
        ])
    except Exception:  # pylint: disable=broad-except
        logger.error("Не удалось зарегистрировать команды плагина")
        logger.debug("TRACEBACK", exc_info=True)

    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        logger.warning("Telegram-бот отключён: алерты и команды плагина недоступны")
        return
    try:
        tg.msg_handler(_cmd_positions, commands=["positions"])
        tg.msg_handler(_cmd_help, commands=["pm_help"])
    except Exception:  # pylint: disable=broad-except
        logger.error("Не удалось привязать обработчики команд Telegram")
        logger.debug("TRACEBACK", exc_info=True)


def _post_start(cardinal: Any) -> None:
    """BIND_TO_POST_START: аккаунт уже инициирован, запускаем поток мониторинга."""
    global _CARDINAL, _THREAD
    _CARDINAL = cardinal
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=_monitor_loop, args=(cardinal,),
                               name="position-monitor", daemon=True)
    _THREAD.start()


def _pre_stop(cardinal: Any) -> None:
    """BIND_TO_PRE_STOP: останавливаем поток мониторинга."""
    _STOP.set()


def _on_delete(cardinal: Any, *_args: Any) -> None:
    """Вызывается Cardinal при удалении плагина: останавливаем поток."""
    _STOP.set()


BIND_TO_PRE_INIT: list[Callable[..., Any]] = [_register_telegram]
BIND_TO_POST_START: list[Callable[..., Any]] = [_post_start]
BIND_TO_PRE_STOP: list[Callable[..., Any]] = [_pre_stop]
BIND_TO_DELETE = _on_delete  # Cardinal ожидает здесь одиночный callable, не список
