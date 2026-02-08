import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from predict_api import OrderBook, Outcome, Position, PredictAPI

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PREDICT_API_KEY = os.environ["PREDICT_API_KEY"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
WALLET_POLL_INTERVAL = int(os.getenv("WALLET_POLL_INTERVAL", "30"))
PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
PREDICT_ACCOUNT = os.getenv("PREDICT_ACCOUNT", "")


def _parse_proxy() -> str | None:
    raw = os.getenv("PROXY", "").strip()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("socks"):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return f"http://{raw}"


PROXY_URL = _parse_proxy()

# Regex to extract slug from predict.fun URL
SLUG_RE = re.compile(r"predict\.fun/market/([A-Za-z0-9_-]+)")


@dataclass
class Subscription:
    chat_id: int
    message_id: int | None
    slug: str
    category_title: str
    outcome: Outcome
    initial_price: float | None = None
    last_notified_price: float | None = None


@dataclass
class WalletWatch:
    chat_id: int
    address: str
    label: str = ""
    known_position_uids: set[str] = field(default_factory=set)

    @property
    def display_name(self) -> str:
        short = f"{self.address[:6]}...{self.address[-4:]}"
        return f"{self.label} ({short})" if self.label else short


# Active subscriptions: key = (chat_id, market_id, outcome_name)
subscriptions: dict[tuple[int, int, str], Subscription] = {}

# Wallet watches: key = (chat_id, address)
wallet_watches: dict[tuple[int, str], WalletWatch] = {}

# Pending outcome selections: key = chat_id, value = (slug, title, outcomes)
pending_selections: dict[int, tuple[str, str, list[Outcome]]] = {}

api: PredictAPI | None = None
engine = None  # FarmingEngine | None — initialized in post_init if PRIVATE_KEY is set

# Pending farming flow state: chat_id -> {slug, title, outcomes, cat_data}
pending_farm: dict[int, dict] = {}
# Pending shares input: chat_id -> config dict
pending_farm_shares: dict[int, dict] = {}
# Pending depth input: chat_id -> config dict (after shares chosen)
pending_farm_depth: dict[int, dict] = {}
# Pending stop_at input: chat_id -> config dict (after depth chosen)
pending_farm_stop: dict[int, dict] = {}
# Pending notify input: chat_id -> config dict (after stop chosen)
pending_farm_notify: dict[int, dict] = {}
# Telegram app reference for sending notifications from engine callback
_tg_app: Application | None = None


def parse_slug(url: str) -> str | None:
    m = SLUG_RE.search(url)
    return m.group(1) if m else None


def _fmt_remaining(stop_at: datetime) -> str:
    """Format time remaining until stop_at as human-readable string."""
    delta = stop_at - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return "завершено"
    total_sec = int(delta.total_seconds())
    hours, remainder = divmod(total_sec, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}г {minutes}хв"
    return f"{minutes}хв"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🔮 *Predict.fun* — Трекер стакану та позицій\n\n"
        "📋 *Команди:*\n"
        "📊 /look <URL> — підписатись на оновлення стакану\n"
        "📑 /subs — список активних підписок\n\n"
        "👁 /watch <адреса> [назва] — стежити за позиціями гаманця\n"
        "💼 /wallets — список гаманців під стеженням\n"
    )
    if engine is not None:
        text += (
            "\n🤖 *Фармінг:*\n"
            "🚀 /farm <URL> — створити фармінг-сесію\n"
            "📋 /sessions — активні фармінг-сесії\n"
            "🛑 /stop <id> — зупинити сесію\n"
            "💰 /balance — баланс USDT\n"
        )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_look(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("ℹ️ Використання: /look <посилання на подію predict.fun>")
        return

    url = context.args[0]
    slug = parse_slug(url)
    if not slug:
        await update.message.reply_text(
            "❌ Невірне посилання. Очікуваний формат: https://predict.fun/market/<slug>"
        )
        return

    await update.message.reply_text(f"⏳ Завантажую подію: {slug} ...")

    try:
        title, outcomes, _ = await api.get_outcomes_from_slug(slug)
    except Exception as e:
        logger.error("Failed to fetch category %s: %s", slug, e)
        await update.message.reply_text(f"❌ Помилка завантаження події: {e}")
        return

    if not outcomes:
        await update.message.reply_text("🤷 Не знайдено outcomes для цієї події.")
        return

    pending_selections[update.effective_chat.id] = (slug, title, outcomes)

    buttons = []
    for i, o in enumerate(outcomes):
        buttons.append(
            [InlineKeyboardButton(f"🎯 {o.name}", callback_data=f"select_outcome:{i}")]
        )

    await update.message.reply_text(
        f"📊 *{title}*\n\nОберіть outcome:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def cmd_subs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_subs = {k: v for k, v in subscriptions.items() if k[0] == chat_id}

    if not user_subs:
        await update.message.reply_text("📭 Немає активних підписок.")
        return

    lines = []
    buttons = []
    for i, ((_, market_id, outcome_name), sub) in enumerate(user_subs.items()):
        price_str = f"{sub.last_notified_price}" if sub.last_notified_price is not None else "—"
        lines.append(f"{i + 1}. 📊 {sub.category_title} → {outcome_name} (бід: {price_str})")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"❌ Відписатись: {outcome_name}",
                    callback_data=f"unsub:{market_id}:{outcome_name}",
                )
            ]
        )

    await update.message.reply_text(
        "📑 *Активні підписки:*\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "ℹ️ Використання: /watch <адреса гаманця> [назва]\n"
            "Приклад: /watch 0x77F3...aEE4 mywallet"
        )
        return

    address = context.args[0].strip()
    label = " ".join(context.args[1:]).strip() if len(context.args) > 1 else ""
    chat_id = update.effective_chat.id
    key = (chat_id, address)

    if key in wallet_watches:
        await update.message.reply_text("⚠️ Ви вже стежите за цим гаманцем.")
        return

    await update.message.reply_text(f"⏳ Завантажую поточні позиції: `{address}` ...", parse_mode="Markdown")

    try:
        positions = await api.get_positions_by_address(address)
    except Exception as e:
        logger.error("Failed to fetch positions for %s: %s", address, e)
        await update.message.reply_text(f"❌ Помилка завантаження позицій: {e}")
        return

    known_uids = {p.uid for p in positions}
    w = WalletWatch(
        chat_id=chat_id,
        address=address,
        label=label,
        known_position_uids=known_uids,
    )
    wallet_watches[key] = w

    unwatch_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚫 Припинити стеження", callback_data=f"unwatch:{address}")]]
    )

    if positions:
        lines = []
        for p in positions:
            lines.append(
                f"  📌 {p.market_title} → {p.outcome_name}\n"
                f"    🎲 {p.size:.2f} шейрсів | 💵 {p.avg_price:.4f} | ${p.value_usd:.2f}"
            )
        pos_text = "\n".join(lines)
    else:
        pos_text = "  Позицій поки немає."

    await update.message.reply_text(
        f"👁 Стеження за гаманцем *{w.display_name}* увімкнено\n\n"
        f"📊 Поточні позиції ({len(positions)}):\n{pos_text}\n\n"
        f"🔔 Ви отримаєте сповіщення при появі нових позицій.",
        reply_markup=unwatch_btn,
        parse_mode="Markdown",
    )


async def cmd_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_watches = {k: v for k, v in wallet_watches.items() if k[0] == chat_id}

    if not user_watches:
        await update.message.reply_text("📭 Немає гаманців під стеженням.")
        return

    lines = []
    buttons = []
    for i, ((_, addr), w) in enumerate(user_watches.items()):
        lines.append(f"{i + 1}. 👁 *{w.display_name}* — {len(w.known_position_uids)} позицій")
        buttons.append(
            [InlineKeyboardButton(f"🚫 Припинити: {w.display_name}", callback_data=f"unwatch:{addr}")]
        )

    await update.message.reply_text(
        "💼 *Гаманці під стеженням:*\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# --------------- Farming commands ---------------

async def cmd_farm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if engine is None:
        await update.message.reply_text("⚠️ Фармінг не налаштовано. Додайте WALLET_PRIVATE_KEY в .env")
        return
    if not context.args:
        await update.message.reply_text("ℹ️ Використання: /farm <посилання на подію predict.fun>")
        return

    url = context.args[0]
    slug = parse_slug(url)
    if not slug:
        await update.message.reply_text("❌ Невірне посилання.")
        return

    await update.message.reply_text(f"⏳ Завантажую подію: {slug} ...")

    try:
        title, outcomes, cat_data = await api.get_outcomes_from_slug(slug)
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")
        return

    if not outcomes:
        await update.message.reply_text("🤷 Не знайдено outcomes.")
        return

    markets = cat_data.get("markets", [])
    pending_farm[update.effective_chat.id] = {
        "slug": slug, "title": title, "outcomes": outcomes,
        "cat_data": cat_data, "markets": markets,
    }

    buttons = []
    for i, o in enumerate(outcomes):
        buttons.append([InlineKeyboardButton(f"🎯 {o.name}", callback_data=f"farm_outcome:{i}")])

    await update.message.reply_text(
        f"🚀 *{title}*\n\nОберіть outcome для фармінгу:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if engine is None:
        await update.message.reply_text("⚠️ Фармінг не налаштовано.")
        return

    if not engine.sessions:
        await update.message.reply_text("📭 Немає активних фармінг-сесій.")
        return

    lines = []
    buttons = []
    for s in engine.sessions.values():
        if s.active and not s.is_expired:
            status = "🟢 Активна"
        elif s.is_expired:
            status = "🔴 Дедлайн"
        else:
            status = "⏸ Зупинено"
        price_str = f"{s.current_order_price_cents}ц" if s.current_order_price_cents else "—"
        bid_str = f"{s.last_top_bid}" if s.last_top_bid else "—"
        ask_str = f"{s.last_top_ask}" if s.last_top_ask else "—"
        err_str = f"\n  ❗ {s.error}" if s.error else ""
        shares_str = "MAX" if s.shares_wei == 0 else f"{s.shares_wei / (10**18):.0f}"

        # Time remaining
        if s.stop_at:
            time_str = f"⏱ Залишилось: *{_fmt_remaining(s.stop_at)}*"
        else:
            time_str = "⏱ Без обмеження часу"

        lines.append(
            f"🏷 `{s.session_id}` | *{s.outcome_name}*\n"
            f"  {status} | 📈 Ордер: {price_str} | Бід/Аск: {bid_str}/{ask_str}\n"
            f"  🎲 Шейрсів: {shares_str} | 📏 Глибина: {s.depth_cents}ц\n"
            f"  {time_str}{err_str}"
        )
        if s.active:
            buttons.append([InlineKeyboardButton(
                f"🛑 Зупинити {s.session_id}", callback_data=f"farm_stop:{s.session_id}"
            )])

    text = "📋 *Фармінг-сесії:*\n\n" + "\n\n".join(lines)
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        parse_mode="Markdown",
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if engine is None:
        await update.message.reply_text("⚠️ Фармінг не налаштовано.")
        return
    if not context.args:
        await update.message.reply_text("ℹ️ Використання: /stop <session\\_id>")
        return

    sid = context.args[0]
    if sid not in engine.sessions:
        await update.message.reply_text(f"🤷 Сесію `{sid}` не знайдено.", parse_mode="Markdown")
        return

    await engine.cancel_all_for_session(sid)
    engine.remove_session(sid)
    await update.message.reply_text(f"🛑 Сесію `{sid}` зупинено, ордер скасовано.", parse_mode="Markdown")


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if engine is None:
        await update.message.reply_text("⚠️ Фармінг не налаштовано.")
        return
    try:
        balance = await engine.get_balance_usdt()
        await update.message.reply_text(f"💰 Баланс: *{balance:.2f} USDT*", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка отримання балансу: {e}")


async def _handle_farm_outcome(query) -> None:
    """User selected an outcome for farming — show orderbook + balance + share options."""
    chat_id = query.message.chat_id
    idx = int(query.data.split(":")[1])

    farm_data = pending_farm.pop(chat_id, None)
    if not farm_data:
        await query.edit_message_text("⏰ Сесія закінчилась. Виконайте /farm ще раз.")
        return

    outcomes = farm_data["outcomes"]
    if idx >= len(outcomes):
        await query.edit_message_text("❌ Невірний вибір.")
        return

    outcome = outcomes[idx]
    cat_data = farm_data["cat_data"]
    markets = farm_data["markets"]
    market = next((mk for mk in markets if mk["id"] == outcome.market_id), {})

    # Fetch orderbook (invert for secondary outcome in binary markets)
    try:
        ob = await api.get_orderbook(outcome.market_id, invert=outcome.invert_book)
    except Exception as e:
        await query.edit_message_text(f"❌ Помилка завантаження стакану: {e}")
        return

    top_bid_cents = round(ob.top_bid_price * 100) if ob.top_bid_price else 0
    top_ask_cents = round(ob.top_ask_price * 100) if ob.top_ask_price else 0
    target_cents = top_bid_cents - 1  # default depth=1

    # Fetch balance
    balance = 0.0
    max_shares = 0
    try:
        balance = await engine.get_balance_usdt()
        if target_cents > 0:
            max_shares = int(balance / (target_cents / 100))
    except Exception:
        pass

    # Store config for next step
    config = {
        "title": farm_data["title"],
        "outcome": outcome,
        "cat_data": cat_data,
        "market": market,
        "top_bid_cents": top_bid_cents,
        "top_ask_cents": top_ask_cents,
        "target_cents": target_cents,
        "balance": balance,
        "max_shares": max_shares,
    }
    pending_farm_shares[chat_id] = config

    buttons = [
        [InlineKeyboardButton(f"🔥 MAX ({max_shares} шейрсів)", callback_data="farm_max")],
        [InlineKeyboardButton("✏️ Ввести к-сть вручну", callback_data="farm_manual")],
    ]

    await query.edit_message_text(
        f"🎯 *{farm_data['title']}* → *{outcome.name}*\n\n"
        f"📊 Топ бід: *{top_bid_cents}ц* | Аск: *{top_ask_cents}ц*\n"
        f"📍 Наш ордер буде: *{target_cents}ц* (глибина 1ц)\n\n"
        f"💰 Баланс: *{balance:.2f} USDT*\n"
        f"🎲 Макс шейрсів за {target_cents}ц: *{max_shares}*",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


def _show_depth_buttons(config: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Build message + buttons for depth selection step."""
    top_bid = config["top_bid_cents"]
    text = (
        f"🎯 *{config['title']}* → *{config['outcome'].name}*\n"
        f"📊 Топ бід: *{top_bid}ц*\n\n"
        f"📏 Оберіть глибину (центів від топ біду):"
    )
    buttons = [
        [
            InlineKeyboardButton("1ц", callback_data="farm_depth:1"),
            InlineKeyboardButton("2ц", callback_data="farm_depth:2"),
            InlineKeyboardButton("3ц", callback_data="farm_depth:3"),
            InlineKeyboardButton("5ц", callback_data="farm_depth:5"),
        ],
        [InlineKeyboardButton("✏️ Ввести вручну", callback_data="farm_depth:manual")],
    ]
    return text, InlineKeyboardMarkup(buttons)


def _show_stop_buttons(config: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Build message + buttons for stop-at selection step."""
    text = (
        f"🎯 *{config['title']}* → *{config['outcome'].name}*\n"
        f"🎲 Шейрсів: *{config['shares_str']}* | 📏 Глибина: *{config['depth_cents']}ц*\n\n"
        f"⏱ Встановити час зупинки (UTC)?\n"
        f"Формат: `YYYY-MM-DD HH:MM`"
    )
    buttons = [
        [InlineKeyboardButton("♾ Без обмеження часу", callback_data="farm_nostop")],
        [InlineKeyboardButton("⏱ Ввести час зупинки", callback_data="farm_setstop")],
    ]
    return text, InlineKeyboardMarkup(buttons)


def _show_notify_buttons(config: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Build message + buttons for notification toggle step."""
    stop_str = config["stop_at"].strftime("%Y-%m-%d %H:%M UTC") if config.get("stop_at") else "без обмеження"
    text = (
        f"🎯 *{config['title']}* → *{config['outcome'].name}*\n"
        f"🎲 Шейрсів: *{config['shares_str']}* | 📏 Глибина: *{config['depth_cents']}ц*\n"
        f"⏱ Зупинка: *{stop_str}*\n\n"
        f"🔔 Надсилати сповіщення при перестановці ордерів?"
    )
    buttons = [
        [InlineKeyboardButton("🔔 Так, сповіщувати", callback_data="farm_notify:yes")],
        [InlineKeyboardButton("🔕 Ні, без сповіщень", callback_data="farm_notify:no")],
    ]
    return text, InlineKeyboardMarkup(buttons)


async def _advance_to_notify(chat_id: int, config: dict, query=None, message=None) -> None:
    """Move to notification toggle step."""
    pending_farm_notify[chat_id] = config
    text, markup = _show_notify_buttons(config)
    if query:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    elif message:
        await message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def _create_farm_session(chat_id: int, config: dict) -> str:
    """Create a FarmingSession from the config dict. Returns message text."""
    from predict_bot import FarmingSession, WEI

    outcome = config["outcome"]
    cat_data = config["cat_data"]
    market = config["market"]
    shares_wei = config.get("shares_wei", 0)
    depth_cents = config.get("depth_cents", 1)
    stop_at = config.get("stop_at")
    notify_moves = config.get("notify_moves", False)

    session = FarmingSession(
        session_id=str(uuid.uuid4())[:8],
        market_id=outcome.market_id,
        market_title=config["title"],
        token_id=outcome.on_chain_id,
        outcome_name=outcome.name,
        side=0,  # BUY
        shares_wei=shares_wei,
        max_spread_cents=3,
        depth_cents=depth_cents,
        stop_at=stop_at,
        is_neg_risk=cat_data.get("isNegRisk", False),
        is_yield_bearing=cat_data.get("isYieldBearing", False),
        fee_rate_bps=market.get("feeRateBps", 0),
        invert_book=outcome.invert_book,
        notify_moves=notify_moves,
        chat_id=chat_id,
    )

    engine.add_session(session)

    shares_str = config.get("shares_str", str(shares_wei))
    stop_str = stop_at.strftime("%Y-%m-%d %H:%M UTC") if stop_at else "без обмеження"
    notify_str = "🔔 увімкнено" if notify_moves else "🔕 вимкнено"
    return (
        f"✅ *Фармінг-сесію створено!*\n\n"
        f"🏷 ID: `{session.session_id}`\n"
        f"📊 Подія: *{config['title']}*\n"
        f"🎯 Outcome: *{outcome.name}*\n"
        f"🎲 Шейрсів: *{shares_str}*\n"
        f"📏 Глибина: *{depth_cents}ц* | Спред: *3ц*\n"
        f"⏱ Зупинка: *{stop_str}*\n"
        f"📨 Сповіщення: *{notify_str}*\n\n"
        f"🤖 Бот почне працювати протягом кількох секунд.\n"
        f"📋 Перевірити: /sessions\n"
        f"🛑 Зупинити: /stop {session.session_id}"
    )


async def _advance_to_depth(chat_id: int, config: dict, query) -> None:
    """Move to depth selection step."""
    pending_farm_depth[chat_id] = config
    text, markup = _show_depth_buttons(config)
    await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")


async def _advance_to_stop(chat_id: int, config: dict, query=None, message=None) -> None:
    """Move to stop-at selection step."""
    pending_farm_stop[chat_id] = config
    text, markup = _show_stop_buttons(config)
    if query:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    elif message:
        await message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def _handle_farm_max(query) -> None:
    """User clicked MAX — proceed to depth step."""
    chat_id = query.message.chat_id
    config = pending_farm_shares.pop(chat_id, None)
    if not config:
        await query.edit_message_text("⏰ Сесія закінчилась. Виконайте /farm ще раз.")
        return

    config["shares_wei"] = 0
    config["shares_str"] = "🔥 MAX (весь баланс)"
    await _advance_to_depth(chat_id, config, query)


async def _handle_farm_manual(query) -> None:
    """User wants to enter shares manually."""
    chat_id = query.message.chat_id
    config = pending_farm_shares.get(chat_id)
    if not config:
        await query.edit_message_text("⏰ Сесія закінчилась. Виконайте /farm ще раз.")
        return

    await query.edit_message_text(
        f"✏️ Введіть кількість шейрсів (макс: {config['max_shares']}):",
    )


async def _handle_farm_depth(query) -> None:
    """User selected depth value."""
    chat_id = query.message.chat_id
    val = query.data.split(":")[1]

    config = pending_farm_depth.pop(chat_id, None)
    if not config:
        await query.edit_message_text("⏰ Сесія закінчилась. Виконайте /farm ще раз.")
        return

    if val == "manual":
        # Put back and wait for text input
        pending_farm_depth[chat_id] = config
        await query.edit_message_text("✏️ Введіть глибину (центів від топ біду), наприклад: 2")
        return

    config["depth_cents"] = int(val)
    await _advance_to_stop(chat_id, config, query=query)


async def _handle_farm_nostop(query) -> None:
    """User chose no stop time — advance to notify step."""
    chat_id = query.message.chat_id
    config = pending_farm_stop.pop(chat_id, None)
    if not config:
        await query.edit_message_text("⏰ Сесія закінчилась. Виконайте /farm ще раз.")
        return

    config["stop_at"] = None
    await _advance_to_notify(chat_id, config, query=query)


async def _handle_farm_setstop(query) -> None:
    """User wants to enter stop time manually."""
    chat_id = query.message.chat_id
    config = pending_farm_stop.get(chat_id)
    if not config:
        await query.edit_message_text("⏰ Сесія закінчилась. Виконайте /farm ще раз.")
        return

    await query.edit_message_text(
        "⏱ Введіть час зупинки (UTC).\nФормат: `YYYY-MM-DD HH:MM`\nНаприклад: `2026-02-08 15:30`",
        parse_mode="Markdown",
    )


async def _handle_farm_notify(query) -> None:
    """User chose notification preference."""
    chat_id = query.message.chat_id
    val = query.data.split(":")[1]

    config = pending_farm_notify.pop(chat_id, None)
    if not config:
        await query.edit_message_text("⏰ Сесія закінчилась. Виконайте /farm ще раз.")
        return

    config["notify_moves"] = val == "yes"
    text = await _create_farm_session(chat_id, config)
    await query.edit_message_text(text, parse_mode="Markdown")


async def _handle_farm_stop(query) -> None:
    """User clicked stop session button."""
    sid = query.data.split(":")[1]
    if sid not in engine.sessions:
        await query.edit_message_text(f"🤷 Сесію `{sid}` не знайдено.", parse_mode="Markdown")
        return

    await engine.cancel_all_for_session(sid)
    engine.remove_session(sid)
    await query.edit_message_text(
        f"🛑 Сесію `{sid}` зупинено, ордер скасовано.", parse_mode="Markdown"
    )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages — used for input during /farm flow steps."""
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # Step: shares input
    if chat_id in pending_farm_shares:
        config = pending_farm_shares.get(chat_id)
        try:
            shares = int(text)
            if shares <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("⚠️ Введіть ціле число більше 0.")
            return

        from predict_bot import WEI
        pending_farm_shares.pop(chat_id, None)
        config["shares_wei"] = shares * WEI
        config["shares_str"] = str(shares)
        # Advance to depth step
        pending_farm_depth[chat_id] = config
        msg_text, markup = _show_depth_buttons(config)
        await update.message.reply_text(msg_text, reply_markup=markup, parse_mode="Markdown")
        return

    # Step: depth input (manual)
    if chat_id in pending_farm_depth:
        config = pending_farm_depth.get(chat_id)
        try:
            depth = int(text)
            if depth <= 0 or depth > 50:
                raise ValueError
        except ValueError:
            await update.message.reply_text("⚠️ Введіть число від 1 до 50.")
            return

        pending_farm_depth.pop(chat_id, None)
        config["depth_cents"] = depth
        await _advance_to_stop(chat_id, config, message=update.message)
        return

    # Step: stop_at input
    if chat_id in pending_farm_stop:
        config = pending_farm_stop.get(chat_id)
        try:
            stop_at = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            if stop_at <= datetime.now(timezone.utc):
                await update.message.reply_text("⚠️ Час має бути у майбутньому. Спробуйте ще раз.")
                return
        except ValueError:
            await update.message.reply_text(
                "❌ Невірний формат. Введіть у форматі: `YYYY-MM-DD HH:MM`",
                parse_mode="Markdown",
            )
            return

        pending_farm_stop.pop(chat_id, None)
        config["stop_at"] = stop_at
        await _advance_to_notify(chat_id, config, message=update.message)
        return


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("select_outcome:"):
        await _handle_select_outcome(query)
    elif data.startswith("unsub:"):
        await _handle_unsub(query)
    elif data.startswith("unwatch:"):
        await _handle_unwatch(query)
    elif data.startswith("farm_outcome:"):
        await _handle_farm_outcome(query)
    elif data == "farm_max":
        await _handle_farm_max(query)
    elif data == "farm_manual":
        await _handle_farm_manual(query)
    elif data.startswith("farm_depth:"):
        await _handle_farm_depth(query)
    elif data == "farm_nostop":
        await _handle_farm_nostop(query)
    elif data == "farm_setstop":
        await _handle_farm_setstop(query)
    elif data.startswith("farm_notify:"):
        await _handle_farm_notify(query)
    elif data.startswith("farm_stop:"):
        await _handle_farm_stop(query)


async def _handle_select_outcome(query) -> None:
    chat_id = query.message.chat_id
    idx = int(query.data.split(":")[1])

    sel = pending_selections.pop(chat_id, None)
    if not sel:
        await query.edit_message_text("⏰ Сесія закінчилась. Виконайте /look ще раз.")
        return

    slug, title, outcomes = sel
    if idx >= len(outcomes):
        await query.edit_message_text("❌ Невірний вибір.")
        return

    outcome = outcomes[idx]
    key = (chat_id, outcome.market_id, outcome.name)

    if key in subscriptions:
        await query.edit_message_text(
            f"⚠️ Ви вже підписані на {outcome.name} для цієї події."
        )
        return

    # Fetch initial orderbook (invert for secondary outcome in binary markets)
    try:
        ob = await api.get_orderbook(outcome.market_id, invert=outcome.invert_book)
    except Exception as e:
        logger.error("Failed to fetch orderbook for market %s: %s", outcome.market_id, e)
        await query.edit_message_text(f"❌ Помилка завантаження стакану: {e}")
        return

    initial_price = ob.top_bid_price

    sub = Subscription(
        chat_id=chat_id,
        message_id=query.message.message_id,
        slug=slug,
        category_title=title,
        outcome=outcome,
        initial_price=initial_price,
        last_notified_price=initial_price,
    )
    subscriptions[key] = sub

    price_str = f"{initial_price}" if initial_price is not None else "немає ставок"
    unsub_button = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Відписатись", callback_data=f"unsub:{outcome.market_id}:{outcome.name}")]]
    )

    await query.edit_message_text(
        f"✅ Підписка на оновлення стакану оформлена\n\n"
        f"📊 Подія: *{title}*\n"
        f"🎯 Outcome: *{outcome.name}*\n"
        f"💵 Початкова найкраща ставка: *{price_str}*\n\n"
        f"🔔 Ви отримаєте сповіщення при зміні ціни.",
        reply_markup=unsub_button,
        parse_mode="Markdown",
    )


async def _handle_unsub(query) -> None:
    chat_id = query.message.chat_id
    parts = query.data.split(":", 2)
    market_id = int(parts[1])
    outcome_name = parts[2]

    key = (chat_id, market_id, outcome_name)
    sub = subscriptions.pop(key, None)

    if sub:
        await query.edit_message_text(
            f"🚫 Відписано від {sub.category_title} → {outcome_name}."
        )
    else:
        await query.edit_message_text("🤷 Підписку не знайдено (вже видалена).")


async def _handle_unwatch(query) -> None:
    chat_id = query.message.chat_id
    address = query.data.split(":", 1)[1]

    key = (chat_id, address)
    w = wallet_watches.pop(key, None)

    if w:
        await query.edit_message_text(f"🚫 Стеження за гаманцем {w.display_name} припинено.")
    else:
        short = f"{address[:6]}...{address[-4:]}"
        await query.edit_message_text(f"🤷 Гаманець {short} не знайдено (вже видалено).")


async def poll_orderbooks(app: Application) -> None:
    """Background task that polls orderbooks for all active subscriptions."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)

        if not subscriptions:
            continue

        # Snapshot keys to avoid mutation during iteration
        keys = list(subscriptions.keys())
        for key in keys:
            sub = subscriptions.get(key)
            if sub is None:
                continue

            try:
                ob = await api.get_orderbook(sub.outcome.market_id, invert=sub.outcome.invert_book)
            except Exception as e:
                logger.warning("Orderbook poll failed for market %s: %s", sub.outcome.market_id, e)
                continue

            current_price = ob.top_bid_price

            if current_price is None:
                continue

            if sub.last_notified_price is None or current_price != sub.last_notified_price:
                old_price = sub.last_notified_price
                sub.last_notified_price = current_price

                if old_price is not None:
                    diff = current_price - old_price
                    direction = "+" if diff > 0 else ""
                    change_str = f"({direction}{diff:.4f})"
                    arrow = "📈" if diff > 0 else ("📉" if diff < 0 else "📊")
                else:
                    change_str = "(перше значення)"
                    arrow = "🔔"

                initial_str = f"{sub.initial_price}" if sub.initial_price is not None else "—"

                unsub_button = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "❌ Відписатись",
                                callback_data=f"unsub:{sub.outcome.market_id}:{sub.outcome.name}",
                            )
                        ]
                    ]
                )

                try:
                    await app.bot.send_message(
                        chat_id=sub.chat_id,
                        text=(
                            f"{arrow} *{sub.category_title}*\n"
                            f"🎯 Outcome: *{sub.outcome.name}*\n\n"
                            f"💵 Найкраща ставка: *{current_price}* {change_str}\n"
                            f"📌 Початкова: {initial_str}"
                        ),
                        reply_markup=unsub_button,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error("Failed to send notification to chat %s: %s", sub.chat_id, e)


async def poll_wallets(app: Application) -> None:
    """Background task that polls positions for all watched wallets."""
    while True:
        await asyncio.sleep(WALLET_POLL_INTERVAL)

        if not wallet_watches:
            continue

        keys = list(wallet_watches.keys())
        for key in keys:
            w = wallet_watches.get(key)
            if w is None:
                continue

            try:
                positions = await api.get_positions_by_address(w.address)
            except Exception as e:
                logger.warning("Wallet poll failed for %s: %s", w.address, e)
                continue

            current_uids = {p.uid for p in positions}
            new_uids = current_uids - w.known_position_uids

            if not new_uids:
                continue

            w.known_position_uids = current_uids
            new_positions = [p for p in positions if p.uid in new_uids]

            unwatch_btn = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🚫 Припинити стеження", callback_data=f"unwatch:{w.address}")]]
            )

            for p in new_positions:
                try:
                    await app.bot.send_message(
                        chat_id=w.chat_id,
                        text=(
                            f"🆕 Нова позиція — *{w.display_name}*\n\n"
                            f"📊 Подія: *{p.market_title}*\n"
                            f"🎯 Outcome: *{p.outcome_name}*\n"
                            f"🎲 Шейрсів: *{p.size:.2f}*\n"
                            f"💵 Ціна: *{p.avg_price:.4f}*\n"
                            f"💰 Вартість: *${p.value_usd:.2f}*"
                        ),
                        reply_markup=unwatch_btn,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error("Failed to send wallet notification to chat %s: %s", w.chat_id, e)


async def _on_order_move(session, old_price_cents, new_price_cents, shares) -> None:
    """Callback from FarmingEngine when an order is moved."""
    if not _tg_app or not session.chat_id:
        return
    if old_price_cents is not None:
        arrow = "📈" if new_price_cents > old_price_cents else "📉"
        old_str = f"{old_price_cents}ц"
    else:
        arrow = "🆕"
        old_str = "—"
    try:
        await _tg_app.bot.send_message(
            chat_id=session.chat_id,
            text=(
                f"{arrow} *Перестановка ордеру*\n\n"
                f"🏷 `{session.session_id}` | *{session.outcome_name}*\n"
                f"💵 Було: *{old_str}* → Стало: *{new_price_cents}ц*\n"
                f"🎲 Шейрсів: *{shares}*\n"
                f"📊 Бід/Аск: {session.last_top_bid}/{session.last_top_ask}"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning("Failed to send order move notification: %s", e)


async def post_init(app: Application) -> None:
    global api, engine, _tg_app
    _tg_app = app
    api = PredictAPI(PREDICT_API_KEY, proxy=PROXY_URL)
    asyncio.create_task(poll_orderbooks(app))
    asyncio.create_task(poll_wallets(app))

    # Initialize farming engine if private key is configured
    if PRIVATE_KEY:
        from predict_bot import FarmingEngine
        engine = FarmingEngine(
            api, PREDICT_API_KEY, PRIVATE_KEY,
            predict_account=PREDICT_ACCOUNT or None,
            on_order_move=_on_order_move,
        )
        try:
            await engine.authenticate()
            engine.start()
            logger.info("Farming engine initialized in Telegram bot")
        except Exception as e:
            logger.error("Failed to initialize farming engine: %s", e)
            engine = None


async def post_shutdown(app: Application) -> None:
    if api:
        await api.close()


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("look", cmd_look))
    app.add_handler(CommandHandler("subs", cmd_subs))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("wallets", cmd_wallets))
    app.add_handler(CommandHandler("farm", cmd_farm))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    logger.info("Bot started. Polling interval: %ds", POLL_INTERVAL)
    app.run_polling()


if __name__ == "__main__":
    main()
