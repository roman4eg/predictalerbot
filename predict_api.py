import httpx
from dataclasses import dataclass


API_BASE = "https://api.predict.fun"


@dataclass
class Outcome:
    name: str
    market_id: int
    index_set: int
    on_chain_id: str


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


class PredictAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"x-api-key": api_key},
            timeout=15.0,
        )

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

    async def get_orderbook(self, market_id: int) -> OrderBook:
        resp = await self.client.get(f"/v1/markets/{market_id}/orderbook")
        resp.raise_for_status()
        data = resp.json()["data"]
        return OrderBook(
            market_id=data["marketId"],
            bids=data.get("bids", []),
            asks=data.get("asks", []),
            update_timestamp_ms=data.get("updateTimestampMs", 0),
        )

    async def get_outcomes_from_slug(self, slug: str) -> tuple[str, list[Outcome]]:
        """Fetch category by slug and extract outcomes (markets within the category).

        Returns (category_title, list_of_outcomes).

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
                outcomes.append(
                    Outcome(
                        name=name,
                        market_id=m["id"],
                        index_set=0,
                        on_chain_id="",
                    )
                )
        elif len(markets) == 1:
            market = markets[0]
            market_outcomes = market.get("outcomes", [])
            if market_outcomes:
                for o in market_outcomes:
                    outcomes.append(
                        Outcome(
                            name=o["name"],
                            market_id=market["id"],
                            index_set=o.get("indexSet", 0),
                            on_chain_id=o.get("onChainId", ""),
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

        return title, outcomes
