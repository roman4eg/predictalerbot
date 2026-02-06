"""Web UI for the predict.fun points farming bot."""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

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
        title, outcomes = await api.get_outcomes_from_slug(slug)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    cat = await api.get_category_by_slug(slug)
    cat_data = cat.get("data", cat)
    markets = cat_data.get("markets", [])

    result_outcomes = []
    for o in outcomes:
        market = next((m for m in markets if m["id"] == o.market_id), {})
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

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


HTML_PAGE = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Predict.fun — Farming Bot</title>
<style>
  :root { --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a; --accent: #6c5ce7; --green: #00b894; --red: #d63031; --text: #dfe6e9; --muted: #636e72; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 20px; max-width: 900px; margin: 0 auto; }
  h1 { font-size: 1.4em; margin-bottom: 20px; color: var(--accent); }
  h2 { font-size: 1.1em; margin-bottom: 12px; color: var(--muted); }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px; }
  label { display: block; font-size: 0.85em; color: var(--muted); margin-bottom: 4px; margin-top: 10px; }
  input, select { width: 100%; padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg); color: var(--text); font-size: 0.95em; }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  .row { display: flex; gap: 12px; }
  .row > * { flex: 1; }
  button { padding: 10px 18px; border: none; border-radius: 6px; cursor: pointer; font-size: 0.9em; font-weight: 600; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { opacity: 0.85; }
  .btn-danger { background: var(--red); color: #fff; font-size: 0.8em; padding: 6px 12px; }
  .btn-danger:hover { opacity: 0.85; }
  .btn-secondary { background: var(--border); color: var(--text); }
  #outcome-selector { margin-top: 10px; }
  .outcomes-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
  .outcome-btn { padding: 8px 16px; border: 2px solid var(--border); border-radius: 6px; background: transparent; color: var(--text); cursor: pointer; }
  .outcome-btn.selected { border-color: var(--accent); background: rgba(108,92,231,0.15); }
  table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
  th { text-align: left; padding: 8px 6px; color: var(--muted); border-bottom: 1px solid var(--border); font-weight: 500; }
  td { padding: 8px 6px; border-bottom: 1px solid var(--border); }
  .active { color: var(--green); }
  .expired { color: var(--red); }
  .stopped { color: var(--muted); }
  .price { font-family: monospace; }
  .error-text { color: var(--red); font-size: 0.8em; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75em; font-weight: 600; }
  .badge-green { background: rgba(0,184,148,0.2); color: var(--green); }
  .badge-red { background: rgba(214,48,49,0.2); color: var(--red); }
  .badge-grey { background: rgba(99,110,114,0.2); color: var(--muted); }
  #market-info { margin-top: 10px; font-weight: 600; }
</style>
</head>
<body>

<h1>Predict.fun — Farming Bot</h1>

<!-- New session form -->
<div class="card">
  <h2>Нова сесія</h2>
  <label>Посилання на подію</label>
  <div class="row">
    <input type="text" id="market-url" placeholder="https://predict.fun/market/...">
    <button class="btn-secondary" onclick="resolveMarket()" style="flex:0 0 auto; width:140px;">Завантажити</button>
  </div>
  <div id="market-info"></div>
  <div id="outcome-selector" style="display:none;">
    <label>Outcome</label>
    <div class="outcomes-row" id="outcomes-row"></div>
  </div>

  <div class="row" style="margin-top:10px;">
    <div>
      <label>Сторона</label>
      <select id="side">
        <option value="0">BUY</option>
        <option value="1">SELL</option>
      </select>
    </div>
    <div>
      <label>Шейрсів</label>
      <input type="number" id="shares" value="200" min="1">
    </div>
    <div>
      <label>Макс. спред (¢)</label>
      <input type="number" id="max-spread" value="3" min="1" max="50">
    </div>
  </div>

  <div class="row">
    <div>
      <label>Час початку матчу (UTC)</label>
      <input type="datetime-local" id="cutoff-utc">
    </div>
    <div>
      <label>Зупинити за N хв до</label>
      <input type="number" id="cutoff-minutes" value="30" min="0">
    </div>
  </div>

  <div style="margin-top:16px;">
    <button class="btn-primary" onclick="createSession()">Запустити фарм</button>
  </div>
</div>

<!-- Active sessions table -->
<div class="card">
  <h2>Активні сесії</h2>
  <table>
    <thead>
      <tr>
        <th>ID</th>
        <th>Подія / Outcome</th>
        <th>Сторона</th>
        <th>Ордер (¢)</th>
        <th>Bid / Ask</th>
        <th>Статус</th>
        <th></th>
      </tr>
    </thead>
    <tbody id="sessions-body">
      <tr><td colspan="7" style="text-align:center;color:var(--muted);">Немає активних сесій</td></tr>
    </tbody>
  </table>
</div>

<script>
let selectedOutcome = null;
let marketData = null;

async function resolveMarket() {
  const url = document.getElementById('market-url').value.trim();
  if (!url) return;
  const info = document.getElementById('market-info');
  info.textContent = 'Завантаження...';
  try {
    const resp = await fetch('/api/resolve-market?url=' + encodeURIComponent(url));
    const data = await resp.json();
    if (data.error) { info.textContent = 'Помилка: ' + data.error; return; }
    marketData = data;
    info.textContent = data.title;
    const row = document.getElementById('outcomes-row');
    row.innerHTML = '';
    selectedOutcome = null;
    data.outcomes.forEach((o, i) => {
      const btn = document.createElement('button');
      btn.className = 'outcome-btn';
      btn.textContent = o.name;
      btn.onclick = () => selectOutcome(i);
      row.appendChild(btn);
    });
    document.getElementById('outcome-selector').style.display = 'block';
  } catch(e) { info.textContent = 'Помилка: ' + e.message; }
}

function selectOutcome(idx) {
  selectedOutcome = marketData.outcomes[idx];
  document.querySelectorAll('.outcome-btn').forEach((b, i) => {
    b.classList.toggle('selected', i === idx);
  });
}

async function createSession() {
  if (!selectedOutcome) { alert('Оберіть outcome'); return; }
  const cutoffInput = document.getElementById('cutoff-utc').value;
  const body = {
    market_id: selectedOutcome.market_id,
    market_title: marketData.title,
    token_id: selectedOutcome.on_chain_id,
    outcome_name: selectedOutcome.name,
    side: parseInt(document.getElementById('side').value),
    shares: parseFloat(document.getElementById('shares').value),
    max_spread_cents: parseInt(document.getElementById('max-spread').value),
    cutoff_utc: cutoffInput ? cutoffInput + ':00' : null,
    cutoff_minutes_before: parseInt(document.getElementById('cutoff-minutes').value),
    is_neg_risk: selectedOutcome.is_neg_risk,
    is_yield_bearing: selectedOutcome.is_yield_bearing,
    fee_rate_bps: selectedOutcome.fee_rate_bps,
  };
  try {
    const resp = await fetch('/api/sessions', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const data = await resp.json();
    if (data.error) { alert('Помилка: ' + data.error); return; }
    refreshSessions();
  } catch(e) { alert('Помилка: ' + e.message); }
}

async function stopSession(id) {
  if (!confirm('Зупинити сесію ' + id + '?')) return;
  await fetch('/api/sessions/' + id, { method: 'DELETE' });
  refreshSessions();
}

async function refreshSessions() {
  try {
    const resp = await fetch('/api/sessions');
    const data = await resp.json();
    const tbody = document.getElementById('sessions-body');
    if (!data.sessions.length) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);">Немає активних сесій</td></tr>';
      return;
    }
    tbody.innerHTML = data.sessions.map(s => {
      let statusClass = 'stopped';
      let statusText = 'Зупинено';
      let badge = 'badge-grey';
      if (s.active && !s.is_expired) { statusClass = 'active'; statusText = 'Активна'; badge = 'badge-green'; }
      else if (s.is_expired) { statusClass = 'expired'; statusText = 'Дедлайн'; badge = 'badge-red'; }
      const errHtml = s.error ? '<br><span class="error-text">' + s.error + '</span>' : '';
      return '<tr>' +
        '<td>' + s.session_id + '</td>' +
        '<td>' + s.market_title + '<br><b>' + s.outcome_name + '</b></td>' +
        '<td>' + s.side + '</td>' +
        '<td class="price">' + (s.current_order_price_cents ?? '—') + '¢</td>' +
        '<td class="price">' + (s.last_top_bid ?? '—') + ' / ' + (s.last_top_ask ?? '—') + '</td>' +
        '<td><span class="badge ' + badge + '">' + statusText + '</span>' + errHtml + '</td>' +
        '<td>' + (s.active ? '<button class="btn-danger" onclick="stopSession(\'' + s.session_id + '\')">Стоп</button>' : '') + '</td>' +
        '</tr>';
    }).join('');
  } catch(e) { console.error(e); }
}

// Auto-refresh every 3 seconds
setInterval(refreshSessions, 3000);
refreshSessions();
</script>

</body>
</html>
"""
