"""
Prometheus Dashboard — Backend API (v3 — single-account, SQLite-backed)

FastAPI server. Reads live state from data/account_a/*.json and historical
data from data/prometheus.db. Live IBKR fetch is still in pnl_worker.py
(unchanged) so per-position P&L stays real-time.

Run from repo root:
    python3 -m uvicorn report.dashboard.backend:app --host 0.0.0.0 --port 8080

Endpoints:
    GET  /api/metrics      latest snapshot + lifetime aggregates
    GET  /api/positions/open    current open positions (live JSON)
    GET  /api/positions/closed  closed trades from SQLite history
    GET  /api/activity     recent trade events from SQLite
    GET  /api/risk         live risk read from current open positions
    GET  /api/sectors      research output (sector rankings)
    GET  /api/performance  equity curve from daily_snapshots
    GET  /api/pnl          live IBKR floating P&L (subprocess to avoid event-loop conflicts)
    GET  /                 static dashboard HTML
"""
import json
import os
import sys
import math
from datetime import datetime
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

# Make report.db importable when this file is run directly via uvicorn.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from report import db as repo_db
from report import positions_loader


app = FastAPI(title="Prometheus Dashboard API (v3)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE          = os.path.expanduser("~/promv2")
DATA_A        = os.path.join(BASE, "data", "account_a")
RESEARCH_DATA = os.path.join(BASE, "trading", "research", "data")
ACCOUNT_CODE  = "A"


def load(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default


def safe_float(v, default=0.0):
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except Exception:
        return default


# ── /api/metrics — latest snapshot + lifetime aggregates ─────────────────────
@app.get("/api/metrics")
def get_metrics():
    conn = repo_db.connect()
    try:
        snap_rows = repo_db.daily_snapshots(conn, account=ACCOUNT_CODE, limit=1)
        snap = snap_rows[0] if snap_rows else {}

        # Lifetime aggregates from closed_trades
        closed = conn.execute(
            "SELECT pnl_pct, pnl_usd FROM closed_trades WHERE account=?",
            (ACCOUNT_CODE,)
        ).fetchall()
        pnls   = [r["pnl_pct"] or 0.0 for r in closed]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        avg_w  = (sum(wins) / len(wins)) if wins else 0
        avg_l  = (sum(abs(p) for p in losses) / len(losses)) if losses else 0

        return {
            "snapshot_date":     snap.get("snapshot_date"),
            "currency":          snap.get("currency"),
            "account_value":     snap.get("account_value"),
            "open_positions":    snap.get("open_trades", 0),
            "total_unrealized":  snap.get("total_unrealized_usd", 0),
            "unrealized_pct":    snap.get("unrealized_pct_account", 0),
            "live_risk_usd":     snap.get("live_risk_usd", 0),
            "budgeted_risk_usd": snap.get("budgeted_risk_usd", 0),
            "closed_trades":     len(closed),
            "win_rate":          round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "avg_win_pct":       round(avg_w, 2),
            "avg_loss_pct":      round(avg_l, 2),
            "win_loss_ratio":    round(avg_w / avg_l, 2) if avg_l else 0,
            "max_drawdown_pct":  round(abs(min(pnls)), 1) if pnls else 0,
            "targets": {"win_rate": 50, "win_loss_ratio": 1.5, "max_drawdown": 15},
        }
    finally:
        conn.close()


# ── /api/positions/open — live JSON ──────────────────────────────────────────
@app.get("/api/positions/open")
def get_open_positions():
    rows = load(os.path.join(DATA_A, "open_positions.json"), [])
    out = []
    for p in rows:
        out.append({
            "ticker":            p.get("ticker", ""),
            "direction":         p.get("direction", ""),
            "conviction":        p.get("conviction", ""),
            "sector":            p.get("sector", ""),
            "entry_date":        p.get("entry_date", ""),
            "entry_price":       safe_float(p.get("entry_price")),
            "entry_qty":         p.get("entry_qty", 0),
            "entry_size_usd":    safe_float(p.get("entry_size_usd")),
            "position_size_pct": safe_float(p.get("position_size_pct")),
            "deadline_date":     p.get("deadline_date", ""),
            "core_thesis":       p.get("core_thesis", ""),
            "catalyst":          p.get("catalyst", ""),
            "invalidation_conditions": p.get("invalidation_conditions", ""),
            "calculated_stop":   p.get("calculated_stop"),
            "paper_trade":       p.get("paper_trade", True),
            "account":           ACCOUNT_CODE,
        })
    return out


# ── /api/positions/closed — from SQLite history ──────────────────────────────
@app.get("/api/positions/closed")
def get_closed_positions(limit: int = 200):
    conn = repo_db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM closed_trades WHERE account=? ORDER BY exit_date DESC LIMIT ?",
            (ACCOUNT_CODE, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── /api/activity — events log ───────────────────────────────────────────────
@app.get("/api/activity")
def get_activity(limit: int = 50):
    conn = repo_db.connect()
    try:
        return repo_db.recent_events(conn, account=ACCOUNT_CODE, limit=limit)
    finally:
        conn.close()


# ── /api/risk — computed live from current open positions ────────────────────
@app.get("/api/risk")
def get_risk():
    open_p = load(os.path.join(DATA_A, "open_positions.json"), [])
    sector_exp = {}
    for p in open_p:
        sector = p.get("sector", "Unknown")
        key    = sector.split("(")[0].strip() if "(" in sector else sector
        sector_exp[key] = sector_exp.get(key, 0) + safe_float(p.get("position_size_pct"))
    largest   = max((safe_float(p.get("position_size_pct")) for p in open_p), default=0)
    net_delta = sum(
        safe_float(p.get("position_size_pct")) * (1 if p.get("direction") == "LONG" else -1)
        for p in open_p
    )
    return {
        "open_count":       len(open_p),
        "largest_position": round(largest, 1),
        "net_delta":        round(abs(net_delta), 1),
        "sector_exposure":  {k: round(v, 1) for k, v in sector_exp.items()},
        "limits": {"max_position": 5, "max_sector": 20, "max_positions": 5, "max_delta": 30},
    }


# ── /api/sectors — research output ───────────────────────────────────────────
@app.get("/api/sectors")
def get_sectors():
    data = load(os.path.join(RESEARCH_DATA, "sector_ranking.json"), {})
    return {
        "generated_at": data.get("generated_at", ""),
        "sectors":      data.get("all_sectors", []),
        "top":          data.get("top_sectors", []),
        "bottom":       data.get("bottom_sectors", []),
    }


# ── /api/performance — equity curve from daily snapshots ─────────────────────
@app.get("/api/performance")
def get_performance(limit: int = 365):
    conn = repo_db.connect()
    try:
        # Snapshots are ordered DESC by date in db helper; reverse for curve.
        snaps = repo_db.daily_snapshots(conn, account=ACCOUNT_CODE, limit=limit)
        snaps = list(reversed(snaps))

        # Also include realized PnL per closed trade (for per-trade markers)
        closed = conn.execute(
            "SELECT exit_date, ticker, pnl_pct FROM closed_trades WHERE account=? "
            "ORDER BY exit_date ASC", (ACCOUNT_CODE,)
        ).fetchall()
        cum = 0.0
        per_trade = []
        for r in closed:
            cum += r["pnl_pct"] or 0.0
            per_trade.append({
                "date":           r["exit_date"],
                "ticker":         r["ticker"],
                "pnl_pct":        round(r["pnl_pct"] or 0.0, 2),
                "cumulative_pct": round(cum, 2),
            })

        return {
            "snapshots":  snaps,
            "per_trade":  per_trade,
        }
    finally:
        conn.close()


# ── /api/pnl — live IBKR floating P&L (unchanged worker, updated path) ──────
@app.get("/api/pnl")
def get_pnl():
    import subprocess
    worker = os.path.join(BASE, "report", "dashboard", "pnl_worker.py")
    result = subprocess.run(
        [sys.executable, worker],
        capture_output=True, text=True, timeout=90
    )
    if result.returncode != 0:
        return {"error": result.stderr[:500], "positions": [], "total_pnl_usd": 0,
                "total_pnl_pct": 0, "priced_count": 0, "total_count": 0,
                "fetched_at": datetime.now().isoformat()}
    return json.loads(result.stdout)


# ── Static dashboard ─────────────────────────────────────────────────────────
_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/")
def serve_dashboard():
    return FileResponse(os.path.join(_STATIC, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
