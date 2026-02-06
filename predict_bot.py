"""Predict.fun points farming bot.

Monitors the orderbook for a given market and places limit BUY orders
one tick behind the current top bid to earn Predict Points, while
staying within the max spread threshold.

The bot hides behind the top bid: if top bid is 38¢ it places at 37¢.
When the top bid moves down to 37¢ (our level), the bot moves to 36¢.
It keeps doing this as long as the order stays within the allowed spread.

A cutoff time can be set (e.g., 30 min before a match) so that the bot
automatically stops working on the market when the deadline arrives.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from predict_sdk import (
    BuildOrderInput,
    ChainId,
    LimitHelperInput,
    OrderBuilder,
    Side,
)

from predict_api import PredictAPI

logger = logging.getLogger(__name__)

WEI = 10**18


@dataclass
class FarmingSession:
    session_id: str
    market_id: int
    market_title: str
    token_id: str  # outcome onChainId
    outcome_name: str
    side: int  # Side.BUY = 0, Side.SELL = 1
    shares_wei: int  # quantity of shares in wei
    max_spread_cents: int  # e.g. 3
    cutoff_utc: datetime | None  # stop farming before this time
    cutoff_minutes_before: int  # stop N minutes before cutoff_utc
    is_neg_risk: bool
    is_yield_bearing: bool
    fee_rate_bps: int

    # Runtime state
    active: bool = True
    current_order_hash: str | None = None
    current_order_price_cents: int | None = None
    last_top_bid: int | None = None
    last_top_ask: int | None = None
    error: str | None = None
    placed_count: int = 0
    cancelled_count: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def deadline(self) -> datetime | None:
        if self.cutoff_utc is None:
            return None
        from datetime import timedelta
        return self.cutoff_utc - timedelta(minutes=self.cutoff_minutes_before)

    @property
    def is_expired(self) -> bool:
        dl = self.deadline
        if dl is None:
            return False
        return datetime.now(timezone.utc) >= dl

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "market_id": self.market_id,
            "market_title": self.market_title,
            "outcome_name": self.outcome_name,
            "side": "BUY" if self.side == 0 else "SELL",
            "shares": self.shares_wei / WEI,
            "max_spread_cents": self.max_spread_cents,
            "cutoff_utc": self.cutoff_utc.isoformat() if self.cutoff_utc else None,
            "cutoff_minutes_before": self.cutoff_minutes_before,
            "active": self.active,
            "is_expired": self.is_expired,
            "current_order_hash": self.current_order_hash,
            "current_order_price_cents": self.current_order_price_cents,
            "last_top_bid": self.last_top_bid,
            "last_top_ask": self.last_top_ask,
            "error": self.error,
            "placed_count": self.placed_count,
            "cancelled_count": self.cancelled_count,
        }


def _price_to_cents(price: float) -> int:
    """Convert a decimal price (e.g. 0.38) to cents (38)."""
    return round(price * 100)


def _cents_to_wei(cents: int) -> int:
    """Convert cents (e.g. 37) to price-per-share in wei (0.37 * 10^18)."""
    return cents * WEI // 100


class FarmingEngine:
    """Manages farming sessions — places/moves orders based on orderbook."""

    def __init__(self, api: PredictAPI, api_key: str, private_key: str,
                 predict_account: str | None = None):
        self.api = api
        self.api_key = api_key
        self.private_key = private_key
        self.predict_account = predict_account
        self.sessions: dict[str, FarmingSession] = {}
        self._poll_task: asyncio.Task | None = None

        opts = None
        if predict_account:
            from predict_sdk import OrderBuilderOptions
            opts = OrderBuilderOptions(predict_account=predict_account)

        self.builder = OrderBuilder.make(
            ChainId.BNB_MAINNET, private_key, opts
        )

    def add_session(self, session: FarmingSession) -> None:
        self.sessions[session.session_id] = session
        logger.info("Added farming session %s for market %s (%s)",
                     session.session_id, session.market_id, session.outcome_name)

    def remove_session(self, session_id: str) -> FarmingSession | None:
        session = self.sessions.pop(session_id, None)
        if session:
            session.active = False
            logger.info("Removed farming session %s", session_id)
        return session

    def start(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info("Farming engine started")

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(3)
            for sid in list(self.sessions):
                session = self.sessions.get(sid)
                if session is None or not session.active:
                    continue
                try:
                    await self._tick(session)
                except Exception as e:
                    session.error = str(e)
                    logger.error("Tick error for session %s: %s", sid, e)

    async def _tick(self, s: FarmingSession) -> None:
        # Check deadline
        if s.is_expired:
            logger.info("Session %s expired (cutoff reached), cancelling order", s.session_id)
            await self._cancel_current_order(s)
            s.active = False
            return

        # Fetch orderbook
        ob = await self.api.get_orderbook(s.market_id)

        top_bid_price = ob.top_bid_price
        top_ask_price = ob.top_ask_price

        if top_bid_price is None or top_ask_price is None:
            s.error = "Orderbook empty"
            return

        top_bid_cents = _price_to_cents(top_bid_price)
        top_ask_cents = _price_to_cents(top_ask_price)
        s.last_top_bid = top_bid_cents
        s.last_top_ask = top_ask_cents

        spread_cents = top_ask_cents - top_bid_cents

        # Check if spread is within the allowed threshold
        if spread_cents > s.max_spread_cents * 2:
            s.error = f"Spread {spread_cents}¢ exceeds max ±{s.max_spread_cents}¢"
            await self._cancel_current_order(s)
            return

        s.error = None

        # Target price: one cent behind top bid (for BUY side)
        if s.side == Side.BUY:
            target_cents = top_bid_cents - 1
            min_allowed = top_ask_cents - s.max_spread_cents * 2
            if target_cents < min_allowed:
                target_cents = min_allowed
        else:
            target_cents = top_ask_cents + 1
            max_allowed = top_bid_cents + s.max_spread_cents * 2
            if target_cents > max_allowed:
                target_cents = max_allowed

        if target_cents <= 0 or target_cents >= 100:
            s.error = f"Invalid target price: {target_cents}¢"
            return

        # If our order is already at the right price — no action needed
        if s.current_order_price_cents == target_cents:
            return

        # If top bid moved down TO our price, we need to move further
        if s.side == Side.BUY and s.current_order_price_cents is not None:
            if top_bid_cents <= s.current_order_price_cents:
                logger.info("Session %s: top bid dropped to our level (%d¢), moving to %d¢",
                            s.session_id, top_bid_cents, target_cents)

        # Cancel existing order and place new one
        await self._cancel_current_order(s)
        await self._place_order(s, target_cents)

    async def _place_order(self, s: FarmingSession, price_cents: int) -> None:
        price_wei = _cents_to_wei(price_cents)

        amounts = self.builder.get_limit_order_amounts(
            LimitHelperInput(
                side=Side(s.side),
                price_per_share_wei=price_wei,
                quantity_wei=s.shares_wei,
            )
        )

        order = self.builder.build_order(
            "LIMIT",
            BuildOrderInput(
                side=Side(s.side),
                token_id=s.token_id,
                maker_amount=str(amounts.maker_amount),
                taker_amount=str(amounts.taker_amount),
                fee_rate_bps=s.fee_rate_bps,
            ),
        )

        typed_data = self.builder.build_typed_data(
            order,
            is_neg_risk=s.is_neg_risk,
            is_yield_bearing=s.is_yield_bearing,
        )
        signed = self.builder.sign_typed_data_order(typed_data)
        order_hash = self.builder.build_typed_data_hash(typed_data)

        price_per_share = str(price_wei)

        payload = {
            "data": {
                "pricePerShare": price_per_share,
                "strategy": "LIMIT",
                "order": {
                    "hash": order_hash,
                    "salt": signed.salt,
                    "maker": signed.maker,
                    "signer": signed.signer,
                    "taker": signed.taker,
                    "tokenId": signed.token_id,
                    "makerAmount": signed.maker_amount,
                    "takerAmount": signed.taker_amount,
                    "expiration": signed.expiration,
                    "nonce": signed.nonce,
                    "feeRateBps": signed.fee_rate_bps,
                    "side": signed.side,
                    "signatureType": signed.signature_type,
                    "signature": signed.signature,
                },
            }
        }

        resp = await self.api.client.post("/v1/orders", json=payload)
        resp.raise_for_status()

        s.current_order_hash = order_hash
        s.current_order_price_cents = price_cents
        s.placed_count += 1

        logger.info("Session %s: placed %s order at %d¢ (hash: %s)",
                     s.session_id, "BUY" if s.side == 0 else "SELL",
                     price_cents, order_hash[:12])

    async def _cancel_current_order(self, s: FarmingSession) -> None:
        if s.current_order_hash is None:
            return

        try:
            resp = await self.api.client.post(
                "/v1/orders/cancel",
                json={"data": {"orderHashes": [s.current_order_hash]}},
            )
            resp.raise_for_status()
            s.cancelled_count += 1
            logger.info("Session %s: cancelled order %s",
                         s.session_id, s.current_order_hash[:12])
        except Exception as e:
            logger.warning("Failed to cancel order %s: %s", s.current_order_hash, e)
        finally:
            s.current_order_hash = None
            s.current_order_price_cents = None

    async def cancel_all_for_session(self, session_id: str) -> None:
        s = self.sessions.get(session_id)
        if s:
            await self._cancel_current_order(s)
            s.active = False
