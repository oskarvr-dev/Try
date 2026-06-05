"""
Congress Trader - Spiegelt Positionen aktiver Kongressmitglieder
Datenquelle: Senate Stock Watcher (kostenlos, kein Login)
Broker: Alpaca Paper Trading
"""

import os
import time
import smtplib
import requests
import datetime
from collections import defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── Konfiguration ──────────────────────────────────────────────────────────────
ALPACA_KEY      = os.environ["ALPACA_KEY"]
ALPACA_SECRET   = os.environ["ALPACA_SECRET"]
ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"

EMAIL_FROM      = os.environ["EMAIL_ADDRESS"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_TO        = os.environ.get("EMAIL_TO", EMAIL_FROM)

TOP_N_POLITICIANS  = 3
MAX_POSITION_PCT   = 0.05
LOOKBACK_DAYS      = 365

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Datenquellen (werden der Reihe nach versucht)
DATA_SOURCES = [
    "https://senatestockwatcher.com/api",
    "https://housestockwatcher.com/api",
    "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json",
]

# ── Alpaca ─────────────────────────────────────────────────────────────────────

def alpaca_get(endpoint):
    r = requests.get(
        f"{ALPACA_BASE_URL}{endpoint}",
        headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
        timeout=15
    )
    r.raise_for_status()
    return r.json()

def alpaca_post(endpoint, payload):
    r = requests.post(
        f"{ALPACA_BASE_URL}{endpoint}",
        json=payload,
        headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
        timeout=15
    )
    r.raise_for_status()
    return r.json()

def get_account():
    return alpaca_get("/account")

def get_positions():
    return alpaca_get("/positions")

def place_order(symbol, notional, side="buy"):
    payload = {
        "symbol": symbol,
        "notional": str(round(notional, 2)),
        "side": side,
        "type": "market",
        "time_in_force": "day"
    }
    print(f"  Order: {side.upper()} {symbol} fuer ${notional:.2f}")
    return alpaca_post("/orders", payload)

def is_market_open():
    return alpaca_get("/clock").get("is_open", False)

# ── Daten laden ────────────────────────────────────────────────────────────────

def fetch_trades():
    for url in DATA_SOURCES:
        print(f"Versuche: {url}")
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            # Senate Stock Watcher gibt Liste von Personen-Objekten zurueck
            trades = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "transactions" in item:
                        # Format: [{first_name, last_name, transactions: [...]}]
                        name = f"{item.get('first_name','')} {item.get('last_name','')}".strip()
                        for tx in item.get("transactions", []):
                            tx["senator"] = name
                            trades.append(tx)
                    elif isinstance(item, dict) and "ticker" in item:
                        # Flaches Format
                        trades.append(item)
            print(f"  {len(trades)} Trades geladen")
            if trades:
                return trades
        except Exception as e:
            print(f"  Fehler: {e}")
    return []

def rank_politicians(trades):
    cutoff = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)
    recent_cutoff = datetime.date.today() - datetime.timedelta(days=90)
    pols = defaultdict(lambda: {"name": "", "trades": [], "buy_count": 0, "sell_count": 0, "recent_buys": []})

    for t in trades:
        # Verschiedene Feldnamen abdecken
        name   = (t.get("senator") or t.get("representative") or
                  t.get("Representative") or t.get("name") or "Unbekannt").strip()
        ticker = (t.get("ticker") or t.get("Ticker") or t.get("asset_ticker") or "").upper().strip()
        txtype = (t.get("type") or t.get("transaction_type") or
                  t.get("Transaction") or "").lower()
        date_s = (t.get("transaction_date") or t.get("TransactionDate") or
                  t.get("date") or t.get("disclosure_date") or "")[:10]

        if not ticker or not ticker.replace(".", "").isalpha() or len(ticker) > 5:
            continue
        try:
            # Format MM/DD/YYYY oder YYYY-MM-DD
            if "/" in date_s:
                parts = date_s.split("/")
                td = datetime.date(int(parts[2]), int(parts[0]), int(parts[1]))
            else:
                td = datetime.date.fromisoformat(date_s)
        except Exception:
            continue
        if td < cutoff:
            continue

        p = pols[name]
        p["name"] = name
        p["trades"].append({"date": date_s, "ticker": ticker, "type": txtype})

        is_buy = any(w in txtype for w in ["purchase", "buy", "bought"])
        is_sell = any(w in txtype for w in ["sale", "sell", "sold"])

        if is_buy:
            p["buy_count"] += 1
            if td >= recent_cutoff:
                p["recent_buys"].append(ticker)
        elif is_sell:
            p["sell_count"] += 1

    result = []
    for name, p in pols.items():
        total = p["buy_count"] + p["sell_count"]
        if total < 2:
            continue
        score = (p["buy_count"] - p["sell_count"] * 0.5) / total * 100
        result.append({
            "name": name,
            "score": round(score, 1),
            "trade_count": total,
            "buy_count": p["buy_count"],
            "recent_buys": list(dict.fromkeys(p["recent_buys"]))[:5],
            "last_5_trades": sorted(p["trades"], key=lambda x: x["date"], reverse=True)[:5]
        })

    result.sort(key=lambda x: (x["score"], x["trade_count"]), reverse=True)
    print(f"  {len(result)} Politiker ausgewertet")
    return result

# ── Portfolio spiegeln ─────────────────────────────────────────────────────────

def mirror_positions(politicians, account):
    portfolio_value = float(account["portfolio_value"])
    budget = portfolio_value * MAX_POSITION_PCT
    orders = []
    existing = {p["symbol"] for p in get_positions()}

    for pol in politicians[:TOP_N_POLITICIANS]:
        tickers = pol["recent_buys"]
        if not tickers:
            tickers = [t["ticker"] for t in pol["last_5_trades"]
                       if any(w in t["type"] for w in ["purchase", "buy"])][:3]
        if not tickers:
            print(f"  {pol['name']}: keine Kauf-Tickers gefunden")
            continue
        per_ticker = budget / len(tickers)
        print(f"\nSpiegele {pol['name']} ({len(tickers)} Tickers) ...")
        for ticker in tickers:
            if ticker in existing:
                print(f"  {ticker} bereits im Portfolio")
                continue
            try:
                order = place_order(ticker, per_ticker)
                orders.append({"politician": pol["name"], "ticker": ticker, "order": order})
                time.sleep(0.5)
            except Exception as e:
                print(f"  {ticker} fehlgeschlagen: {e}")
                orders.append({"politician": pol["name"], "ticker": ticker, "error": str(e)})
    return orders

# ── E-Mail ─────────────────────────────────────────────────────────────────────

def send_email(subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(EMAIL_FROM, EMAIL_PASSWORD)
        s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print(f"E-Mail gesendet an {EMAIL_TO}")

def build_email(politicians, orders, account, market_open):
    today = datetime.date.today().strftime("%d.%m.%Y")

    rows_pol = ""
    for i, p in enumerate(politicians[:TOP_N_POLITICIANS], 1):
        last5 = " &nbsp;|&nbsp; ".join(
            f"{t['date']} <b>{t['ticker']}</b> ({t['type']})"
            for t in p.get("last_5_trades", [])
        ) or "-"
        rows_pol += f"""<tr>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">{i}</td>
          <td style="padding:8px;border-bottom:1px solid #eee"><b>{p['name']}</b></td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">{p['trade_count']}</td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">{p['score']}%</td>
          <td style="padding:8px;border-bottom:1px solid #eee;font-size:12px">{last5}</td>
        </tr>"""

    rows_ord = ""
    for o in orders:
        st = "Ausgefuehrt" if "order" in o else f"Fehler: {o.get('error','')}"
        rows_ord += f"""<tr>
          <td style="padding:8px;border-bottom:1px solid #eee">{o['politician']}</td>
          <td style="padding:8px;border-bottom:1px solid #eee"><b>{o['ticker']}</b></td>
          <td style="padding:8px;border-bottom:1px solid #eee">{st}</td>
        </tr>"""

    badge = ('<span style="background:#22c55e;color:white;padding:3px 10px;border-radius:20px;font-size:12px">Markt offen</span>'
             if market_open else
             '<span style="background:#94a3b8;color:white;padding:3px 10px;border-radius:20px;font-size:12px">Markt geschlossen</span>')

    no_data = '<tr><td colspan="5" style="padding:16px;text-align:center;color:#888">Keine Daten verfuegbar</td></tr>'

    return f"""<html><body style="font-family:Arial,sans-serif;max-width:750px;margin:auto;color:#333">
      <h2 style="background:#1a1a2e;color:white;padding:16px 24px;border-radius:8px;margin-bottom:6px">
        Congress Trader - Tagesbericht {today}
      </h2>
      <p>{badge}</p>
      <h3>Konto-Uebersicht</h3>
      <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
        <tr><td style="padding:5px 0"><b>Portfolio-Wert</b></td><td>${float(account['portfolio_value']):,.2f}</td></tr>
        <tr><td style="padding:5px 0"><b>Verfuegbares Kapital</b></td><td>${float(account['buying_power']):,.2f}</td></tr>
        <tr><td style="padding:5px 0"><b>Unrealisierter P&L</b></td><td>${float(account.get('unrealized_pl',0)):,.2f}</td></tr>
      </table>
      <h3>Top {TOP_N_POLITICIANS} Senatoren (letzte 12 Monate)</h3>
      <table style="width:100%;border-collapse:collapse;border:1px solid #eee;margin-bottom:16px">
        <thead><tr style="background:#f5f5f5">
          <th style="padding:8px">#</th><th style="padding:8px">Name</th>
          <th style="padding:8px">Trades</th><th style="padding:8px">Score</th>
          <th style="padding:8px">Letzte 5 Trades</th>
        </tr></thead>
        <tbody>{rows_pol or no_data}</tbody>
      </table>
      <h3>Heutige Orders</h3>
      {"<p>Keine neuen Orders heute.</p>" if not orders else
       f'<table style="width:100%;border-collapse:collapse;border:1px solid #eee"><thead><tr style="background:#f5f5f5"><th style="padding:8px">Politiker</th><th style="padding:8px">Ticker</th><th style="padding:8px">Status</th></tr></thead><tbody>{rows_ord}</tbody></table>'}
      <p style="color:#888;font-size:12px;margin-top:24px;border-top:1px solid #eee;padding-top:12px">
        Congress Trader - Alpaca Paper Trading - Automatisch generiert
      </p>
    </body></html>"""

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"Congress Trader - {datetime.datetime.now():%d.%m.%Y %H:%M}")
    print("=" * 60)

    account = get_account()
    print(f"Portfolio: ${float(account['portfolio_value']):,.2f} | Buying Power: ${float(account['buying_power']):,.2f}")

    market_open = is_market_open()
    trades = fetch_trades()
    politicians = rank_politicians(trades)

    if politicians:
        print("\nTop 5:")
        for i, p in enumerate(politicians[:5], 1):
            print(f"  {i}. {p['name']} Score:{p['score']}% Trades:{p['trade_count']} Kaeufe:{p['recent_buys'][:3]}")

    orders = []
    if market_open and politicians:
        orders = mirror_positions(politicians, account)
        account = get_account()
    else:
        print("Markt geschlossen oder keine Daten - kein Handel.")

    send_email(
        f"Congress Trader {datetime.date.today():%d.%m.%Y} - {'Markt offen' if market_open else 'Markt geschlossen'}",
        build_email(politicians[:TOP_N_POLITICIANS], orders, account, market_open)
    )
    print("\nFertig!")

if __name__ == "__main__":
    main()
