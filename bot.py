"""
bot.py (Tonnel, управление через inline-кнопки)

Никаких команд вводить не нужно — всё через кнопки:
  /start -> "➕ Добавить слежку" / "📋 Мои слежки"
  Добавить -> кнопки с популярными коллекциями + "✏️ Другое" (если своей
              коллекции нет в списке — бот один раз попросит написать
              название текстом, дальше снова только кнопки)
  Дальше -> кнопки с порогом (10% / 15% / 20% / 30%) -> готово

Логика сравнения не изменилась: раз в 5 минут бот сверяет активные лоты
коллекции на Tonnel с (а) медианной ценой по коллекции и (б) последней
продажей NFT с точно таким же Model+Backdrop+Symbol. Алерт — если лот
дешевле хотя бы по одному критерию на заданный процент.

ЧЕСТНО О ГРАНИЦАХ:
- Схема getGifts()/saleHistory() задокументирована автором пакета tonnelmp
  и уже используется другими людьми, но сам я её вживую не тестировал (нет
  доступа в интернет из моей песочницы) — мелкие расхождения в названиях
  полей всё ещё возможны.
- Список коллекций-кнопок ниже (PRESET_COLLECTIONS) — просто те названия,
  что мы уже разбирали в переписке. Через "✏️ Другое" можно добавить любую
  другую коллекцию Tonnel по названию.

Установка:
    pip install aiogram tonnelmp --break-system-packages

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
POLL_INTERVAL_SECONDS = 300
MIN_LISTINGS_FOR_STATS = 5
SALES_HISTORY_LIMIT = 50

PRESET_COLLECTIONS = [
    "Vice Cream", "Chill Flame", "Mood Pack", "Liberty Figure",
    "Snake Box", "Big Year", "Faith Amulet", "Jolly Chimp",
    "Bow Tie", "Xmas Stocking", "Santa Hat", "Ice Cream",
    "Party Sparkler", "Clover Pin", "Money Pot", "Candy Cane",
    "Easter Egg",
]
THRESHOLD_OPTIONS = [10, 15, 20, 30]

# chat_id -> ждём, что следующее текстовое сообщение это название коллекции
awaiting_custom_name: set[int] = set()


def strip_rarity(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw.split(" (")[0].strip()


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
    already_alerted: set = field(default_factory=set)


tracked: list[TrackedCollection] = []


# ---------------- Tonnel ----------------

async def tonnel_active_listings(name: str) -> list[Listing]:
    def _call():
        return tonnelmp.getGifts(gift_name=name, sort="price_asc", limit=30, asset="TON")
    try:
        raw = await asyncio.to_thread(_call)
    except Exception as e:
        log.warning(f"getGifts('{name}') не удался: {e}")
        return []
    listings = []
    for it in raw or []:
        if it.get("status") != "forsale" or not it.get("price"):
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
    return listings


async def tonnel_sales_history(name: str) -> list[SaleRecord]:
    def _call():
        return tonnelmp.saleHistory(authData="", gift_name=name, limit=SALES_HISTORY_LIMIT, sort="latest")
    try:
        raw = await asyncio.to_thread(_call)
    except Exception as e:
        log.warning(f"saleHistory('{name}') не удался: {e}")
        return []
    sales = []
    for it in raw or []:
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
    return sales


def build_last_sale_index(sales: list[SaleRecord]) -> dict:
    index: dict = {}
    for s in sales:
        if s.model is None and s.backdrop is None and s.symbol is None:
            continue
        current = index.get(s.trait_key)
        if current is None or s.timestamp > current.timestamp:
            index[s.trait_key] = s
    return index


async def scan_collection(bot: Bot, tc: TrackedCollection):
    listings = await tonnel_active_listings(tc.name)
    if len(listings) < MIN_LISTINGS_FOR_STATS:
        return

    prices = [l.price_ton for l in listings]
    median_price = statistics.median(prices)

    sales = await tonnel_sales_history(tc.name)
    last_sale_index = build_last_sale_index(sales)

    for l in listings:
        if l.nft_id in tc.already_alerted:
            continue

        reasons = []

        if median_price > 0:
            discount_vs_median = (median_price - l.price_ton) / median_price * 100
            if discount_vs_median >= tc.threshold_pct:
                reasons.append(f"дешевле медианы коллекции на {discount_vs_median:.0f}% (медиана {median_price:.2f} TON)")

        match = last_sale_index.get(l.trait_key)
        if match and match.price_ton > 0:
            discount_vs_last_sale = (match.price_ton - l.price_ton) / match.price_ton * 100
            if discount_vs_last_sale >= tc.threshold_pct:
                reasons.append(
                    f"дешевле последней продажи такого же Model/Backdrop/Symbol "
                    f"({l.model} / {l.backdrop} / {l.symbol}) на {discount_vs_last_sale:.0f}% "
                    f"(была за {match.price_ton:.2f} TON)"
                )

        if reasons:
            tc.already_alerted.add(l.nft_id)
            reason_text = "\n".join(f"— {r}" for r in reasons)

            kb = None
            if l.tg_link:
                # Официальный формат Telegram — самый надёжный, но полагается на то,
                # что gift_num вообще пришёл от tonnelmp (ключ поля не проверен вживую)
                kb_builder = InlineKeyboardBuilder()
                kb_builder.button(text="🎁 Открыть подарок", url=l.tg_link)
                kb = kb_builder.as_markup()

            await bot.send_message(
                tc.chat_id,
                f"🔥 {tc.name}: {l.name}\nЦена сейчас: {l.price_ton:.2f} TON\n{reason_text}",
                reply_markup=kb,
            )


async def poll_loop(bot: Bot):
    while True:
        for tc in list(tracked):
            try:
                await scan_collection(bot, tc)
            except Exception as e:
                log.exception(f"Ошибка при сканировании «{tc.name}»: {e}")
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


def my_watches_kb(chat_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    mine = [t for t in tracked if t.chat_id == chat_id]
    for t in mine:
        b.button(text=f"🗑 {t.name} ({t.threshold_pct:.0f}%)", callback_data=f"del|{t.name}")
    b.button(text="⬅️ Назад", callback_data="menu|back")
    b.adjust(1)
    return b.as_markup()


# ---------------- Хендлеры ----------------

async def cmd_start(message: Message):
    awaiting_custom_name.discard(message.chat.id)
    await message.answer(
        "<b>🎯 Gift Sniper — Tonnel</b>\n\n"
        "Слежу за лотами на Tonnel и присылаю алерт, когда что-то заметно "
        "дешевле рынка — либо дешевле медианы по коллекции, либо дешевле "
        "последней продажи точно такого же подарка.\n\n"
        "Выбери действие 👇",
        reply_markup=main_menu_kb(),
    )


async def on_menu_back(callback: CallbackQuery):
    awaiting_custom_name.discard(callback.message.chat.id)
    await callback.message.edit_text("Выбери действие:", reply_markup=main_menu_kb())
    await callback.answer()


async def on_menu_add(callback: CallbackQuery):
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
            "Твои слежки (нажми, чтобы убрать):",
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
        f"«{name}» — на сколько % дешевле рынка алертить?",
        reply_markup=threshold_picker_kb(name),
    )
    await callback.answer()


async def on_custom_name_text(message: Message):
    """Ловит текст ТОЛЬКО если чат только что нажал «Другое»."""
    if message.chat.id not in awaiting_custom_name:
        return  # не в режиме ожидания названия — игнорируем, чтобы не мешать другим сообщениям
    awaiting_custom_name.discard(message.chat.id)
    name = message.text.strip().replace("|", " ").strip()
    if len(name) > 40:
        name = name[:40].strip()
    if not name:
        await message.answer("Пустое название не подойдёт, начни заново.", reply_markup=main_menu_kb())
        return
    await message.answer(
        f"«{name}» — на сколько % дешевле рынка алертить?",
        reply_markup=threshold_picker_kb(name),
    )


async def on_threshold(callback: CallbackQuery):
    _, name, pct_str = callback.data.split("|", 2)
    threshold = float(pct_str)
    chat_id = callback.message.chat.id
    existing = next((t for t in tracked if t.chat_id == chat_id and t.name == name), None)
    if existing:
        existing.threshold_pct = threshold
        existing.already_alerted.clear()  # порог сменился — считаем алерты заново
    else:
        tracked.append(TrackedCollection(chat_id, name, threshold))
    await callback.message.edit_text(
        f"✅ Слежу за «{name}», алерт от {threshold:.0f}% дешевле рынка.\n"
        f"Проверка каждые {POLL_INTERVAL_SECONDS // 60} мин.",
        reply_markup=main_menu_kb(),
    )
    await callback.answer("Добавлено")


async def on_delete(callback: CallbackQuery):
    name = callback.data.split("|", 1)[1]
    chat_id = callback.message.chat.id
    tracked[:] = [t for t in tracked if not (t.chat_id == chat_id and t.name == name)]
    mine = [t for t in tracked if t.chat_id == chat_id]
    if mine:
        await callback.message.edit_text(
            "Твои слежки (нажми, чтобы убрать):", reply_markup=my_watches_kb(chat_id)
        )
    else:
        await callback.message.edit_text("Слежек не осталось.", reply_markup=main_menu_kb())
    await callback.answer(f"«{name}» убран")


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Укажи BOT_TOKEN в переменных окружения")
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
    dp.callback_query.register(on_delete, F.data.startswith("del|"))
    # Общий текстовый хендлер — сработает только если чат ждёт название (см. awaiting_custom_name)
    dp.message.register(on_custom_name_text, F.text)

    asyncio.create_task(poll_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
