"""Web UI for the predict.fun points farming bot."""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from predict_api import PredictAPI
from predict_bot import FarmingEngine, FarmingSession, WEI

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

PREDICT_API_KEY = os.environ["PREDICT_API_KEY"]
PRIVATE_KEY = os.environ["WALLET_PRIVATE_KEY"]
PREDICT_ACCOUNT = os.getenv("PREDICT_ACCOUNT", "")

STATIC_DIR = Path(__file__).parent / "static"

api: PredictAPI | None = None
engine: FarmingEngine | None = None

app = FastAPI(title="Predict.fun Farming Bot")


@app.on_event("startup")
async def startup():
    global api, engine
    api = PredictAPI(PREDICT_API_KEY)
    engine = FarmingEngine(
        api, PREDICT_API_KEY, PRIVATE_KEY,
        predict_account=PREDICT_ACCOUNT or None,
    )
    engine.start()
    logger.info("Farming web UI started")


@app.on_event("shutdown")
async def shutdown():
    if api:
        await api.close()


# --------------- API routes ---------------

@app.get("/api/resolve-market")
async def resolve_market(url: str):
    """Parse a predict.fun URL and return market info with outcomes."""
    import re
    m = re.search(r"predict\.fun/market/([A-Za-z0-9_-]+)", url)
    if not m:
        return JSONResponse({"error": "Invalid URL"}, status_code=400)

    slug = m.group(1)
    try:
        title, outcomes, cat_data = await api.get_outcomes_from_slug(slug)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    markets = cat_data.get("markets", [])

    result_outcomes = []
    for o in outcomes:
        market = next((mk for mk in markets if mk["id"] == o.market_id), {})
        result_outcomes.append({
            "name": o.name,
            "market_id": o.market_id,
            "on_chain_id": o.on_chain_id,
            "is_neg_risk": cat_data.get("isNegRisk", False),
            "is_yield_bearing": cat_data.get("isYieldBearing", False),
            "fee_rate_bps": market.get("feeRateBps", 0),
        })

    return {
        "title": title,
        "slug": slug,
        "outcomes": result_outcomes,
    }


@app.post("/api/sessions")
async def create_session(request: Request):
    """Create a new farming session."""
    body = await request.json()

    cutoff_utc = None
    if body.get("cutoff_utc"):
        cutoff_utc = datetime.fromisoformat(body["cutoff_utc"]).replace(tzinfo=timezone.utc)

    shares = float(body.get("shares", 200))
    shares_wei = int(shares * WEI)

    session = FarmingSession(
        session_id=str(uuid.uuid4())[:8],
        market_id=int(body["market_id"]),
        market_title=body.get("market_title", ""),
        token_id=body["token_id"],
        outcome_name=body.get("outcome_name", ""),
        side=int(body.get("side", 0)),
        shares_wei=shares_wei,
        max_spread_cents=int(body.get("max_spread_cents", 3)),
        cutoff_utc=cutoff_utc,
        cutoff_minutes_before=int(body.get("cutoff_minutes_before", 30)),
        is_neg_risk=body.get("is_neg_risk", False),
        is_yield_bearing=body.get("is_yield_bearing", False),
        fee_rate_bps=int(body.get("fee_rate_bps", 0)),
    )

    engine.add_session(session)
    return {"ok": True, "session": session.to_dict()}


@app.get("/api/sessions")
async def list_sessions():
    """List all farming sessions."""
    return {
        "sessions": [s.to_dict() for s in engine.sessions.values()]
    }


@app.delete("/api/sessions/{session_id}")
async def stop_session(session_id: str):
    """Stop and remove a farming session."""
    await engine.cancel_all_for_session(session_id)
    engine.remove_session(session_id)
    return {"ok": True}


# --------------- HTML page ---------------

@app.get("/", response_class=FileResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")
