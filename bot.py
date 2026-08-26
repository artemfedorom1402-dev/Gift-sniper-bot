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
- Ссылки в алертах ведут прямо в Tonnel-бот
  (t.me/tonnel_network_bot/gift?startapp=...) вместо t.me/nft/... —
  так сразу открывается карточка лота/аукциона с кнопкой покупки/ставки.
- Умный скоринг офферов (MEGA / HOT / GOOD): учитывает % скидки,
  экономию в TON, совпадение нескольких критериев и близость к полу.
- За скан шлётся только топ самых вкусных алертов (без спама).
- Красивые карточки алертов с трейтами, экономией и кнопкой «Купить».

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
import re
import statistics
import threading
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
TONNEL_AUTH_DATA = os.environ.get("TONNEL_AUTH_DATA", "")
# Пол в 5 сек — защита от опечатки вида POLL_INTERVAL_SECONDS=0, которая иначе
# превратила бы фоновый цикл в бесконечный busy-loop, долбящий Tonnel без пауз
# (риск мгновенного бана IP/прокси).
POLL_INTERVAL_SECONDS = max(5, int(os.environ.get("POLL_INTERVAL_SECONDS", "120")))
# Пол в 1 — SCAN_CONCURRENCY=0 создал бы asyncio.Semaphore(0), и ни одна
# слежка никогда не сканировалась бы (тихий дедлок без единой ошибки в логах).
SCAN_CONCURRENCY = max(1, int(os.environ.get("SCAN_CONCURRENCY", "3")))

# Прокси для запросов к Tonnel (обход блокировки Cloudflare на дата-центровых
# IP — например Railway). Нужен обычный HTTP(S) резидентский прокси.
#
# PROXY_URL понимает практически любой формат, который выдают провайдеры
# резидентских прокси, и сам приводит его к виду http://user:pass@host:port:
#   http://user:pass@host:port   — уже готовый вид, используется как есть
#   user:pass@host:port          — просто добавляем схему http://
#   host:port:user:pass          — частый формат у провайдеров резидентских
#                                   прокси (4 значения через двоеточие)
#   host:port                    — прокси без логина/пароля
#
# Можно указать НЕСКОЛЬКО прокси через запятую: PROXY_URL="прокси1,прокси2"
# — бот будет по очереди перебирать их между запросами (round-robin). Полезно,
# если провайдер выдал несколько портов/сессий: сканы разных коллекций и так
# уже идут параллельно (см. SCAN_CONCURRENCY), и им есть смысл не толкаться в
# одну и ту же сессию прокси.
#
# Если не задана — запросы идут напрямую (и почти наверняка будут падать с
# 403 на большинстве облачных хостингов, включая Railway).
def _normalize_proxy(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return raw
    if "://" in raw:
        return raw
    if "@" in raw:
        return f"http://{raw}"
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    # Ни "://", ни "@", ни 2/4 частей через ":" — формат не из тех, что мы
    # умеем разбирать осознанно. Раньше это молча попадало в тот же
    # `return f"http://{raw}"`, что могло тихо собрать заведомо битый URL
    # прокси (например, если в пароле встретился лишний ":"). Логируем
    # предупреждение (без самого содержимого — там может быть пароль),
    # чтобы было видно в логах Railway, что именно этот адрес стоит
    # перепроверить, если запросы через него не проходят.
    log.warning(
        f"PROXY_URL: не распознал формат одного из адресов "
        f"(частей через ':': {len(parts)}, без '@' и схемы) — использую "
        f"как есть; если запросы через него не проходят, проверь формат."
    )
    return f"http://{raw}"


_PROXY_URLS = [_normalize_proxy(p) for p in os.environ.get("PROXY_URL", "").split(",") if p.strip()]
_proxy_rotation = {"i": 0}
_proxy_rotation_lock = threading.Lock()


def next_tonnel_proxies() -> dict | None:
    """dict для параметра proxies=... в tonnelmp, либо None, если прокси не
    настроен. При нескольких адресах в PROXY_URL перебирает их по очереди.

    Вызывается из _call_tonnel_with_retries (основной event loop, ПЕРЕД
    тем как отправить синхронный вызов в поток через asyncio.to_thread) —
    поэтому инкремент защищён Lock'ом: несколько тоннель-запросов внутри
    одного scan_collection (см. asyncio.gather) могут запросить следующий
    адрес почти одновременно, и без Lock'а счётчик ротации мог бы терять
    обновления."""
    if not _PROXY_URLS:
        return None
    with _proxy_rotation_lock:
        i = _proxy_rotation["i"] % len(_PROXY_URLS)
        _proxy_rotation["i"] += 1
    proxy = _PROXY_URLS[i]
    return {"http": proxy, "https": proxy}


_PROXY_CREDS_RE = re.compile(r"(://[^:@/\s]+:)[^@/\s]+(@)")


def mask_proxy_creds(text: str) -> str:
    """Прячет пароль прокси, если он случайно попал в текст исключения
    (некоторые HTTP-клиенты подставляют полный proxy URL в текст ошибки).
    Без этого пароль от прокси мог бы улететь в логи Railway и в отчёт
    «🔍 Проверить», который бот присылает прямо в чат."""
    return _PROXY_CREDS_RE.sub(r"\1***\2", str(text))


# Сколько раз повторять запрос к Tonnel при сетевой/прокси-ошибке, и с какой
# задержкой между попытками (линейный backoff: delay*1, delay*2, ...).
# Полы (max(1, ...) / max(0.0, ...)) защищают от опечаток в переменных
# окружения: TONNEL_MAX_RETRIES=0 привёл бы к `raise None` (цикл попыток
# не выполнился бы ни разу), а отрицательная задержка уронила бы asyncio.sleep.
TONNEL_MAX_RETRIES = max(1, int(os.environ.get("TONNEL_MAX_RETRIES", "3")))
TONNEL_RETRY_DELAY_SECONDS = max(0.0, float(os.environ.get("TONNEL_RETRY_DELAY_SECONDS", "1.5")))

# Сколько запросов одновременно разрешаем пускать через ОДИН и тот же адрес
# прокси. По умолчанию 1 (последовательно) — потому что дешёвые
# резидентские прокси часто ограничивают ровно одно одновременное
# соединение на сессию/порт и отвечают на CONNECT ошибкой 503, если бот
# пытается пробить туннель несколькими запросами разом. А это ровно то, что
# делает scan_collection: лоты (price_asc + latest), история продаж и
# аукционы уходят ОДНИМ asyncio.gather, и если настроен всего один
# PROXY_URL, все 3-4 запроса раньше валились в тот же прокси одновременно.
# PROXY_MAX_CONCURRENT_PER_ADDR=0 создал бы asyncio.Semaphore(0), у которого
# никогда не освобождается ни один "пропуск" — все запросы к Tonnel зависли
# бы навсегда без единой ошибки в логах (тихий дедлок). Пол в 1 это исключает.
PROXY_MAX_CONCURRENT_PER_ADDR = max(1, int(os.environ.get("PROXY_MAX_CONCURRENT_PER_ADDR", "1")))

_proxy_semaphores: dict[str, asyncio.Semaphore] = {}


def _proxy_semaphore_for(proxy_key: str) -> asyncio.Semaphore:
    """asyncio.Semaphore нельзя создавать до появления event loop, поэтому
    создаём лениво и кешируем по адресу прокси. Вызывается только из
    event-loop-потока (см. _call_tonnel_with_retries), поэтому без Lock'а —
    в отличие от _proxy_rotation, который дёргают вспомогательные потоки
    asyncio.to_thread."""
    sem = _proxy_semaphores.get(proxy_key)
    if sem is None:
        sem = asyncio.Semaphore(PROXY_MAX_CONCURRENT_PER_ADDR)
        _proxy_semaphores[proxy_key] = sem
    return sem


_PROXY_ERROR_HINTS = (
    "connect tunnel failed", "tunnel failed", "response 503", "response 502",
    "response 429", "econnrefused", "timed out", "timeout",
)


def looks_like_proxy_error(text: str) -> bool:
    """Отличает сбой соединения (обычно — сторона прокси: его шлюз не смог
    поднять туннель до Tonnel, перегружен, кончилась сессия и т.п.) от
    ошибки разбора ответа/логики. Используется только для более понятной
    формулировки в отчётах — саму ошибку это никак не "чинит", но помогает
    не искать баг в коде бота там, где реальная причина — сбой самого
    прокси-провайдера."""
    low = str(text).lower()
    return any(h in low for h in _PROXY_ERROR_HINTS)


# Таймаут на один вызов tonnelmp (сек). Без него зависшее соединение
# (прокси принял TCP, но не отвечает) держало бы await вечно — сам скан
# коллекции завис бы навсегда, а вместе с ним и вся асинхронная цепочка
# (scan_lock коллекции остался бы захвачен, семафор прокси — тоже занят).
# ВАЖНО: это не убивает сам поток — Python не может принудительно прервать
# заблокированный синхронный вызов внутри asyncio.to_thread. Таймаут лишь
# перестаёт ЖДАТЬ такой вызов и даёт коду пойти дальше (ретрай/следующая
# коллекция); повисший поток в редких случаях может доработать в фоне сам
# по себе позже. Это осознанный компромисс: без таймаута зависание было бы
# гарантированно фатальным для всего цикла сканирования, с таймаутом —
# в худшем случае просто лишний поток доработает впустую.
TONNEL_REQUEST_TIMEOUT_SECONDS = max(1.0, float(os.environ.get("TONNEL_REQUEST_TIMEOUT_SECONDS", "25")))


async def _call_tonnel_with_retries(fn):
    """Вызывает синхронную tonnelmp-функцию `fn(proxies)` в отдельном
    потоке с несколькими попытками (TONNEL_MAX_RETRIES). На каждой попытке:
      1) берём следующий адрес из ротации PROXY_URL (next_tonnel_proxies) —
         если прокси несколько, повтор пойдёт уже через другой адрес;
      2) не даём двум одновременным запросам этого бота долбить ОДИН и тот
         же адрес прокси разом (см. PROXY_MAX_CONCURRENT_PER_ADDR) — иначе
         несколько запросов, ушедших через asyncio.gather в scan_collection,
         сами же провоцируют "CONNECT tunnel failed, response 503" на
         прокси с лимитом в одно одновременное соединение;
      3) ограничивает время ожидания одного вызова TONNEL_REQUEST_TIMEOUT_SECONDS
         (см. пояснение у константы выше).
    Пробрасывает последнее исключение, если все попытки исчерпаны."""
    last_exc: Exception | None = None
    for attempt in range(1, TONNEL_MAX_RETRIES + 1):
        proxies = next_tonnel_proxies()
        proxy_key = (proxies or {}).get("https") or (proxies or {}).get("http") or "__direct__"
        sem = _proxy_semaphore_for(proxy_key)
        async with sem:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(fn, proxies), timeout=TONNEL_REQUEST_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                last_exc = TimeoutError(
                    f"запрос завис дольше {TONNEL_REQUEST_TIMEOUT_SECONDS:.0f} сек без ответа (таймаут)"
                )
            except Exception as e:
                last_exc = e
        if attempt < TONNEL_MAX_RETRIES:
            await asyncio.sleep(TONNEL_RETRY_DELAY_SECONDS * attempt)
    raise last_exc


MIN_LISTINGS_FOR_STATS = 5
SALES_HISTORY_LIMIT = 50
AUCTIONS_LIMIT = 30
CONSECUTIVE_ERRORS_BEFORE_NOTIFY = 3
# Сколько самых вкусных алертов отправлять за один скан одной коллекции.
# Остальные (слабее по score) откладываются — не спамим чат десятками
# почти-одинаковых лотов, а сначала показываем самое жирное.
MAX_ALERTS_PER_SCAN = max(1, int(os.environ.get("MAX_ALERTS_PER_SCAN", "6")))
# Лимит already_alerted на одну слежку — чтобы set не раздувался бесконечно
# на долгоживущем процессе (Railway рестартит редко).
ALREADY_ALERTED_MAX = 2000

PRESET_COLLECTIONS = [
    "Vice Cream", "Chill Flame", "Mood Pack", "Liberty Figure",
    "Snake Box", "Big Year", "Faith Amulet", "Jolly Chimp",
    "Bow Tie", "Xmas Stocking", "Santa Hat", "Ice Cream",
    "Party Sparkler", "Clover Pin", "Money Pot", "Candy Cane",
    "Easter Egg", "Plush Pepe", "Durov's Cap", "Snoop Dogg",
    "Toy Bear", "Swiss Watch", "Lol Pop", "Desk Calendar",
]
THRESHOLD_OPTIONS = [5, 10, 15, 20, 25, 30]

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


def safe_truncate_html(s: str, max_len: int) -> str:
    """Обрезает уже HTML-экранированный текст (parse_mode=HTML), не разрывая
    сущность вроде &amp; или &lt; посередине. Обычная нарезка по индексу
    здесь опасна: если разрез попадёт между '&' и ';', Telegram ответит
    'can't parse entities' и всё сообщение целиком не уйдёт."""
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:]:
        cut = cut[:amp]
    return cut.rstrip() + "\n…(обрезано)"


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
        """Ссылка на страницу подарка в Tonnel-боте (там же можно купить).
        nft_id = gift_id из API."""
        if self.nft_id:
            return f"https://t.me/tonnel_network_bot/gift?startapp={self.nft_id}"
        if not self.gift_num:
            return None
        slug = self.name.replace(" ", "")
        return f"https://t.me/nft/{slug}-{self.gift_num}"


@dataclass
class AuctionItem:
    auction_id: str
    name: str
    current_price: float
    gift_id: str | None = None
    gift_num: str | None = None
    model: str | None = None
    backdrop: str | None = None
    symbol: str | None = None

    @property
    def trait_key(self):
        return (self.model, self.backdrop, self.symbol)

    @property
    def tg_link(self) -> str | None:
        """Ссылка на страницу подарка/аукциона в Tonnel-боте.
        Предпочитаем startapp с gift_id — так открывается карточка
        с текущим аукционом и кнопкой ставки. Если gift_id нет — fallback
        на официальную NFT-ссылку Telegram."""
        if self.gift_id:
            return f"https://t.me/tonnel_network_bot/gift?startapp={self.gift_id}"
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
    # Не даёт двум сканам ОДНОЙ И ТОЙ ЖЕ слежки выполняться одновременно —
    # без этого нажатие "🔍 Проверить" ровно в момент фонового скана той же
    # коллекции (poll_loop) могло бы привести к тому, что оба скана читают
    # already_alerted ДО того, как один из них успеет туда что-то добавить,
    # и один и тот же лот/аукцион уйдёт в чат алертом дважды.
    scan_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)


tracked: list[TrackedCollection] = []


def is_tracked(tc: TrackedCollection) -> bool:
    """True, если слежка всё ещё в списке (по identity, не по равенству
    полей). Нужно из-за того, что scan_collection получает объект tc и
    какое-то время работает с ним асинхронно (сетевые запросы, отправка
    сообщений) — а пользователь может успеть нажать "🗑" и удалить именно
    эту слежку через on_delete, пока её скан ещё не закончился. Без этой
    проверки бот прислал бы алерт или уведомление об ошибках по слежке,
    которую юзер только что убрал."""
    return any(t is tc for t in tracked)


# ---------------- Tonnel ----------------

async def tonnel_listings_by_sort(name: str, sort: str) -> tuple[list[Listing], str | None]:
    def _call(proxies):
        return tonnelmp.getGifts(gift_name=name, sort=sort, limit=30, asset="TON", proxies=proxies)
    try:
        raw = await _call_tonnel_with_retries(_call)
    except Exception as e:
        err = mask_proxy_creds(e)
        log.warning(f"getGifts('{name}', sort={sort}) не удался: {err}")
        return [], err

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


# getAuctions() в tonnelmp возвращает записи подарков с auction_id.
# Текущая ставка хранится не в current_bid/price, а в bidHistory: сортировки
# самого tonnelmp используют bidHistory.amount / bidHistory.timestamp.
# Для аукциона без ставок используем startingBid/starting_bid, если поле есть.
_AUCTION_ID_KEYS = ("auction_id", "auctionId", "auctionIdStr")
_AUCTION_START_PRICE_KEYS = (
    "startingBid", "starting_bid", "startBid", "start_bid",
    "current_bid", "currentBid", "highest_bid", "highestBid",
    "price", "bid",
)
_AUCTION_BID_KEYS = ("amount", "bid", "value", "price")


def _to_float(value):
    """Безопасно превращает значение цены/ставки в float."""
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _auction_current_price(item: dict):
    """Возвращает последнюю ставку из bidHistory либо стартовую цену.

    В актуальном tonnelmp getAuctions() сортировка highest_bid/latest_bid
    напрямую использует поле bidHistory.amount, поэтому именно история ставок
    является главным источником текущей цены. У некоторых ответов история может
    быть списком словарей, у некоторых — словарём/вложенным объектом; поддерживаем
    оба варианта, чтобы бот не ломался при небольшом изменении формы ответа.
    """
    history = item.get("bidHistory")
    if history is None:
        history = item.get("bid_history")

    candidates = []
    if isinstance(history, list):
        candidates = history
    elif isinstance(history, dict):
        # Иногда история может прийти как {"amount": ...}; также поддерживаем
        # вложенные списки под распространёнными ключами.
        if any(k in history for k in _AUCTION_BID_KEYS):
            candidates = [history]
        else:
            for k in ("history", "bids", "items", "data"):
                if isinstance(history.get(k), list):
                    candidates = history[k]
                    break

    # Последняя запись bidHistory — текущая ставка. Если timestamp есть,
    # дополнительно выбираем самую свежую запись, не полагаясь на порядок API.
    parsed = []
    for bid in candidates:
        if not isinstance(bid, dict):
            continue
        amount = None
        for key in _AUCTION_BID_KEYS:
            amount = _to_float(bid.get(key))
            if amount is not None:
                break
        if amount is None:
            continue
        parsed.append((bid.get("timestamp"), amount))

    if parsed:
        with_timestamp = [x for x in parsed if x[0] is not None]
        if with_timestamp:
            try:
                return max(with_timestamp, key=lambda x: str(x[0]))[1]
            except Exception:
                pass
        return parsed[-1][1]

    # Аукцион без ставок: берём стартовую цену, если API её отдаёт.
    for key in _AUCTION_START_PRICE_KEYS:
        price = _to_float(item.get(key))
        if price is not None:
            return price

    # На случай, если данные аукциона вложены в поле auction.
    auction = item.get("auction")
    if isinstance(auction, dict):
        for key in _AUCTION_START_PRICE_KEYS:
            price = _to_float(auction.get(key))
            if price is not None:
                return price
        nested_history = auction.get("bidHistory") or auction.get("bid_history")
        if isinstance(nested_history, list):
            for bid in reversed(nested_history):
                if isinstance(bid, dict):
                    for key in _AUCTION_BID_KEYS:
                        price = _to_float(bid.get(key))
                        if price is not None:
                            return price

    return None


def _auction_field(item: dict, *keys, default=None):
    """Берёт первое непустое поле из записи аукциона."""
    for key in keys:
        value = item.get(key)
        if value is not None and value != "":
            return value
    return default


async def tonnel_auctions(name: str) -> tuple[list[AuctionItem], str | None]:
    def _call(proxies):
        return tonnelmp.getAuctions(
            gift_name=name,
            sort="latest",
            limit=AUCTIONS_LIMIT,
            asset="TON",
            proxies=proxies,
        )

    try:
        raw = await _call_tonnel_with_retries(_call)
    except Exception as e:
        err = mask_proxy_creds(e)
        log.warning(f"getAuctions('{name}') не удался: {err}")
        return [], err

    if not isinstance(raw, list):
        msg = f"getAuctions('{name}') вернул неожиданный формат: {type(raw).__name__} — {raw!r}"[:300]
        log.warning(msg)
        return [], msg

    auctions: list[AuctionItem] = []
    unrecognized = 0

    for it in raw:
        if not isinstance(it, dict):
            unrecognized += 1
            continue

        auction_id = _auction_field(it, *_AUCTION_ID_KEYS)
        # Если API вложил auction_id внутрь auction — тоже поддерживаем.
        if auction_id is None and isinstance(it.get("auction"), dict):
            auction_id = _auction_field(it["auction"], *_AUCTION_ID_KEYS)

        price = _auction_current_price(it)

        if auction_id is None or price is None:
            unrecognized += 1
            # Логируем только ключи, а не весь объект: так лог Railway остаётся
            # компактным и не засоряется потенциально большими вложенными данными.
            log.warning(
                f"getAuctions('{name}'): не смог распознать id/цену; "
                f"ключи записи: {sorted(it.keys())}; "
                f"auction keys: {sorted(it['auction'].keys()) if isinstance(it.get('auction'), dict) else []}"
            )
            continue

        gift_num = _auction_field(it, "gift_num", "giftNum")
        gift_id = _auction_field(it, "gift_id", "giftId")
        gift_name = _auction_field(it, "name", "gift_name", "giftName", default=name)

        auctions.append(AuctionItem(
            auction_id=str(auction_id),
            gift_id=str(gift_id) if gift_id is not None else None,
            gift_num=str(gift_num) if gift_num is not None else None,
            name=str(gift_name),
            current_price=float(price),
            model=strip_rarity(_auction_field(it, "model")),
            backdrop=strip_rarity(_auction_field(it, "backdrop")),
            symbol=strip_rarity(_auction_field(it, "symbol")),
        ))

    error = None
    if raw and unrecognized == len(raw):
        error = "getAuctions() вернул данные, но не удалось распознать ни одного аукциона (см. логи)"
    elif unrecognized:
        log.info(
            f"getAuctions('{name}'): распознано {len(auctions)} из {len(raw)} записей; "
            f"пропущено {unrecognized}"
        )

    return auctions, error


async def tonnel_sales_history(name: str) -> tuple[list[SaleRecord], str | None]:
    # ОТКЛЮЧЕНО: authData для saleHistory() не документирован в tonnelmp и не работает.
    # Сравнение с историей продаж пропускается, но основной функционал
    # (сравнение с медианой и аукционы) остаётся работоспособным.
    return [], None


def build_last_sale_index(sales: list[SaleRecord]) -> dict:
    index: dict = {}
    for s in sales:
        if s.model is None and s.backdrop is None and s.symbol is None:
            continue
        current = index.get(s.trait_key)
        if current is None or s.timestamp > current.timestamp:
            index[s.trait_key] = s
    return index


@dataclass
class DealEval:
    """Результат оценки оффера: score выше = вкуснее, grade для заголовка."""
    score: float
    reasons: list[str]
    discount_pct: float
    savings_ton: float
    grade: str  # "MEGA" | "HOT" | "GOOD"
    ref_label: str  # с чем сравнивали для шапки
    ref_price: float


def _deal_grade(discount_pct: float, dual_hit: bool) -> str:
    if discount_pct >= 35 or (discount_pct >= 25 and dual_hit):
        return "MEGA"
    if discount_pct >= 20 or dual_hit:
        return "HOT"
    return "GOOD"


def evaluate_deal(
    price: float,
    median_price: float,
    floor_price: float,
    last_sale_index: dict,
    trait_key,
    threshold_pct: float,
) -> DealEval | None:
    """Оценивает оффер. None — не проходит порог пользователя.

    Score учитывает:
      • максимальный % скидки к медиане / последней продаже / полу
      • абсолютную экономию в TON
      • бонус, если сработали сразу два критерия
      • бонус, если цена близка к полу рынка
    """
    reasons: list[str] = []
    best_discount = 0.0
    best_savings = 0.0
    best_ref_label = ""
    best_ref_price = 0.0
    hits = 0

    if median_price > 0:
        d = (median_price - price) / median_price * 100
        if d >= threshold_pct:
            hits += 1
            reasons.append(
                f"дешевле медианы на <b>{d:.0f}%</b> "
                f"(медиана {median_price:.2f} TON)"
            )
            if d > best_discount:
                best_discount, best_savings = d, median_price - price
                best_ref_label, best_ref_price = "медианы", median_price

    match = last_sale_index.get(trait_key)
    if match and match.price_ton > 0:
        d = (match.price_ton - price) / match.price_ton * 100
        if d >= threshold_pct:
            hits += 1
            model, backdrop, symbol = trait_key
            fmt = lambda v: esc(v) if v else "—"  # noqa: E731
            reasons.append(
                f"дешевле последней продажи такого же трейта "
                f"({fmt(model)} / {fmt(backdrop)} / {fmt(symbol)}) на <b>{d:.0f}%</b> "
                f"(была {match.price_ton:.2f} TON)"
            )
            if d > best_discount:
                best_discount, best_savings = d, match.price_ton - price
                best_ref_label, best_ref_price = "посл. продажи", match.price_ton

    # Пол рынка — только бонус к уже прошедшему порог офферу.
    # Иначе самый дешёвый лот в выборке алертил бы при любом threshold.
    at_floor = floor_price > 0 and price <= floor_price * 1.001
    if at_floor and hits >= 1:
        reasons.append(f"цена на <b>полу рынка</b> ({floor_price:.2f} TON)")
        hits += 1

    if not reasons:
        return None

    dual = hits >= 2
    grade = _deal_grade(best_discount, dual)
    # Score: % скидки доминирует, плюс экономия и бонусы
    score = best_discount * 10.0 + best_savings * 3.0
    if dual:
        score += 25.0
    if at_floor:
        score += 15.0
    if grade == "MEGA":
        score += 40.0
    elif grade == "HOT":
        score += 15.0

    return DealEval(
        score=score,
        reasons=reasons,
        discount_pct=best_discount,
        savings_ton=max(0.0, best_savings),
        grade=grade,
        ref_label=best_ref_label or "рынка",
        ref_price=best_ref_price if best_ref_price > 0 else price,
    )


def _traits_line(model, backdrop, symbol) -> str:
    parts = []
    if model:
        parts.append(f"Model: <code>{esc(model)}</code>")
    if backdrop:
        parts.append(f"Backdrop: <code>{esc(backdrop)}</code>")
    if symbol:
        parts.append(f"Symbol: <code>{esc(symbol)}</code>")
    return " · ".join(parts) if parts else ""


def format_listing_alert(tc: TrackedCollection, listing: Listing, deal: DealEval) -> str:
    grade_emoji = {"MEGA": "🔥🔥🔥", "HOT": "🔥🔥", "GOOD": "✨"}.get(deal.grade, "✨")
    grade_title = {"MEGA": "MEGA DEAL", "HOT": "HOT DEAL", "GOOD": "GOOD DEAL"}.get(deal.grade, "DEAL")
    num = f" #{esc(listing.gift_num)}" if listing.gift_num else ""
    traits = _traits_line(listing.model, listing.backdrop, listing.symbol)
    reasons = "\n".join(f"  • {r}" for r in deal.reasons)
    savings = (
        f"\n💵 Экономия ≈ <b>{deal.savings_ton:.2f} TON</b> к {deal.ref_label}"
        if deal.savings_ton > 0.01
        else ""
    )
    return (
        f"{grade_emoji} <b>{grade_title}</b>\n\n"
        f"📦 <b>{esc(tc.name)}</b>{num}\n"
        f"💰 <b>{listing.price_ton:.2f} TON</b>"
        f"  <i>(−{deal.discount_pct:.0f}% от {deal.ref_label} {deal.ref_price:.2f})</i>"
        f"{savings}\n"
        + (f"🏷 {traits}\n" if traits else "")
        + f"\nПочему вкусно:\n{reasons}"
    )


def format_auction_alert(tc: TrackedCollection, auction: AuctionItem, deal: DealEval) -> str:
    grade_emoji = {"MEGA": "🔥🔥🔥", "HOT": "🔥🔥", "GOOD": "✨"}.get(deal.grade, "✨")
    grade_title = {"MEGA": "MEGA AUCTION", "HOT": "HOT AUCTION", "GOOD": "AUCTION"}.get(
        deal.grade, "AUCTION"
    )
    num = f" #{esc(auction.gift_num)}" if auction.gift_num else ""
    traits = _traits_line(auction.model, auction.backdrop, auction.symbol)
    reasons = "\n".join(f"  • {r}" for r in deal.reasons)
    savings = (
        f"\n💵 Экономия ≈ <b>{deal.savings_ton:.2f} TON</b> к {deal.ref_label}"
        if deal.savings_ton > 0.01
        else ""
    )
    return (
        f"{grade_emoji} <b>{grade_title}</b>\n\n"
        f"📦 <b>{esc(tc.name)}</b>{num}\n"
        f"🔨 Ставка: <b>{auction.current_price:.2f} TON</b>"
        f"  <i>(−{deal.discount_pct:.0f}% от {deal.ref_label} {deal.ref_price:.2f})</i>"
        f"{savings}\n"
        + (f"🏷 {traits}\n" if traits else "")
        + f"\nПочему вкусно:\n{reasons}\n\n"
        f"⚠️ Аукцион — ставка может вырасти, пока ты открываешь."
    )


def _prune_already_alerted(tc: TrackedCollection) -> None:
    if len(tc.already_alerted) > ALREADY_ALERTED_MAX:
        # set не упорядочен — просто очищаем половину «старых» через пересоздание
        # (точные id нам уже не критичны: максимум повторный алерт через долгое время)
        keep = list(tc.already_alerted)[-ALREADY_ALERTED_MAX // 2 :]
        tc.already_alerted = set(keep)


async def scan_collection(bot: Bot, tc: TrackedCollection, manual: bool = False) -> dict:
    """Сканирует одну коллекцию (лоты + аукционы), рассылает алерты.
    Возвращает диагностику — используется и для ручной проверки, и для
    учёта повторяющихся ошибок в фоновом цикле.

    Тело целиком под tc.scan_lock: сетевые запросы (asyncio.gather ниже)
    всё ещё идут параллельно между собой, но сам скан ЭТОЙ коллекции не
    может пересекаться с другим сканом той же коллекции (ручным или
    фоновым) — см. пояснение у поля scan_lock в TrackedCollection."""
    async with tc.scan_lock:
        (listings, listings_err), (sales, sales_err), (auctions, auctions_err) = await asyncio.gather(
            tonnel_market_sample(tc.name),
            tonnel_sales_history(tc.name),
            tonnel_auctions(tc.name),
        )

        errors = [e for e in (listings_err, sales_err, auctions_err) if e]
        # all_failed = True если ВСЕ три запроса упали И нет данных
        all_failed = bool(listings_err) and bool(auctions_err) and not listings and not auctions

        if all_failed:
            tc.consecutive_errors += 1
        else:
            tc.consecutive_errors = 0
            tc.error_notified = False

        if tc.consecutive_errors >= CONSECUTIVE_ERRORS_BEFORE_NOTIFY and not tc.error_notified and not manual and is_tracked(tc):
            tc.error_notified = True
            last_error = esc(truncate_text(errors[0])) if errors else "неизвестна"
            proxy_hint = (
                "\nПохоже на сбой самого прокси (не бота) — проверь его в "
                "личном кабинете провайдера (сессия/трафик/лимит соединений)."
                if errors and any(looks_like_proxy_error(e) for e in errors) else ""
            )
            try:
                await bot.send_message(
                    tc.chat_id,
                    f"⚠️ «{esc(tc.name)}»: {tc.consecutive_errors} раз(а) подряд не получилось "
                    f"получить данные с Tonnel (возможно, 429/403 от их API или сменилось "
                    f"название коллекции). Последняя ошибка: {last_error}.{proxy_hint}\n"
                    f"Если ошибка про 403/CloudFlare — нужен прокси (переменная PROXY_URL), "
                    f"без него Tonnel блокирует IP облачного хостинга.\n"
                    f"Открой «Мои слежки» → «🔍 Проверить», чтобы посмотреть подробности.",
                )
            except Exception as e:
                # Не даём сбою отправки этого уведомления оборвать весь скан —
                # иначе ни один алерт по лотам/аукционам этой коллекции не
                # ушёл бы в этом цикле только из-за того, что не прошло ЭТО
                # конкретное сообщение (например, Telegram временно недоступен).
                log.warning(f"Не удалось отправить уведомление об ошибках «{tc.name}»: {e}")

        prices = [l.price_ton for l in listings]
        median_price = statistics.median(prices) if len(prices) >= MIN_LISTINGS_FOR_STATS else 0.0
        floor_price = min(prices) if prices else 0.0
        last_sale_index = build_last_sale_index(sales)

        # Собираем кандидатов, сортируем по score (самые вкусные первыми),
        # шлём только TOP MAX_ALERTS_PER_SCAN — без спама слабыми офферами.
        candidates: list[tuple[float, str, object, DealEval, bool]] = []
        # (score, alert_key, item, deal, is_auction)

        for l in listings:
            if l.nft_id in tc.already_alerted:
                continue
            if tc.max_price is not None and l.price_ton > tc.max_price:
                continue
            deal = evaluate_deal(
                l.price_ton, median_price, floor_price,
                last_sale_index, l.trait_key, tc.threshold_pct,
            )
            if deal:
                candidates.append((deal.score, l.nft_id, l, deal, False))

        for a in auctions:
            alert_key = f"auction:{a.auction_id}"
            if alert_key in tc.already_alerted:
                continue
            if tc.max_price is not None and a.current_price > tc.max_price:
                continue
            deal = evaluate_deal(
                a.current_price, median_price, floor_price,
                last_sale_index, a.trait_key, tc.threshold_pct,
            )
            if deal:
                candidates.append((deal.score, alert_key, a, deal, True))

        candidates.sort(key=lambda x: x[0], reverse=True)
        top = candidates[:MAX_ALERTS_PER_SCAN]
        alerts_sent = 0

        for _score, alert_key, item, deal, is_auction in top:
            if not is_tracked(tc):
                break
            try:
                if is_auction:
                    text = format_auction_alert(tc, item, deal)
                    kb = None
                    if item.tg_link:
                        kb_builder = InlineKeyboardBuilder()
                        kb_builder.button(text="🔨 Открыть аукцион", url=item.tg_link)
                        kb = kb_builder.as_markup()
                else:
                    text = format_listing_alert(tc, item, deal)
                    kb = None
                    if item.tg_link:
                        kb_builder = InlineKeyboardBuilder()
                        kb_builder.button(text="⚡️ Купить на Tonnel", url=item.tg_link)
                        kb = kb_builder.as_markup()
                await bot.send_message(tc.chat_id, text, reply_markup=kb)
                # Помечаем только после успешной отправки — иначе при сбое
                # Telegram оффер навсегда «пропадёт» из алертов.
                tc.already_alerted.add(alert_key)
                alerts_sent += 1
            except Exception as e:
                log.warning(
                    f"Не удалось отправить алерт ({'auction' if is_auction else 'lot'} "
                    f"{alert_key}, {tc.name}): {e}"
                )

        skipped = len(candidates) - len(top)
        if skipped > 0:
            log.info(
                f"«{tc.name}»: отправлено {alerts_sent} топ-офферов, "
                f"ещё {skipped} слабее по score отложены (лимит {MAX_ALERTS_PER_SCAN})"
            )

        _prune_already_alerted(tc)

        return {
            "listings": len(listings),
            "auctions": len(auctions),
            "sales": len(sales),
            "median": median_price,
            "floor": floor_price,
            "candidates": len(candidates),
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
        label = f"{pct}%"
        if pct <= 10:
            label = f"⚡️ {pct}%"
        elif pct >= 25:
            label = f"💎 {pct}%"
        b.button(text=label, callback_data=f"thr|{name}|{pct}")
    b.button(text="⬅️ Назад", callback_data="menu|add")
    b.adjust(3, 3, 1)
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

def safe_split_callback_data(data: str, max_parts: int, default_part: str = "") -> list[str]:
    """Безопасно разбирает callback_data, добавляя пустые строки если недостаточно частей.
    Предотвращает IndexError если данные повреждены."""
    parts = data.split("|", max_parts - 1)
    while len(parts) < max_parts:
        parts.append(default_part)
    return parts


def reset_pending_state(chat_id: int):
    awaiting_custom_name.discard(chat_id)
    awaiting_max_price.pop(chat_id, None)


def confirmation_text(name: str, threshold: float, max_price: float | None) -> str:
    limit_text = f"до {max_price:.2f} TON" if max_price is not None else "без лимита по цене"
    mins = max(1, POLL_INTERVAL_SECONDS // 60)
    return (
        f"✅ <b>Слежка активна</b>\n\n"
        f"📦 «{esc(name)}»\n"
        f"🎯 Алерт от <b>{threshold:.0f}%</b> дешевле рынка\n"
        f"💰 Лимит: {limit_text}\n"
        f"⏱ Проверка каждые ~{mins} мин\n\n"
        f"Буду присылать только самые вкусные офферы "
        f"(MEGA / HOT / GOOD) — сначала жирные, без спама."
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
        "Ловлю <b>вкусные</b> лоты и аукционы:\n"
        "• дешевле медианы коллекции\n"
        "• дешевле последней продажи того же трейта\n"
        "• на полу рынка\n\n"
        "Каждый оффер оценивается и помечается:\n"
        "🔥🔥🔥 <b>MEGA</b> · 🔥🔥 <b>HOT</b> · ✨ <b>GOOD</b>\n\n"
        "Сначала прилетают самые жирные — без спама.\n\n"
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
    parts = safe_split_callback_data(callback.data, 2)
    name = parts[1]
    if not name:
        await callback.answer("Ошибка в данных кнопки", show_alert=True)
        return
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
    parts = safe_split_callback_data(callback.data, 3)
    _, name, pct_str = parts[0], parts[1], parts[2]
    if not name or not pct_str:
        await callback.answer("Ошибка в данных кнопки", show_alert=True)
        return
    try:
        threshold = float(pct_str)
    except ValueError:
        await callback.answer("Ошибка: некорректный порог", show_alert=True)
        return
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
    parts = safe_split_callback_data(callback.data, 3)
    _, name, threshold_str = parts[0], parts[1], parts[2]
    if not name or not threshold_str:
        await callback.answer("Ошибка в данных кнопки", show_alert=True)
        return
    try:
        threshold = float(threshold_str)
    except ValueError:
        await callback.answer("Ошибка: некорректный порог", show_alert=True)
        return
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
    parts = safe_split_callback_data(callback.data, 2)
    name = parts[1]
    if not name:
        await callback.answer("Ошибка в данных кнопки", show_alert=True)
        return
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
    floor = stats.get("floor") or 0
    candidates = stats.get("candidates", stats.get("alerts_sent", 0))
    lines = [
        f"🔍 <b>Проверка «{esc(tc.name)}»</b>",
        f"Условие: от {tc.threshold_pct:.0f}% дешевле, {limit_label}",
        f"Лотов: {stats['listings']}"
        + (
            f" · медиана {stats['median']:.2f} · пол {floor:.2f} TON"
            if stats["median"] > 0
            else " (мало данных для медианы)"
        ),
        f"Аукционов: {stats['auctions']} · история продаж: {stats['sales']}",
        f"Вкусных кандидатов: {candidates}",
        f"Отправлено алертов: {stats['alerts_sent']}"
        + (f" (топ из {candidates})" if candidates > stats["alerts_sent"] else ""),
    ]
    if stats["alerts_sent"] == 0:
        lines.append(
            "Ничего вкусного сейчас — рынок выше порога, "
            "или эти лоты уже присылались раньше."
        )
    if stats["errors"]:
        lines.append("⚠️ Ошибки запросов к Tonnel:")
        lines.extend(f"— {esc(truncate_text(e))}" for e in stats["errors"])
        if any(looks_like_proxy_error(e) for e in stats["errors"]):
            lines.append(
                "↳ Похоже на сбой именно на стороне прокси (его шлюз не "
                "смог поднять туннель до Tonnel) — не баг в логике бота. "
                "Частые причины: у прокси-провайдера закончилась сессия/"
                "трафик, его шлюз сейчас перегружен, либо тариф прокси "
                "не даёт нескольких одновременных соединений (бот уже "
                "ограничивает это переменной PROXY_MAX_CONCURRENT_PER_ADDR "
                "и повторяет запрос несколько раз, но если ошибка не "
                "проходит — стоит проверить прокси в личном кабинете "
                "провайдера)."
            )

    report = "\n".join(lines)
    if len(report) > 3900:  # запас от лимита Telegram в 4096 символов
        report = safe_truncate_html(report, 3900)
    await callback.message.answer(report)


async def on_delete(callback: CallbackQuery):
    parts = safe_split_callback_data(callback.data, 2)
    name = parts[1]
    if not name:
        await callback.answer("Ошибка в данных кнопки", show_alert=True)
        return
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
    if not _PROXY_URLS:
        log.warning(
            "PROXY_URL не задан — запросы к Tonnel пойдут напрямую с IP этого "
            "сервера. На облачных хостингах (Railway и т.п.) Tonnel почти "
            "всегда отвечает 403 (Cloudflare) без прокси."
        )
    else:
        log.info(
            f"Прокси для Tonnel настроен: {len(_PROXY_URLS)} адрес(ов) (round-robin), "
            f"макс. {PROXY_MAX_CONCURRENT_PER_ADDR} одновременных запросов на адрес, "
            f"до {TONNEL_MAX_RETRIES} попыток на запрос"
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
    # on_check_now требует параметр bot - создаём async обработчик-обёртку
    async def check_handler(callback: CallbackQuery):
        await on_check_now(callback, bot)
    dp.callback_query.register(check_handler, F.data.startswith("chk|"))
    dp.callback_query.register(on_delete, F.data.startswith("del|"))
    # Общий текстовый хендлер — сработает только если чат ждёт название
    # коллекции или максимальную цену (см. awaiting_custom_name / awaiting_max_price)
    dp.message.register(on_text, F.text)
    dp.errors.register(on_unhandled_error)

    # Убедимся, что нет конфликтующего вебхука или другого экземпляра
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Вебхук удален (если был) — очищены ожидающие обновления")
    except Exception as e:
        log.warning(f"Не удалось удалить вебхук: {e}")
    
    asyncio.create_task(poll_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
