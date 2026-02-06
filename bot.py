import asyncio
import logging
import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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
    known_position_uids: set[str] = field(default_factory=set)


# Active subscriptions: key = (chat_id, market_id, outcome_name)
subscriptions: dict[tuple[int, int, str], Subscription] = {}

# Wallet watches: key = (chat_id, address)
wallet_watches: dict[tuple[int, str], WalletWatch] = {}

# Pending outcome selections: key = chat_id, value = (slug, title, outcomes)
pending_selections: dict[int, tuple[str, str, list[Outcome]]] = {}

api: PredictAPI | None = None


def parse_slug(url: str) -> str | None:
    m = SLUG_RE.search(url)
    return m.group(1) if m else None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Predict.fun — Трекер стакану та позицій\n\n"
        "Команди:\n"
        "/look <посилання на подію> — підписатись на оновлення стакану\n"
        "/subs — список активних підписок на стакан\n\n"
        "/watch <адреса гаманця> — стежити за позиціями гаманця\n"
        "/wallets — список гаманців під стеженням\n"
    )


async def cmd_look(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Використання: /look <посилання на подію predict.fun>")
        return

    url = context.args[0]
    slug = parse_slug(url)
    if not slug:
        await update.message.reply_text(
            "Невірне посилання. Очікуваний формат: https://predict.fun/market/<slug>"
        )
        return

    await update.message.reply_text(f"Завантажую подію: {slug} ...")

    try:
        title, outcomes = await api.get_outcomes_from_slug(slug)
    except Exception as e:
        logger.error("Failed to fetch category %s: %s", slug, e)
        await update.message.reply_text(f"Помилка завантаження події: {e}")
        return

    if not outcomes:
        await update.message.reply_text("Не знайдено outcomes для цієї події.")
        return

    pending_selections[update.effective_chat.id] = (slug, title, outcomes)

    buttons = []
    for i, o in enumerate(outcomes):
        buttons.append(
            [InlineKeyboardButton(o.name, callback_data=f"select_outcome:{i}")]
        )

    await update.message.reply_text(
        f"*{title}*\n\nДоступні outcomes:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def cmd_subs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_subs = {k: v for k, v in subscriptions.items() if k[0] == chat_id}

    if not user_subs:
        await update.message.reply_text("Немає активних підписок.")
        return

    lines = []
    buttons = []
    for i, ((_, market_id, outcome_name), sub) in enumerate(user_subs.items()):
        price_str = f"{sub.last_notified_price}" if sub.last_notified_price is not None else "—"
        lines.append(f"{i + 1}. {sub.category_title} → {outcome_name} (найкраща ставка: {price_str})")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"Відписатись: {outcome_name}",
                    callback_data=f"unsub:{market_id}:{outcome_name}",
                )
            ]
        )

    await update.message.reply_text(
        "Активні підписки:\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Використання: /watch <адреса гаманця>")
        return

    address = context.args[0].strip()
    chat_id = update.effective_chat.id
    key = (chat_id, address)

    if key in wallet_watches:
        await update.message.reply_text(f"Ви вже стежите за цим гаманцем.")
        return

    await update.message.reply_text(f"Завантажую поточні позиції: `{address}` ...", parse_mode="Markdown")

    try:
        positions = await api.get_positions_by_address(address)
    except Exception as e:
        logger.error("Failed to fetch positions for %s: %s", address, e)
        await update.message.reply_text(f"Помилка завантаження позицій: {e}")
        return

    known_uids = {p.uid for p in positions}
    wallet_watches[key] = WalletWatch(
        chat_id=chat_id,
        address=address,
        known_position_uids=known_uids,
    )

    short_addr = f"{address[:6]}...{address[-4:]}"
    unwatch_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Припинити стеження", callback_data=f"unwatch:{address}")]]
    )

    if positions:
        lines = []
        for p in positions:
            lines.append(
                f"  • {p.market_title} → {p.outcome_name}\n"
                f"    Шейрсів: {p.size:.2f} | Ціна: {p.avg_price:.4f} | ${p.value_usd:.2f}"
            )
        pos_text = "\n".join(lines)
    else:
        pos_text = "  Позицій поки немає."

    await update.message.reply_text(
        f"Стеження за гаманцем `{short_addr}` увімкнено\n\n"
        f"Поточні позиції ({len(positions)}):\n{pos_text}\n\n"
        f"Ви отримаєте сповіщення при появі нових позицій.",
        reply_markup=unwatch_btn,
        parse_mode="Markdown",
    )


async def cmd_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_watches = {k: v for k, v in wallet_watches.items() if k[0] == chat_id}

    if not user_watches:
        await update.message.reply_text("Немає гаманців під стеженням.")
        return

    lines = []
    buttons = []
    for i, ((_, addr), w) in enumerate(user_watches.items()):
        short = f"{addr[:6]}...{addr[-4:]}"
        lines.append(f"{i + 1}. `{short}` — {len(w.known_position_uids)} позицій")
        buttons.append(
            [InlineKeyboardButton(f"Припинити: {short}", callback_data=f"unwatch:{addr}")]
        )

    await update.message.reply_text(
        "Гаманці під стеженням:\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


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


async def _handle_select_outcome(query) -> None:
    chat_id = query.message.chat_id
    idx = int(query.data.split(":")[1])

    sel = pending_selections.pop(chat_id, None)
    if not sel:
        await query.edit_message_text("Сесія закінчилась. Виконайте /look ще раз.")
        return

    slug, title, outcomes = sel
    if idx >= len(outcomes):
        await query.edit_message_text("Невірний вибір.")
        return

    outcome = outcomes[idx]
    key = (chat_id, outcome.market_id, outcome.name)

    if key in subscriptions:
        await query.edit_message_text(
            f"Ви вже підписані на {outcome.name} для цієї події."
        )
        return

    # Fetch initial orderbook
    try:
        ob = await api.get_orderbook(outcome.market_id)
    except Exception as e:
        logger.error("Failed to fetch orderbook for market %s: %s", outcome.market_id, e)
        await query.edit_message_text(f"Помилка завантаження стакану: {e}")
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
        [[InlineKeyboardButton("Відписатись", callback_data=f"unsub:{outcome.market_id}:{outcome.name}")]]
    )

    await query.edit_message_text(
        f"Підписка на оновлення стакану оформлена\n\n"
        f"Подія: *{title}*\n"
        f"Outcome: *{outcome.name}*\n"
        f"Початкова найкраща ставка: *{price_str}*\n\n"
        f"Ви отримаєте сповіщення при зміні ціни.",
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
            f"Відписано від {sub.category_title} → {outcome_name}."
        )
    else:
        await query.edit_message_text("Підписку не знайдено (вже видалена).")


async def _handle_unwatch(query) -> None:
    chat_id = query.message.chat_id
    address = query.data.split(":", 1)[1]

    key = (chat_id, address)
    w = wallet_watches.pop(key, None)

    short = f"{address[:6]}...{address[-4:]}"
    if w:
        await query.edit_message_text(f"Стеження за гаманцем {short} припинено.")
    else:
        await query.edit_message_text(f"Гаманець {short} не знайдено (вже видалено).")


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
                ob = await api.get_orderbook(sub.outcome.market_id)
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
                else:
                    change_str = "(перше значення)"

                initial_str = f"{sub.initial_price}" if sub.initial_price is not None else "—"

                unsub_button = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Відписатись",
                                callback_data=f"unsub:{sub.outcome.market_id}:{sub.outcome.name}",
                            )
                        ]
                    ]
                )

                try:
                    await app.bot.send_message(
                        chat_id=sub.chat_id,
                        text=(
                            f"Оновлення ціни — *{sub.category_title}*\n"
                            f"Outcome: *{sub.outcome.name}*\n\n"
                            f"Найкраща ставка: *{current_price}* {change_str}\n"
                            f"Початкова: {initial_str}"
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

            short_addr = f"{w.address[:6]}...{w.address[-4:]}"
            unwatch_btn = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Припинити стеження", callback_data=f"unwatch:{w.address}")]]
            )

            for p in new_positions:
                try:
                    await app.bot.send_message(
                        chat_id=w.chat_id,
                        text=(
                            f"Нова позиція — гаманець `{short_addr}`\n\n"
                            f"Подія: *{p.market_title}*\n"
                            f"Outcome: *{p.outcome_name}*\n"
                            f"Шейрсів: *{p.size:.2f}*\n"
                            f"Ціна: *{p.avg_price:.4f}*\n"
                            f"Вартість: *${p.value_usd:.2f}*"
                        ),
                        reply_markup=unwatch_btn,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error("Failed to send wallet notification to chat %s: %s", w.chat_id, e)


async def post_init(app: Application) -> None:
    global api
    api = PredictAPI(PREDICT_API_KEY)
    asyncio.create_task(poll_orderbooks(app))
    asyncio.create_task(poll_wallets(app))


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
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot started. Polling interval: %ds", POLL_INTERVAL)
    app.run_polling()


if __name__ == "__main__":
    main()
