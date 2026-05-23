"""
Standalone P&L worker — run as subprocess by backend.py to avoid
ib_insync/asyncio event loop conflicts with uvicorn.
Prints a single JSON object to stdout.
"""
import sys, os, json, math, time
sys.path.insert(0, os.path.expanduser("~/prometheus/report/dashboard"))

BASE   = os.path.expanduser("~/prometheus")
ACCT_A = os.path.join(BASE, "data", "account_a")

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(BASE, ".env"))
except ImportError:
    pass


def load(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default


def safe_float(val, default=0.0):
    try:
        v = float(val)
        return default if math.isnan(v) else v
    except Exception:
        return default


def load_open(data_dir, account_label):
    positions = load(os.path.join(data_dir, "open_positions.json"), [])
    for p in positions:
        p.setdefault("_account", account_label)
    return positions


def fetch_prices(open_pos, port, account_label, client_id, results):
    if not open_pos:
        return
    from ib_insync import IB, Stock
    ib = IB()
    connected = False
    try:
        ib.connect("127.0.0.1", port, clientId=client_id, timeout=10)
        ib.reqMarketDataType(4)
        connected = True

        for p in open_pos:
            ticker    = p.get("ticker", "")
            direction = p.get("direction", "LONG")
            entry_px  = safe_float(p.get("entry_price"))
            qty       = int(p.get("entry_qty", 0))
            size_usd  = safe_float(p.get("entry_size_usd"))
            stop      = p.get("calculated_stop")

            current = None
            source  = None
            try:
                contract = Stock(ticker, "SMART", "USD")
                ib.qualifyContracts(contract)
                td = ib.reqMktData(contract, "", False, False)
                ib.sleep(3)
                for attr in ["last", "close", "bid"]:
                    v = getattr(td, attr, None)
                    if v and not math.isnan(v) and v > 0:
                        current = round(float(v), 2)
                        source  = attr
                        break
            except Exception:
                pass

            # yfinance fallback for display-only (e.g. Account A paper subscription gap)
            if current is None:
                try:
                    import yfinance as yf
                    fi = yf.Ticker(ticker).fast_info
                    p_yf = getattr(fi, "last_price", None) or getattr(fi, "previous_close", None)
                    if p_yf and not math.isnan(float(p_yf)) and float(p_yf) > 0:
                        current = round(float(p_yf), 2)
                        source  = "yfinance"
                except Exception:
                    pass

            if current and entry_px:
                raw = (current - entry_px) / entry_px * 100
                if direction == "SHORT":
                    raw = -raw
                pnl_pct = round(raw, 2)
                pnl_usd = round(raw / 100 * size_usd, 2)
                stop_f  = safe_float(stop) if stop else None
                dist    = round(abs(current - stop_f) / current * 100, 2) if stop_f else None
            else:
                pnl_pct = pnl_usd = dist = None

            results.append({
                "ticker":           ticker,
                "account":          account_label,
                "direction":        direction,
                "conviction":       p.get("conviction", ""),
                "sector":           p.get("sector", ""),
                "entry_price":      entry_px,
                "current_price":    current,
                "price_source":     source,
                "qty":              qty,
                "size_usd":         size_usd,
                "pnl_pct":          pnl_pct,
                "pnl_usd":          pnl_usd,
                "calculated_stop":  safe_float(stop) if stop else None,
                "dist_to_stop_pct": dist,
                "entry_date":       p.get("entry_date", ""),
                "deadline_date":    p.get("deadline_date", ""),
                "available":        current is not None,
            })

    except Exception as e:
        for p in open_pos:
            results.append({
                "ticker":           p.get("ticker", ""),
                "account":          account_label,
                "direction":        p.get("direction", "LONG"),
                "conviction":       p.get("conviction", ""),
                "sector":           p.get("sector", ""),
                "entry_price":      safe_float(p.get("entry_price")),
                "current_price":    None,
                "price_source":     None,
                "qty":              int(p.get("entry_qty", 0)),
                "size_usd":         safe_float(p.get("entry_size_usd")),
                "pnl_pct":          None,
                "pnl_usd":          None,
                "calculated_stop":  None,
                "dist_to_stop_pct": None,
                "entry_date":       p.get("entry_date", ""),
                "deadline_date":    p.get("deadline_date", ""),
                "available":        False,
                "error":            str(e) or "Gateway unreachable",
            })
    finally:
        if connected:
            try: ib.disconnect()
            except: pass


def main():
    from datetime import datetime
    results = []
    open_a  = load_open(ACCT_A, "A")

    fetch_prices(open_a, 4002, "A", 20, results)

    priced        = [r for r in results if r["available"]]
    total_pnl_usd = round(sum(r["pnl_usd"] or 0 for r in priced), 2)
    total_pnl_pct = round(sum(r["pnl_pct"] or 0 for r in priced), 2)

    print(json.dumps({
        "positions":     results,
        "total_pnl_usd": total_pnl_usd,
        "total_pnl_pct": total_pnl_pct,
        "priced_count":  len(priced),
        "total_count":   len(results),
        "fetched_at":    datetime.now().isoformat(),
    }))


if __name__ == "__main__":
    main()
