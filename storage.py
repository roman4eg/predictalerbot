"""Persistent storage for farming sessions and order metrics.

Sessions are saved to data/sessions.json on every state change.
Order history is appended to data/metrics.json on every order completion.

Predict.fun points week: Tuesday 14:00 UTC → Tuesday 14:00 UTC.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")


# ── Predict.fun week helpers ────────────────────────────────

def get_predict_week_start(dt: datetime | None = None) -> datetime:
    """Return the Tuesday 14:00 UTC that starts the current predict.fun points week."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    # Tuesday = weekday() == 1
    days_since_tue = (dt.weekday() - 1) % 7
    candidate = (dt - timedelta(days=days_since_tue)).replace(
        hour=14, minute=0, second=0, microsecond=0,
    )
    if candidate > dt:
        candidate -= timedelta(weeks=1)
    return candidate


# ── Data classes ────────────────────────────────────────────

@dataclass
class WeeklyStats:
    week_start: datetime
    week_end: datetime
    total_orders: int
    liquidity_hours: float       # sum of ($ × hours) across all orders
    avg_lifetime_sec: float      # average order lifetime in seconds
    avg_depth_cents: float       # average placement depth
    avg_liquidity_usd: float     # time-weighted average $ in orderbook
    markets: list[str]


# ── Storage ─────────────────────────────────────────────────

class Storage:
    """Persistent storage for farming sessions and order metrics."""

    def __init__(self, data_dir: str | Path = DATA_DIR):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_path = self.data_dir / "sessions.json"
        self._metrics_path = self.data_dir / "metrics.json"
        self._orders: list[dict] = []
        self._load_metrics()

    # ── Session persistence ─────────────────────────────────

    def save_sessions(self, sessions: dict) -> None:
        """Save active farming sessions to disk."""
        data = []
        for s in sessions.values():
            if not s.active:
                continue
            data.append({
                "session_id": s.session_id,
                "market_id": s.market_id,
                "market_title": s.market_title,
                "token_id": s.token_id,
                "outcome_name": s.outcome_name,
                "side": s.side,
                "shares_wei": s.shares_wei,
                "max_spread_cents": s.max_spread_cents,
                "depth_cents": s.depth_cents,
                "stop_at": s.stop_at.isoformat() if s.stop_at else None,
                "is_neg_risk": s.is_neg_risk,
                "is_yield_bearing": s.is_yield_bearing,
                "fee_rate_bps": s.fee_rate_bps,
                "invert_book": s.invert_book,
                "start_at": s.start_at.isoformat() if s.start_at else None,
                "notify_moves": s.notify_moves,
                "chat_id": s.chat_id,
                # Runtime state for orphan recovery on restart
                "current_order_id": s.current_order_id,
                "current_order_hash": s.current_order_hash,
                "current_order_price_cents": s.current_order_price_cents,
            })
        self._write_json(self._sessions_path, data)

    def load_sessions(self) -> list[dict]:
        """Load saved sessions from disk."""
        if not self._sessions_path.exists():
            return []
        try:
            with open(self._sessions_path) as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to load sessions: %s", e)
            return []

    # ── Order metrics ───────────────────────────────────────

    def record_order(self, session_id: str, market_title: str, outcome_name: str,
                     placed_at: float, cancelled_at: float, price_cents: int,
                     shares: int, depth_cents: int) -> None:
        """Record a completed order (placed → cancelled/moved)."""
        if cancelled_at <= placed_at:
            return  # invalid record
        self._orders.append({
            "sid": session_id,
            "market": market_title,
            "outcome": outcome_name,
            "placed": placed_at,
            "cancelled": cancelled_at,
            "price": price_cents,
            "shares": shares,
            "depth": depth_cents,
        })
        self._save_metrics()

    def get_weekly_stats(self, week_start: datetime | None = None) -> WeeklyStats:
        """Calculate aggregated stats for a predict.fun points week."""
        if week_start is None:
            week_start = get_predict_week_start()
        week_end = week_start + timedelta(weeks=1)
        ws = week_start.timestamp()
        we = week_end.timestamp()

        total_orders = 0
        total_liq_sec = 0.0
        total_duration = 0.0
        total_depth = 0
        markets: set[str] = set()

        for o in self._orders:
            # Check if order overlaps with week
            if o["placed"] >= we or o["cancelled"] <= ws:
                continue
            # Clamp to week boundaries
            start = max(o["placed"], ws)
            end = min(o["cancelled"], we)
            dur = end - start
            liq_usd = o["shares"] * o["price"] / 100

            total_liq_sec += liq_usd * dur
            total_duration += dur
            total_orders += 1
            total_depth += o["depth"]
            markets.add(o["market"])

        return WeeklyStats(
            week_start=week_start,
            week_end=week_end,
            total_orders=total_orders,
            liquidity_hours=total_liq_sec / 3600,
            avg_lifetime_sec=total_duration / total_orders if total_orders else 0,
            avg_depth_cents=total_depth / total_orders if total_orders else 0,
            avg_liquidity_usd=total_liq_sec / total_duration if total_duration else 0,
            markets=sorted(markets),
        )

    def get_current_liquidity_usd(self, sessions: dict) -> float:
        """Calculate total $ currently sitting in the orderbook."""
        total = 0.0
        for s in sessions.values():
            if s.active and s.current_order_price_cents and s.last_placed_shares:
                total += s.last_placed_shares * s.current_order_price_cents / 100
        return total

    # ── Internal ────────────────────────────────────────────

    def _load_metrics(self) -> None:
        if not self._metrics_path.exists():
            return
        try:
            with open(self._metrics_path) as f:
                data = json.load(f)
                self._orders = data.get("orders", [])
            # Prune orders older than 8 weeks
            self._prune_old_orders()
            logger.info("Loaded %d order records from metrics", len(self._orders))
        except Exception as e:
            logger.warning("Failed to load metrics: %s", e)

    def _prune_old_orders(self) -> None:
        """Remove orders older than 8 weeks to prevent unbounded file growth."""
        cutoff = time.time() - 8 * 7 * 86400
        old_len = len(self._orders)
        self._orders = [o for o in self._orders if o["cancelled"] > cutoff]
        pruned = old_len - len(self._orders)
        if pruned:
            logger.info("Pruned %d old order records (>8 weeks)", pruned)
            self._save_metrics()

    def _save_metrics(self) -> None:
        self._write_json(self._metrics_path, {"orders": self._orders})

    @staticmethod
    def _write_json(path: Path, data) -> None:
        """Atomic write: write to .tmp then rename to avoid corruption."""
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            tmp.replace(path)
        except Exception as e:
            logger.warning("Failed to write %s: %s", path, e)
