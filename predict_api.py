import logging

import httpx
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


API_BASE = "https://api.predict.fun"


@dataclass
class Outcome:
    name: str
    market_id: int
    index_set: int
    on_chain_id: str
    invert_book: bool = False  # True for secondary outcome in single-market binary events


@dataclass
class OrderBook:
    market_id: int
    bids: list[list[float]]  # [[price, quantity], ...]
    asks: list[list[float]]
    update_timestamp_ms: int

    @property
    def top_bid_price(self) -> float | None:
        if self.bids:
            return self.bids[0][0]
        return None

    @property
    def top_ask_price(self) -> float | None:
        if self.asks:
            return self.asks[0][0]
        return None


@dataclass
class Position:
    market_id: int
    market_title: str
    category_slug: str
    outcome_name: str
    outcome_index: int
    size: float
    avg_price: float
    value_usd: float
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def uid(self) -> str:
        return f"{self.market_id}:{self.outcome_index}"


def _pick_float(d: dict, *keys: str) -> float:
    """Return the first non-zero float found among *keys* in dict *d*."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                f = float(v)
                if f != 0.0:
                    return f
            except (ValueError, TypeError):
                continue
    return 0.0


def invert_orderbook(ob: OrderBook) -> OrderBook:
    """Invert an orderbook for the secondary outcome in a binary market.

    Buying outcome B at price P = selling outcome A at price (1-P),
    so bids become inverted asks and vice versa.
    """
    inv_bids = sorted(
        [[round(1 - a[0], 4), a[1]] for a in ob.asks],
        key=lambda x: x[0], reverse=True,
    )
    inv_asks = sorted(
        [[round(1 - b[0], 4), b[1]] for b in ob.bids],
        key=lambda x: x[0],
    )
    return OrderBook(
        market_id=ob.market_id,
        bids=inv_bids,
        asks=inv_asks,
        update_timestamp_ms=ob.update_timestamp_ms,
    )


AMOUNT_DECIMALS = 18  # on-chain ERC1155 conditional tokens use 18 decimals


class PredictAPI:
    def __init__(self, api_key: str, proxy: str | None = None):
        self.api_key = api_key
        client_kwargs = dict(
            base_url=API_BASE,
            headers={"x-api-key": api_key},
            timeout=15.0,
        )
        if proxy:
            client_kwargs["proxy"] = proxy
        self.client = httpx.AsyncClient(**client_kwargs)

    async def close(self):
        await self.client.aclose()

    async def get_category_by_slug(self, slug: str) -> dict:
        resp = await self.client.get(f"/v1/categories/{slug}")
        resp.raise_for_status()
        return resp.json()

    async def search(self, query: str) -> dict:
        resp = await self.client.get("/v1/search", params={"query": query})
        resp.raise_for_status()
        return resp.json()

    async def get_market(self, market_id: int) -> dict:
        resp = await self.client.get(f"/v1/markets/{market_id}")
        resp.raise_for_status()
        return resp.json()

    async def get_orderbook(self, market_id: int, invert: bool = False) -> OrderBook:
        resp = await self.client.get(f"/v1/markets/{market_id}/orderbook")
        resp.raise_for_status()
        data = resp.json()["data"]
        ob = OrderBook(
            market_id=data["marketId"],
            bids=data.get("bids", []),
            asks=data.get("asks", []),
            update_timestamp_ms=data.get("updateTimestampMs", 0),
        )
        return invert_orderbook(ob) if invert else ob

    async def get_positions_by_address(self, address: str) -> list[Position]:
        positions: list[Position] = []
        cursor: str | None = None

        while True:
            params: dict = {"first": 50}
            if cursor:
                params["after"] = cursor

            resp = await self.client.get(f"/v1/positions/{address}", params=params)
            resp.raise_for_status()
            body = resp.json()

            items = body.get("data", [])
            if items:
                logger.debug("Positions API sample item keys: %s", list(items[0].keys()))
                logger.debug("Positions API sample item: %s", items[0])

            for p in items:
                market = p.get("market", {})
                outcome = p.get("outcome", {})

                # amount is raw on-chain value with 18 decimals
                raw_amount = _pick_float(p, "amount", "size", "shares", "quantity", "balance")
                shares = raw_amount / (10 ** AMOUNT_DECIMALS) if raw_amount > 1e12 else raw_amount

                value_usd = _pick_float(p, "valueUsd", "value", "totalValue", "cost")
                avg_price = _pick_float(p, "avgPrice", "averagePrice", "price", "entryPrice")

                # API doesn't return price directly — derive from value/shares
                if avg_price == 0.0 and shares > 0 and value_usd > 0:
                    avg_price = value_usd / shares
                if value_usd == 0.0 and shares > 0 and avg_price > 0:
                    value_usd = shares * avg_price

                positions.append(
                    Position(
                        market_id=market.get("id", 0),
                        market_title=market.get("title", ""),
                        category_slug=market.get("categorySlug", ""),
                        outcome_name=outcome.get("name", ""),
                        outcome_index=outcome.get("indexSet", 0),
                        size=shares,
                        avg_price=avg_price,
                        value_usd=value_usd,
                        raw=p,
                    )
                )

            cursor = body.get("cursor")
            if not cursor or not items:
                break

        return positions

    async def get_outcomes_from_slug(self, slug: str) -> tuple[str, list[Outcome], dict]:
        """Fetch category by slug and extract outcomes (markets within the category).

        Returns (category_title, list_of_outcomes, raw_category_data).

        Predict.fun structure:
        - A category (event) contains multiple markets.
        - For neg-risk categories each market represents a separate outcome.
        - For simple binary categories there is one market with outcomes in its outcomes[] array.
        """
        cat = await self.get_category_by_slug(slug)
        cat_data = cat.get("data", cat)

        title = cat_data.get("title", slug)
        markets = cat_data.get("markets", [])

        outcomes: list[Outcome] = []

        if len(markets) > 1:
            # Multi-market category (neg-risk): each market IS an outcome
            for m in markets:
                name = m.get("title") or m.get("question", f"Market {m['id']}")
                # For neg-risk markets, the token_id is the onChainId of the
                # "Yes" outcome (indexSet=1) inside each market's outcomes[]
                market_ocs = m.get("outcomes", [])
                on_chain_id = ""
                idx_set = 0
                for o in market_ocs:
                    if o.get("indexSet") == 1 or not on_chain_id:
                        on_chain_id = o.get("onChainId", "")
                        idx_set = o.get("indexSet", 0)
                outcomes.append(
                    Outcome(
                        name=name,
                        market_id=m["id"],
                        index_set=idx_set,
                        on_chain_id=on_chain_id,
                    )
                )
        elif len(markets) == 1:
            market = markets[0]
            market_outcomes = market.get("outcomes", [])
            if market_outcomes:
                for i, o in enumerate(market_outcomes):
                    outcomes.append(
                        Outcome(
                            name=o["name"],
                            market_id=market["id"],
                            index_set=o.get("indexSet", 0),
                            on_chain_id=o.get("onChainId", ""),
                            invert_book=i > 0,  # second+ outcome needs inverted orderbook
                        )
                    )
            else:
                outcomes.append(
                    Outcome(
                        name=market.get("title", "Yes"),
                        market_id=market["id"],
                        index_set=0,
                        on_chain_id="",
                    )
                )

        return title, outcomes, cat_data
