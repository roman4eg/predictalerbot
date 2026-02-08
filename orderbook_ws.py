"""WebSocket client for predict.fun orderbook streams.

Maintains a real-time cache of orderbooks per market, updated via
WebSocket full snapshots.  Falls back to REST when WS data is unavailable.

Protocol (wss://ws.predict.fun/ws):
- Subscribe:  {"method": "subscribe", "requestId": N, "params": ["predictOrderbook/{marketId}"]}
- Data:       {"type": "M", "topic": "predictOrderbook/{marketId}", "data": {"bids": [...], "asks": [...], "timestamp": ...}}
- Heartbeat:  server sends {"type": "M", "topic": "heartbeat", "data": <ts>}
              client responds {"method": "heartbeat", "data": <ts>}
- Ack:        {"type": "R", "requestId": N, ...}

Each data message is a **full snapshot** (not incremental diff), so the
client simply replaces its cached orderbook on every message.
"""

import asyncio
import json
import logging
import time

import websockets

from predict_api import OrderBook

logger = logging.getLogger(__name__)

WS_URL = "wss://ws.predict.fun/ws"
HEARTBEAT_TIMEOUT = 20  # seconds — server sends every ~15s
STALE_THRESHOLD_DISCONNECTED = 60  # seconds — cache TTL when WS is down
MAX_RECONNECT_DELAY = 60


def _normalize_levels(raw_levels: list) -> list:
    """Normalize orderbook levels to [[price, size], ...] format.

    The WS may send levels as:
    - [[0.40, 100], ...]              — array of arrays (same as REST)
    - [{"price": "0.40", "size": "100"}, ...]  — array of objects
    """
    if not raw_levels:
        return []
    sample = raw_levels[0]
    if isinstance(sample, dict):
        return [[float(e["price"]), float(e["size"])] for e in raw_levels]
    return [[float(e[0]), float(e[1])] for e in raw_levels]


class OrderBookWS:
    """Real-time orderbook cache via WebSocket with auto-reconnect."""

    def __init__(self, api_key: str):
        self.api_key = api_key

        # WebSocket state
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._reconnect_delay = 1
        self._connected = asyncio.Event()

        # Orderbook cache: market_id -> OrderBook
        self._cache: dict[int, OrderBook] = {}
        # Wall-clock time of last update per market
        self._cache_ts: dict[int, float] = {}

        # Subscriptions with reference counting
        self._subscriptions: set[int] = set()
        self._ref_counts: dict[int, int] = {}

        # Protocol state
        self._request_id = 0

        # Stats
        self._msg_count = 0
        self._markets_seen: set[int] = set()

    # ── Public API ──────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._connected.is_set()

    def subscribe(self, market_id: int) -> None:
        """Add a subscription (ref-counted). Safe to call multiple times."""
        self._ref_counts[market_id] = self._ref_counts.get(market_id, 0) + 1
        if market_id not in self._subscriptions:
            self._subscriptions.add(market_id)
            if self.is_connected:
                asyncio.create_task(self._send_subscribe(market_id))

    def unsubscribe(self, market_id: int) -> None:
        """Remove a subscription (ref-counted). Actually unsubscribes when
        ref count drops to zero."""
        count = self._ref_counts.get(market_id, 0)
        if count <= 1:
            self._ref_counts.pop(market_id, None)
            self._subscriptions.discard(market_id)
            self._cache.pop(market_id, None)
            self._cache_ts.pop(market_id, None)
            self._markets_seen.discard(market_id)
            if self.is_connected:
                asyncio.create_task(self._send_unsubscribe(market_id))
        else:
            self._ref_counts[market_id] = count - 1

    def get_orderbook(self, market_id: int) -> OrderBook | None:
        """Return the latest cached orderbook, or None if unavailable.

        When WS is connected, the cache is always trusted — no update simply
        means the orderbook hasn't changed.  When disconnected, a staleness
        threshold is applied.
        """
        ob = self._cache.get(market_id)
        if ob is None:
            return None
        # Connected: trust the cache (no WS update = no orderbook change)
        if self.is_connected:
            return ob
        # Disconnected: apply staleness check
        ts = self._cache_ts.get(market_id, 0)
        if time.time() - ts > STALE_THRESHOLD_DISCONNECTED:
            return None
        return ob

    async def start(self) -> None:
        """Start the WebSocket connection loop in the background."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("OrderBookWS starting")

    async def stop(self) -> None:
        """Gracefully stop the WebSocket connection."""
        self._running = False
        self._connected.clear()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._ws = None
        logger.info("OrderBookWS stopped")

    # ── Connection loop ─────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Outer loop: connect → listen → reconnect on failure."""
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("OrderBookWS connection error: %s", e)
            finally:
                self._connected.clear()
                self._ws = None

            if not self._running:
                break

            logger.info("OrderBookWS reconnecting in %ds…", self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, MAX_RECONNECT_DELAY)

    async def _connect_and_listen(self) -> None:
        """Single connection lifecycle: connect, subscribe, process."""
        headers = {"x-api-key": self.api_key}
        async with websockets.connect(
            WS_URL,
            additional_headers=headers,
            compression=None,
            max_size=10 * 1024 * 1024,
            ping_interval=None,  # we handle heartbeats manually
        ) as ws:
            self._ws = ws
            self._connected.set()
            self._reconnect_delay = 1  # reset backoff on successful connect
            self._msg_count = 0
            logger.info("OrderBookWS connected (%d subscriptions)", len(self._subscriptions))

            # Resubscribe to all active markets
            for market_id in list(self._subscriptions):
                await self._send_subscribe(market_id)

            # Read messages with heartbeat-based timeout
            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=HEARTBEAT_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("OrderBookWS heartbeat timeout (%ds), reconnecting",
                                   HEARTBEAT_TIMEOUT)
                    return

                try:
                    msg = json.loads(raw)
                    await self._handle_message(msg)
                except Exception as e:
                    logger.warning("OrderBookWS message error: %s (raw: %.200s)", e, raw)

    # ── Message handling ────────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        msg_type = msg.get("type")

        if msg_type == "M":
            topic = msg.get("topic", "")

            if topic == "heartbeat":
                await self._echo_heartbeat(msg["data"])

            elif topic.startswith("predictOrderbook/"):
                self._process_orderbook(topic, msg.get("data", {}))

            else:
                logger.debug("OrderBookWS unknown topic: %s", topic)

        elif msg_type == "R":
            # Ack for subscribe/unsubscribe
            req_id = msg.get("requestId")
            success = msg.get("success", True)
            if success:
                logger.info("OrderBookWS request %s confirmed OK", req_id)
            else:
                logger.warning("OrderBookWS request %s failed: %s", req_id, msg)

        else:
            logger.debug("OrderBookWS unknown message type: %s", msg_type)

    async def _echo_heartbeat(self, ts_data) -> None:
        """Respond to server heartbeat to keep connection alive."""
        try:
            await self._ws.send(json.dumps({
                "method": "heartbeat",
                "data": ts_data,
            }))
        except Exception as e:
            logger.warning("OrderBookWS heartbeat send failed: %s", e)

    def _process_orderbook(self, topic: str, data: dict) -> None:
        """Update cached orderbook from a full snapshot message."""
        market_id = int(topic.split("/")[1])

        bids = _normalize_levels(data.get("bids", []))
        asks = _normalize_levels(data.get("asks", []))
        ts = data.get("timestamp", 0)

        self._cache[market_id] = OrderBook(
            market_id=market_id,
            bids=bids,
            asks=asks,
            update_timestamp_ms=ts,
        )
        self._cache_ts[market_id] = time.time()
        self._msg_count += 1

        # Log first message per market and then every 100 messages
        if market_id not in self._markets_seen:
            self._markets_seen.add(market_id)
            top_bid = bids[0][0] if bids else None
            top_ask = asks[0][0] if asks else None
            logger.info("OrderBookWS first data for market %d: bid=%s ask=%s (%d levels)",
                        market_id, top_bid, top_ask, len(bids) + len(asks))
        elif self._msg_count % 100 == 0:
            logger.info("OrderBookWS stats: %d messages received, %d markets active",
                        self._msg_count, len(self._cache))

    # ── Subscribe / unsubscribe ─────────────────────────────────

    async def _send_subscribe(self, market_id: int) -> None:
        self._request_id += 1
        try:
            await self._ws.send(json.dumps({
                "method": "subscribe",
                "requestId": self._request_id,
                "params": [f"predictOrderbook/{market_id}"],
            }))
            logger.info("OrderBookWS subscribing to market %d (req %d)",
                        market_id, self._request_id)
        except Exception as e:
            logger.warning("OrderBookWS subscribe failed for market %d: %s", market_id, e)

    async def _send_unsubscribe(self, market_id: int) -> None:
        self._request_id += 1
        try:
            await self._ws.send(json.dumps({
                "method": "unsubscribe",
                "requestId": self._request_id,
                "params": [f"predictOrderbook/{market_id}"],
            }))
            logger.info("OrderBookWS unsubscribed from market %d", market_id)
        except Exception as e:
            logger.warning("OrderBookWS unsubscribe failed for market %d: %s", market_id, e)
