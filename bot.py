"""
bot.py (Tonnel, управление через inline-кнопки)

Никаких команд вводить не нужно — всё через кнопки:
  /start -> "➕ Добавить слежку" / "📋 Мои слежки"
  Добавить -> кнопки с популярными коллекциями + "✏️ Другое" (если своей
              коллекции нет в списке — бот один раз попросит написать
              название текстом, дальше снова только кнопки)
  Дальше -> кнопки с порогом (10% / 15% / 20% / 30%)
  Дальше -> бот спрашивает, до какой цены в TON искать (текстом) —
            или "Без лимита" кнопкой -> готово
  "Мои слежки" -> у каждой слежки есть "🔍 Проверить" (моментальный скан
                  с отчётом прямо в чат — без ожидания цикла) и "🗑" (снять)

Логика сравнения проверяет ОБА раздела Tonnel:
  1) Обычные лоты (getGifts) — сверяются с (а) медианной ценой по коллекции
     и (б) последней продажей NFT с точно таким же Model+Backdrop+Symbol.
  2) Аукционы (getAuctions) — текущая ставка сверяется с теми же двумя
     ориентирами.
Алерт — если позиция дешевле хотя бы по одному критерию на заданный
процент И (если задан лимит) укладывается в цену, которую попросил юзер.

ЧТО ИЗМЕНИЛОСЬ В ЭТОЙ ВЕРСИИ (быстрее и правильнее):
- Медиана для сравнения раньше считалась только по 30 САМЫМ ДЕШЁВЫМ лотам
  (sort="price_asc") — это занижало медиану и почти никогда не давало
  сработать порогу "дешевле медианы на X%", потому что сама медиана уже
  была смещена в дешёвую сторону. Теперь медиана считается по объединённой
  выборке price_asc + latest (до ~60 уникальных лотов) — заметно честнее.
- Раньше, если лотов было меньше MIN_LISTINGS_FOR_STATS, скан целой
  коллекции (включая аукционы!) просто пропускался. Теперь аукционы и
  сравнение с последней продажей продолжают работать даже при тонком рынке
  лотов — пропускается только сравнение с медианой, которому не хватает
  данных.
- Все сетевые запросы (лоты по двум сортировкам, история продаж, аукционы)
  внутри одной коллекции идут параллельно (asyncio.gather), а не по
  очереди — сам скан одной коллекции стал в разы быстрее.
- Разные отслеживаемые коллекции тоже сканируются параллельно (с
  ограничением SCAN_CONCURRENCY одновременных коллекций, чтобы не словить
  403 от Tonnel).
- POLL_INTERVAL_SECONDS теперь можно менять переменной окружения без
  правки кода (по умолчанию 120 сек вместо 300).
- Если у коллекции несколько раз подряд не получается получить данные с
  Tonnel (сеть, 403 и т.п.), бот один раз сам напишет об этом в чат —
  раньше ошибка была видна только в логах Railway, до которых с телефона
  не добраться.
- У каждой слежки в "Мои слежки" появилась кнопка "🔍 Проверить" —
  мгновенный скан с отчётом (сколько лотов/аукционов нашлось, какая
  медиана, почему алертов не было), чтобы не гадать вслепую, работает ли
  бот вообще.

ЧЕСТНО О ГРАНИЦАХ:
- Схема getGifts()/saleHistory() задокументирована автором пакета tonnelmp
  и уже используется другими людьми, но сам я её вживую не тестировал (нет
  доступа в интернет из моей песочницы) — мелкие расхождения в названиях
  полей всё ещё возможны.
- Структура ответа getAuctions() в документации пакета не расписана
  построчно (в отличие от getGifts()). Поле с текущей ставкой и id
  аукциона определяются перебором нескольких вероятных названий ключей
  (current_bid/currentBid/price/highest_bid и т.д.) — если реальные ключи
  называются иначе, бот пишет warning с сырым словарём в логи, и это же
  будет видно в отчёте "🔍 Проверить".
- limit у getAuctions() и getGifts() ограничен 30 за один запрос — это
  ограничение самого tonnelmp, обойти нельзя.
- Список коллекций-кнопок ниже (PRESET_COLLECTIONS) — просто те названия,
  что мы уже разбирали в переписке. Через "✏️ Другое" можно добавить любую
  другую коллекцию Tonnel по названию.

Установка:
    pip install aiogram tonnelmp fake-useragent --break-system-packages

Запуск:
    export BOT_TOKEN="токен от @BotFather"
    python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import statistics
from dataclasses import dataclass, field
from html import escape as _esc

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import tonnelmp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sniper")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
SCAN_CONCURRENCY = int(os.environ.get("SCAN_CONCURRENCY", "3"))

# Прокси для запросов к Tonnel (обход блокировки Cloudflare на дата-центровых
# IP — например Railway). Формат: http://user:pass@host:port или
# http://host:port. Одна и та же переменная используется и для http, и для
# https-запросов. Если не задана — запросы идут напрямую (и почти наверняка
# будут падать с 403 на большинстве облачных хостингов).
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
TONNEL_PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
MIN_LISTINGS_FOR_STATS = 5
SALES_HISTORY_LIMIT = 50
AUCTIONS_LIMIT = 30
CONSECUTIVE_ERRORS_BEFORE_NOTIFY = 3

PRESET_COLLECTIONS = [
    "Vice Cream", "Chill Flame", "Mood Pack", "Liberty Figure",
    "Snake Box", "Big Year", "Faith Amulet", "Jolly Chimp",
    "Bow Tie", "Xmas Stocking", "Santa Hat", "Ice Cream",
    "Party Sparkler", "Clover Pin", "Money Pot", "Candy Cane",
    "Easter Egg",
]
THRESHOLD_OPTIONS = [10, 15, 20, 30]

# Максимум для кастомного названия коллекции — в БАЙТАХ UTF-8, не в
# символах. callback_data в Telegram ограничен 64 байтами, а самый длинный
# префикс у нас "nolimit|" + "|30.0" ~= 13 байт, так что 40 байт на само
# название оставляет достаточный запас.
CUSTOM_NAME_MAX_BYTES = 40

# chat_id -> ждём, что следующее текстовое сообщение это название коллекции
awaiting_custom_name: set[int] = set()
# chat_id -> (name, threshold_pct) — ждём текстом максимальную цену в TON
awaiting_max_price: dict[int, tuple[str, float]] = {}


def strip_rarity(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw.split(" (")[0].strip()


def esc(value) -> str:
    """HTML-экранирование для любых динамических данных, которые попадают в
    текст сообщений (parse_mode=HTML): названия коллекций (в т.ч. введённые
    юзером через «Другое»), названия/модели/фоны NFT с Tonnel, тексты ошибок.
    Без этого символы <, >, & в имени коллекции или в тексте ошибки ломают
    отправку сообщения целиком (Telegram: 'can't parse entities')."""
    return _esc(str(value), quote=False)


def truncate_utf8(s: str, max_bytes: int) -> str:
    """Обрезает строку по байтам UTF-8, не разрывая символ посередине.
    Нужно для callback_data (лимит Telegram — 64 байта): обрезка по
    количеству СИМВОЛОВ не годится для кириллицы/эмодзи (2-4 байта на
    символ), иначе кнопки для такой слежки перестанут работать
    (BUTTON_DATA_INVALID)."""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    b = b[:max_bytes]
    while b:
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            b = b[:-1]
    return ""


def truncate_text(s: str, max_len: int = 300) -> str:
    """Обрезает длинные тексты ошибок (например, если tonnelmp когда-нибудь
    вернёт в исключении сырое тело HTML-страницы блокировки) — иначе можно
    упереться в лимит Telegram на длину сообщения (4096 символов) при
    нескольких длинных ошибках сразу."""
    s = str(s)
    return s if len(s) <= max_len else s[:max_len].rstrip() + "…"


@dataclass
class Listing:
    nft_id: str
    name: str
    price_ton: float
    gift_num: str | None = None
    model: str | None = None
    backdrop: str | None = None
    symbol: str | None = None

    @property
    def trait_key(self):
        return (self.model, self.backdrop, self.symbol)

    @property
    def tg_link(self) -> str | None:
        """Официальная ссылка Telegram на конкретный NFT-подарок (t.me/nft/Имя-номер)."""
        if not self.gift_num:
            return None
        slug = self.name.replace(" ", "")
        return f"https://t.me/nft/{slug}-{self.gift_num}"


@dataclass
class AuctionItem:
    auction_id: str
    name: str
    current_price: float
    gift_num: str | None = None
    model: str | None = None
    backdrop: str | None = None
    symbol: str | None = None

    @property
    def trait_key(self):
        return (self.model, self.backdrop, self.symbol)

    @property
    def tg_link(self) -> str | None:
        if not self.gift_num:
            return None
        slug = self.name.replace(" ", "")
        return f"https://t.me/nft/{slug}-{self.gift_num}"


@dataclass
class SaleRecord:
    price_ton: float
    timestamp: float
    model: str | None = None
    backdrop: str | None = None
    symbol: str | None = None

    @property
    def trait_key(self):
        return (self.model, self.backdrop, self.symbol)


@dataclass
class TrackedCollection:
    chat_id: int
    name: str
    threshold_pct: float
    max_price: float | None = None
    already_alerted: set = field(default_factory=set)
    consecutive_errors: int = 0
    error_notified: bool = False


tracked: list[TrackedCollection] = []


# ---------------- Tonnel ----------------

async def tonnel_listings_by_sort(name: str, sort: str) -> tuple[list[Listing], str | None]:
    def _call():
        return tonnelmp.getGifts(gift_name=name, sort=sort, limit=30, asset="TON", proxies=TONNEL_PROXIES)
    try:
        raw = await asyncio.to_thread(_call)
    except Exception as e:
        log.warning(f"getGifts('{name}', sort={sort}) не удался: {e}")
        return [], str(e)

    # tonnelmp в норме возвращает list, но некоторые обёртки при ошибке
    # молча отдают dict (например {"error": "..."}) вместо исключения —
    # без этой проверки `for it in raw` читал бы словарь по ключам-строкам
    # и падал с AttributeError на it.get(...), причём мимо try/except выше.
    if not isinstance(raw, list):
        msg = f"getGifts('{name}', sort={sort}) вернул неожиданный формат: {type(raw).__name__} — {raw!r}"[:300]
        log.warning(msg)
        return [], msg

    listings = []
    try:
        for it in raw:
            if not isinstance(it, dict):
                continue
            if it.get("status") != "forsale" or it.get("price") is None or not it.get("gift_id"):
                continue
            listings.append(Listing(
                nft_id=str(it.get("gift_id")),
                gift_num=it.get("gift_num"),  # номер вида #94634 — ключ поля не проверен вживую
                name=it.get("name", name),
                price_ton=float(it["price"]),
                model=strip_rarity(it.get("model")),
                backdrop=strip_rarity(it.get("backdrop")),
                symbol=strip_rarity(it.get("symbol")),
            ))
    except (TypeError, ValueError, KeyError) as e:
        log.warning(f"getGifts('{name}', sort={sort}): ошибка разбора ответа: {e}")
        return listings, f"ошибка разбора ответа: {e}"
    return listings, None


async def tonnel_market_sample(name: str) -> tuple[list[Listing], str | None]:
    """Объединённая выборка лотов: самые дешёвые (price_asc) + недавно
    выставленные (latest). Так медиана не смещена искусственно в дешёвую
    сторону (см. пояснение в шапке файла) и заодно расширяется до ~60
    уникальных лотов вместо 30."""
    (cheap, err1), (recent, err2) = await asyncio.gather(
        tonnel_listings_by_sort(name, "price_asc"),
        tonnel_listings_by_sort(name, "latest"),
    )
    merged: dict[str, Listing] = {}
    for l in cheap + recent:
        merged[l.nft_id] = l
    error = f"{err1} / {err2}" if (err1 and err2) else None
    return list(merged.values()), error


# Вероятные названия ключа с текущей ставкой/ценой и id аукциона —
# структура ответа getAuctions() нигде построчно не задокументирована,
# поэтому перебираем несколько вариантов (см. предупреждение в шапке файла).
_AUCTION_PRICE_KEYS = ("current_bid", "currentBid", "highest_bid", "highestBid", "price", "bid")
_AUCTION_ID_KEYS = ("auction_id", "auctionId", "id", "gift_id")


async def tonnel_auctions(name: str) -> tuple[list[AuctionItem], str | None]:
    def _call():
        return tonnelmp.getAuctions(gift_name=name, sort="latest", limit=AUCTIONS_LIMIT, asset="TON", proxies=TONNEL_PROXIES)
    try:
        raw = await asyncio.to_thread(_call)
    except Exception as e:
        log.warning(f"getAuctions('{name}') не удался: {e}")
        return [], str(e)

    if not isinstance(raw, list):
        msg = f"getAuctions('{name}') вернул неожиданный формат: {type(raw).__name__} — {raw!r}"[:300]
        log.warning(msg)
        return [], msg

    auctions = []
    unrecognized = 0
    for it in raw:
        if not isinstance(it, dict):
            unrecognized += 1
            continue
        auction_id = None
        for k in _AUCTION_ID_KEYS:
            if it.get(k) is not None:
                auction_id = str(it[k])
                break
        price = None
        for k in _AUCTION_PRICE_KEYS:
            if it.get(k) is not None:
                try:
                    price = float(it[k])
                except (TypeError, ValueError):
                    continue
                break
        if not auction_id or not price:
            unrecognized += 1
            log.warning(f"getAuctions('{name}'): не смог распознать id/цену в записи: {it}")
            continue
        auctions.append(AuctionItem(
            auction_id=auction_id,
            gift_num=it.get("gift_num"),
            name=it.get("name", name),
            current_price=price,
            model=strip_rarity(it.get("model")),
            backdrop=strip_rarity(it.get("backdrop")),
            symbol=strip_rarity(it.get("symbol")),
        ))
    error = None
    if raw and unrecognized == len(raw):
        error = "getAuctions() вернул данные, но не распознан ни один нужный ключ (см. логи)"
    return auctions, error


async def tonnel_sales_history(name: str) -> tuple[list[SaleRecord], str | None]:
    def _call():
        return tonnelmp.saleHistory(authData="", gift_name=name, limit=SALES_HISTORY_LIMIT, sort="latest", proxies=TONNEL_PROXIES)
    try:
        raw = await asyncio.to_thread(_call)
    except Exception as e:
        log.warning(f"saleHistory('{name}') не удался: {e}")
        return [], str(e)

    if not isinstance(raw, list):
        msg = f"saleHistory('{name}') вернул неожиданный формат: {type(raw).__name__} — {raw!r}"[:300]
        log.warning(msg)
        return [], msg

    sales = []
    try:
        for it in raw:
            if not isinstance(it, dict):
                continue
            price = it.get("price")
            if not price:
                continue
            sales.append(SaleRecord(
                price_ton=float(price),
                timestamp=float(it.get("timestamp") or it.get("date") or 0),
                model=strip_rarity(it.get("model")),
                backdrop=strip_rarity(it.get("backdrop")),
                symbol=strip_rarity(it.get("symbol")),
            ))
    except (TypeError, ValueError, KeyError) as e:
        log.warning(f"saleHistory('{name}'): ошибка разбора ответа: {e}")
        return sales, f"ошибка разбора ответа: {e}"
    return sales, None


def build_last_sale_index(sales: list[SaleRecord]) -> dict:
    index: dict = {}
    for s in sales:
        if s.model is None and s.backdrop is None and s.symbol is None:
            continue
        current = index.get(s.trait_key)
        if current is None or s.timestamp > current.timestamp:
            index[s.trait_key] = s
    return index


def discount_reasons(price: float, median_price: float, last_sale_index: dict, trait_key, threshold_pct: float) -> list[str]:
    reasons = []
    if median_price > 0:
        discount_vs_median = (median_price - price) / median_price * 100
        if discount_vs_median >= threshold_pct:
            reasons.append(f"дешевле медианы коллекции на {discount_vs_median:.0f}% (медиана {median_price:.2f} TON)")

    match = last_sale_index.get(trait_key)
    if match and match.price_ton > 0:
        discount_vs_last_sale = (match.price_ton - price) / match.price_ton * 100
        if discount_vs_last_sale >= threshold_pct:
            model, backdrop, symbol = trait_key
            reasons.append(
                f"дешевле последней продажи такого же Model/Backdrop/Symbol "
                f"({esc(model)} / {esc(backdrop)} / {esc(symbol)}) на {discount_vs_last_sale:.0f}% "
                f"(была за {match.price_ton:.2f} TON)"
            )
    return reasons


async def scan_collection(bot: Bot, tc: TrackedCollection, manual: bool = False) -> dict:
    """Сканирует одну коллекцию (лоты + аукционы), рассылает алерты.
    Возвращает диагностику — используется и для ручной проверки, и для
    учёта повторяющихся ошибок в фоновом цикле."""
    (listings, listings_err), (sales, sales_err), (auctions, auctions_err) = await asyncio.gather(
        tonnel_market_sample(tc.name),
        tonnel_sales_history(tc.name),
        tonnel_auctions(tc.name),
    )

    errors = [e for e in (listings_err, sales_err, auctions_err) if e]
    all_failed = bool(listings_err) and bool(auctions_err) and not listings and not auctions

    if all_failed:
        tc.consecutive_errors += 1
    else:
        tc.consecutive_errors = 0
        tc.error_notified = False

    if tc.consecutive_errors >= CONSECUTIVE_ERRORS_BEFORE_NOTIFY and not tc.error_notified and not manual:
        tc.error_notified = True
        last_error = esc(truncate_text(errors[0])) if errors else "неизвестна"
        await bot.send_message(
            tc.chat_id,
            f"⚠️ «{esc(tc.name)}»: {tc.consecutive_errors} раз(а) подряд не получилось "
            f"получить данные с Tonnel (возможно, 429/403 от их API или сменилось "
            f"название коллекции). Последняя ошибка: {last_error}.\n"
            f"Если ошибка про 403/CloudFlare — нужен прокси (переменная PROXY_URL), "
            f"без него Tonnel блокирует IP облачного хостинга.\n"
            f"Открой «Мои слежки» → «🔍 Проверить», чтобы посмотреть подробности.",
        )

    prices = [l.price_ton for l in listings]
    median_price = statistics.median(prices) if len(prices) >= MIN_LISTINGS_FOR_STATS else 0
    last_sale_index = build_last_sale_index(sales)

    alerts_sent = 0

    # --- обычные лоты ---
    for l in listings:
        if l.nft_id in tc.already_alerted:
            continue
        if tc.max_price is not None and l.price_ton > tc.max_price:
            continue

        reasons = discount_reasons(l.price_ton, median_price, last_sale_index, l.trait_key, tc.threshold_pct)
        if reasons:
            tc.already_alerted.add(l.nft_id)
            alerts_sent += 1
            reason_text = "\n".join(f"— {r}" for r in reasons)

            kb = None
            if l.tg_link:
                kb_builder = InlineKeyboardBuilder()
                kb_builder.button(text="🎁 Открыть подарок", url=l.tg_link)
                kb = kb_builder.as_markup()

            await bot.send_message(
                tc.chat_id,
                f"🔥 {esc(tc.name)}: {esc(l.name)}\nЦена сейчас: {l.price_ton:.2f} TON\n{reason_text}",
                reply_markup=kb,
            )

    # --- аукционы ---
    for a in auctions:
        alert_key = f"auction:{a.auction_id}"
        if alert_key in tc.already_alerted:
            continue
        if tc.max_price is not None and a.current_price > tc.max_price:
            continue

        reasons = discount_reasons(a.current_price, median_price, last_sale_index, a.trait_key, tc.threshold_pct)
        if reasons:
            tc.already_alerted.add(alert_key)
            alerts_sent += 1
            reason_text = "\n".join(f"— {r}" for r in reasons)

            kb = None
            if a.tg_link:
                kb_builder = InlineKeyboardBuilder()
                kb_builder.button(text="🎁 Открыть подарок", url=a.tg_link)
                kb = kb_builder.as_markup()

            await bot.send_message(
                tc.chat_id,
                f"🔨 {esc(tc.name)} (аукцион): {esc(a.name)}\n"
                f"Текущая ставка: {a.current_price:.2f} TON\n{reason_text}\n"
                f"⚠️ Это аукцион — ставка может вырасти, прежде чем ты успеешь купить.",
                reply_markup=kb,
            )

    return {
        "listings": len(listings),
        "auctions": len(auctions),
        "sales": len(sales),
        "median": median_price,
        "alerts_sent": alerts_sent,
        "errors": errors,
    }


async def poll_loop(bot: Bot):
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def bounded_scan(tc: TrackedCollection):
        async with sem:
            try:
                await scan_collection(bot, tc)
            except Exception as e:
                log.exception(f"Ошибка при сканировании «{tc.name}»: {e}")

    while True:
        if tracked:
            await asyncio.gather(*(bounded_scan(tc) for tc in list(tracked)))
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ---------------- Клавиатуры ----------------

def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛍 Открыть Tonnel Market", url="https://t.me/tonnel_network_bot")
    b.button(text="➕ Добавить слежку", callback_data="menu|add")
    b.button(text="📋 Мои слежки", callback_data="menu|list")
    b.adjust(1)
    return b.as_markup()


def collection_picker_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for name in PRESET_COLLECTIONS:
        b.button(text=name, callback_data=f"pick|{name}")
    b.button(text="✏️ Другое", callback_data="pick|__custom__")
    b.button(text="⬅️ Назад", callback_data="menu|back")
    b.adjust(2)
    return b.as_markup()


def threshold_picker_kb(name: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for pct in THRESHOLD_OPTIONS:
        b.button(text=f"{pct}%", callback_data=f"thr|{name}|{pct}")
    b.button(text="⬅️ Назад", callback_data="menu|add")
    b.adjust(len(THRESHOLD_OPTIONS))
    return b.as_markup()


def max_price_kb(name: str, threshold: float) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="♾ Без лимита", callback_data=f"nolimit|{name}|{threshold}")
    b.button(text="⬅️ Назад", callback_data="menu|add")
    b.adjust(1)
    return b.as_markup()


def my_watches_kb(chat_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    mine = [t for t in tracked if t.chat_id == chat_id]
    for t in mine:
        b.button(text=f"🔍 Проверить «{t.name}»", callback_data=f"chk|{t.name}")
        limit_label = f"до {t.max_price:.0f} TON" if t.max_price is not None else "без лимита"
        b.button(text=f"🗑 {t.name} ({t.threshold_pct:.0f}%, {limit_label})", callback_data=f"del|{t.name}")
    b.button(text="⬅️ Назад", callback_data="menu|back")
    b.adjust(1)
    return b.as_markup()


# ---------------- Хендлеры ----------------

def reset_pending_state(chat_id: int):
    awaiting_custom_name.discard(chat_id)
    awaiting_max_price.pop(chat_id, None)


def confirmation_text(name: str, threshold: float, max_price: float | None) -> str:
    limit_text = f"до {max_price:.2f} TON" if max_price is not None else "без ограничения по цене"
    return (
        f"✅ Слежу за «{esc(name)}», алерт от {threshold:.0f}% дешевле рынка, {limit_text}.\n"
        f"Проверяю обычные лоты и аукционы каждые {POLL_INTERVAL_SECONDS // 60} мин "
        f"({POLL_INTERVAL_SECONDS} сек)."
    )


def save_tracked(chat_id: int, name: str, threshold: float, max_price: float | None):
    existing = next((t for t in tracked if t.chat_id == chat_id and t.name == name), None)
    if existing:
        existing.threshold_pct = threshold
        existing.max_price = max_price
        existing.already_alerted.clear()  # параметры сменились — считаем алерты заново
        existing.consecutive_errors = 0
        existing.error_notified = False
    else:
        tracked.append(TrackedCollection(chat_id, name, threshold, max_price))


async def cmd_start(message: Message):
    reset_pending_state(message.chat.id)
    await message.answer(
        "<b>🎯 Gift Sniper — Tonnel</b>\n\n"
        "Слежу за лотами и аукционами на Tonnel и присылаю алерт, когда "
        "что-то заметно дешевле рынка — либо дешевле медианы по коллекции, "
        "либо дешевле последней продажи точно такого же подарка.\n\n"
        "Выбери действие 👇",
        reply_markup=main_menu_kb(),
    )


async def on_menu_back(callback: CallbackQuery):
    reset_pending_state(callback.message.chat.id)
    await callback.message.edit_text("Выбери действие:", reply_markup=main_menu_kb())
    await callback.answer()


async def on_menu_add(callback: CallbackQuery):
    reset_pending_state(callback.message.chat.id)
    await callback.message.edit_text(
        "Выбери коллекцию (или напиши свою через «Другое»):",
        reply_markup=collection_picker_kb(),
    )
    await callback.answer()


async def on_menu_list(callback: CallbackQuery):
    mine = [t for t in tracked if t.chat_id == callback.message.chat.id]
    if not mine:
        await callback.message.edit_text(
            "Пока нет активных слежек.", reply_markup=main_menu_kb()
        )
    else:
        await callback.message.edit_text(
            "Твои слежки:",
            reply_markup=my_watches_kb(callback.message.chat.id),
        )
    await callback.answer()


async def on_pick(callback: CallbackQuery):
    name = callback.data.split("|", 1)[1]
    if name == "__custom__":
        awaiting_custom_name.add(callback.message.chat.id)
        await callback.message.edit_text("Напиши название коллекции текстом (например: Toy Bear)")
        await callback.answer()
        return
    await callback.message.edit_text(
        f"«{esc(name)}» — на сколько % дешевле рынка алертить?",
        reply_markup=threshold_picker_kb(name),
    )
    await callback.answer()


async def on_threshold(callback: CallbackQuery):
    _, name, pct_str = callback.data.split("|", 2)
    threshold = float(pct_str)
    chat_id = callback.message.chat.id
    awaiting_max_price[chat_id] = (name, threshold)
    await callback.message.edit_text(
        f"«{esc(name)}», алерт от {threshold:.0f}% дешевле рынка.\n\n"
        f"До какой цены в TON искать офферы? Напиши число (например: 15 "
        f"или 15.5) — или нажми «Без лимита».",
        reply_markup=max_price_kb(name, threshold),
    )
    await callback.answer()


async def on_no_limit(callback: CallbackQuery):
    _, name, threshold_str = callback.data.split("|", 2)
    threshold = float(threshold_str)
    chat_id = callback.message.chat.id
    awaiting_max_price.pop(chat_id, None)
    save_tracked(chat_id, name, threshold, None)
    await callback.message.edit_text(confirmation_text(name, threshold, None), reply_markup=main_menu_kb())
    await callback.answer("Добавлено")


async def on_text(message: Message):
    """Общий текстовый хендлер: срабатывает только если чат ждёт название
    коллекции или максимальную цену (см. awaiting_custom_name / awaiting_max_price)."""
    chat_id = message.chat.id

    if chat_id in awaiting_custom_name:
        awaiting_custom_name.discard(chat_id)
        name = (message.text or "").strip().replace("|", " ").strip()
        name = truncate_utf8(name, CUSTOM_NAME_MAX_BYTES).strip()
        if not name:
            await message.answer("Пустое название не подойдёт, начни заново.", reply_markup=main_menu_kb())
            return
        await message.answer(
            f"«{esc(name)}» — на сколько % дешевле рынка алертить?",
            reply_markup=threshold_picker_kb(name),
        )
        return

    if chat_id in awaiting_max_price:
        name, threshold = awaiting_max_price[chat_id]
        raw = (message.text or "").strip().replace(",", ".")
        try:
            price = float(raw)
            if price <= 0:
                raise ValueError
        except ValueError:
            await message.answer(
                "Не понял цену. Напиши число в TON, например 15 или 15.5, "
                "либо нажми «Без лимита» в сообщении выше."
            )
            return
        awaiting_max_price.pop(chat_id, None)
        save_tracked(chat_id, name, threshold, price)
        await message.answer(confirmation_text(name, threshold, price), reply_markup=main_menu_kb())
        return

    # не в режиме ожидания — игнорируем, чтобы не мешать другим сообщениям


async def on_check_now(callback: CallbackQuery, bot: Bot):
    name = callback.data.split("|", 1)[1]
    chat_id = callback.message.chat.id
    tc = next((t for t in tracked if t.chat_id == chat_id and t.name == name), None)
    if not tc:
        await callback.answer("Слежка не найдена", show_alert=True)
        return
    await callback.answer("Проверяю…")
    try:
        stats = await scan_collection(bot, tc, manual=True)
    except Exception as e:
        # Без этого try/except неожиданная ошибка разбора ответа Tonnel
        # (см. tonnel_listings_by_sort и т.п.) молча "проглатывалась" бы
        # где-то в дебрях asyncio.gather, и юзер жал бы "Проверить" без
        # какой-либо реакции бота вообще.
        log.exception(f"on_check_now('{tc.name}') неожиданная ошибка: {e}")
        await callback.message.answer(
            f"⚠️ Проверка «{esc(tc.name)}» упала с неожиданной ошибкой:\n{esc(truncate_text(str(e)))}"
        )
        return

    limit_label = f"до {tc.max_price:.2f} TON" if tc.max_price is not None else "без лимита"
    lines = [
        f"🔍 Проверка «{esc(tc.name)}»",
        f"Условие: дешевле рынка на {tc.threshold_pct:.0f}%, {limit_label}",
        f"Лотов в выборке: {stats['listings']}"
        + (f" (медиана {stats['median']:.2f} TON)" if stats["median"] > 0 else " (мало данных для медианы)"),
        f"Записей в истории продаж: {stats['sales']}",
        f"Активных аукционов: {stats['auctions']}",
        f"Отправлено алертов сейчас: {stats['alerts_sent']}",
    ]
    if stats["alerts_sent"] == 0:
        lines.append("Ничего не подошло под условия — либо рынок сейчас дороже порога, либо всё уже было отправлено раньше.")
    if stats["errors"]:
        lines.append("⚠️ Ошибки запросов к Tonnel:")
        lines.extend(f"— {esc(truncate_text(e))}" for e in stats["errors"])

    report = "\n".join(lines)
    if len(report) > 3900:  # запас от лимита Telegram в 4096 символов
        report = report[:3900].rstrip() + "\n…(обрезано)"
    await callback.message.answer(report)


async def on_delete(callback: CallbackQuery):
    name = callback.data.split("|", 1)[1]
    chat_id = callback.message.chat.id
    tracked[:] = [t for t in tracked if not (t.chat_id == chat_id and t.name == name)]
    mine = [t for t in tracked if t.chat_id == chat_id]
    if mine:
        await callback.message.edit_text(
            "Твои слежки:", reply_markup=my_watches_kb(chat_id)
        )
    else:
        await callback.message.edit_text("Слежек не осталось.", reply_markup=main_menu_kb())
    await callback.answer(f"«{name}» убран")


async def on_unhandled_error(event, bot: Bot):
    """Глобальный перехватчик: без него необработанное исключение в любом
    хендлере (клик по кнопке, ввод текста) просто логируется aiogram-ом и
    гасится — юзер не получает вообще никакой обратной связи, выглядит так,
    будто бот завис. Здесь хотя бы шлём короткое сообщение в чат и не роняем
    процесс."""
    log.exception(f"Необработанная ошибка в хендлере: {event.exception}")
    update = event.update
    chat_id = None
    if update.message:
        chat_id = update.message.chat.id
    elif update.callback_query and update.callback_query.message:
        chat_id = update.callback_query.message.chat.id
    if chat_id:
        try:
            await bot.send_message(
                chat_id,
                "⚠️ Что-то пошло не так при обработке. Попробуй ещё раз или начни заново: /start",
            )
        except Exception:
            pass
    return True  # помечаем ошибку как обработанную


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Укажи BOT_TOKEN в переменных окружения")
    if not PROXY_URL:
        log.warning(
            "PROXY_URL не задан — запросы к Tonnel пойдут напрямую с IP этого "
            "сервера. На облачных хостингах (Railway и т.п.) Tonnel почти "
            "всегда отвечает 403 (Cloudflare) без прокси."
        )
    # DefaultBotProperties — синтаксис для aiogram 3.7+. На старых версиях
    # (маловероятно, pip ставит последнюю) замени на Bot(BOT_TOKEN, parse_mode="HTML")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.message.register(cmd_start, Command("start"))
    dp.callback_query.register(on_menu_back, F.data == "menu|back")
    dp.callback_query.register(on_menu_add, F.data == "menu|add")
    dp.callback_query.register(on_menu_list, F.data == "menu|list")
    dp.callback_query.register(on_pick, F.data.startswith("pick|"))
    dp.callback_query.register(on_threshold, F.data.startswith("thr|"))
    dp.callback_query.register(on_no_limit, F.data.startswith("nolimit|"))
    dp.callback_query.register(on_check_now, F.data.startswith("chk|"))
    dp.callback_query.register(on_delete, F.data.startswith("del|"))
    # Общий текстовый хендлер — сработает только если чат ждёт название
    # коллекции или максимальную цену (см. awaiting_custom_name / awaiting_max_price)
    dp.message.register(on_text, F.text)
    dp.errors.register(on_unhandled_error)

    asyncio.create_task(poll_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
