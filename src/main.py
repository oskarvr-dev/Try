"""
Congress Trader - Spiegelt Positionen aktiver Kongressmitglieder
Datenquelle: quiverquant.com (kostenlose API)
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ── Alpaca Hilfsfunktionen ─────────────────────────────────────────────────────

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
        "symbol":   symbol,
        "notional": str(round(notional, 2)),
        "side":     side,
        "type":     "market",
        "time_in_force": "day"
    }
    print(f"  Order: {side.upper()} {symbol} fuer ${notional:.2f}")
    return alpaca_post("/orders", payload)

def is_market_open():
    data = alpaca_get("/clock")
    return data.get("is_open", False)

# ── Congress Trades API (Quiver Quantitative) ──────────────────────────────────

def fetch_congress_trades():
    """
    Lädt Kongress-Trades der letzten 12 Monate von der
    öffentlichen Quiver Quantitative API.
    """
    print("Lade Kongress-Trades von quiverquant.com ...")
    url = "https://api.quiverquant.com/beta/live/congresstrading"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        trades = resp.json()
        print(f"  {len(trades)} Trades geladen")
        return trades
    except Exception as e:
        print(f"  Fehler: {e}")
        # Fallback: Capitol Trades JSON-Endpunkt
        try:
            url2 = "https://www.capitoltrades.com/api/trades?pageSize=500"
            resp2 = requests.get(url2, headers=HEADERS, timeout=20)
            resp2.raise_for_status()
            data = resp2.json()
            trades = data.get("trades", data) if isinstance(data, dict) else data
            print(f"  {len(trades)} Trades geladen (Fallback)")
            return trades
        except Exception as e2:
            print(f"  Fallback fehlgeschlagen: {e2}")
            return []

def rank_politicians(trades):
    """
    Gruppiert Trades nach Politiker und berechnet einen Score
    basierend auf Kauf-Aktivität der letzten 12 Monate.
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)
    recent_cutoff = datetime.date.today() - datetime.timedelta(days=90)

    politicians = defaultdict(lambda: {
        "name": "",
        "trades": [],
        "buy_count": 0,
        "sell_count": 0,
        "recent_buys": []
    })

    for t in trades:
        # Quiver Quantitative Feldnamen
        name      = t.get("Representative") or t.get("politician") or t.get("name", "Unbekannt")
        ticker    = (t.get("Ticker") or t.get("ticker") or t.get("asset", "")).upper().strip()
        tx_type   = (t.get("Transaction") or t.get("type") or t.get("transaction", "")).lower()
        date_str  = t.get("TransactionDate") or t.get("date") or t.get("traded", "")[:10]

        if not ticker or not ticker.isalpha():
            continue
        try:
            trade_date = datetime.date.fromisoformat(date_str[:10])
        except Exception:
            continue
        if trade_date < cutoff:
            continue

        pol = politicians[name]
        pol["name"] = name
        pol["trades"].append({
            "date": date_str[:10],
            "ticker": ticker,
            "type": tx_type
        })

        is_buy = "purchase" in tx_type or "buy" in tx_type
        is_sell = "sell" in tx_type or "sale" in tx_type

        if is_buy:
            pol["buy_count"] += 1
            if trade_date >= recent_cutoff:
                pol["recent_buys"].append(ticker)
        elif is_sell:
            pol["sell_count"] += 1

    # Score berechnen & sortieren
    result = []
    for name, pol in politicians.items():
        total = pol["buy_count"] + pol["sell_count"]
        if total == 0:
            continue
        score = (pol["buy_count"] - pol["sell_count"] * 0.5) / total * 100
        pol["score"]       = round(score, 1)
        pol["trade_count"] = total
        pol["recent_buys"] = list(dict.fromkeys(pol["recent_buys"]))[:5]
        pol["last_5_trades"] = sorted(pol["trades"], key=lambda x: x["date"], reverse=True)[:5]
        result.append(pol)

    result.sort(key=lambda x: x["score"], reverse=True)
    print(f"  {len(result)} Politiker ausgewertet")
    return result

# ── Portfolio-Spiegelung ───────────────────────────────────────────────────────

def mirror_positions(top_politicians, account):
    portfolio_value = float(account["portfolio_value"])
    budget_per_pol  = portfolio_value * MAX_POSITION_PCT
    orders = []
    existing = {p["symbol"]: p for p in get_positions()}

    for pol in top_politicians[:TOP_N_POLITICIANS]:
        print(f"\nSpiegele Positionen von {pol['name']} ...")
        tickers = pol["recent_buys"] or [t["ticker"] for t in pol["last_5_trades"] if "buy" in t["type"] or "purchase" in t["type"]][:5]
        if not tickers:
            print(f"  Keine Kauf-Trades gefunden - ueberspringe")
            continue
        budget_per_ticker = budget_per_pol / len(tickers)
        for ticker in tickers:
            if ticker in existing:
                print(f"  {ticker} bereits im Portfolio - ueberspringe")
                continue
            try:
                order = place_order(ticker, budget_per_ticker)
                orders.append({"politician": pol["name"], "ticker": ticker, "order": order})
                time.sleep(0.5)
            except Exception as e:
                print(f"  Order fuer {ticker} fehlgeschlagen: {e}")
                orders.append({"politician": pol["name"], "ticker": ticker, "error": str(e)})

    return orders

# ── E-Mail ─────────────────────────────────────────────────────────────────────

def send_email(subject, body_html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body_html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print(f"E-Mail gesendet an {EMAIL_TO}")

def build_email(politicians, orders, account, market_open=True):
    today = datetime.date.today().strftime("%d.%m.%Y")
    rows_pol = ""
    for i, p in enumerate(politicians[:TOP_N_POLITICIANS], 1):
        last5 = " | ".join([
            f"{t['date']} <b>{t['ticker']}</b> ({t['type']})"
            for t in p.get("last_5_trades", [])
        ]) or "-"
        rows_pol += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center">{i}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee"><b>{p['name']}</b></td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center">{p['trade_count']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center">{p['score']}%</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px">{last5}</td>
        </tr>"""

    rows_orders = ""
    for o in orders:
        status = "✅ Ausgefuehrt" if "order" in o else f"❌ {o.get('error','Fehler')}"
        rows_orders += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #eee">{o['politician']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee"><b>{o['ticker']}</b></td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee">{status}</td>
        </tr>"""

    status_badge = (
        '<span style="background:#22c55e;color:white;padding:3px 10px;border-radius:20px;font-size:12px">Markt offen</span>'
        if market_open else
        '<span style="background:#94a3b8;color:white;padding:3px 10px;border-radius:20px;font-size:12px">Markt geschlossen</span>'
    )

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:750px;margin:auto;color:#333;">
      <h2 style="background:#1a1a2e;color:white;padding:16px 24px;border-radius:8px;margin-bottom:4px;">
        Congress Trader - Tagesbericht {today}
      </h2>
      <p style="margin:0 0 20px">{status_badge}</p>

      <h3>Konto-Uebersicht</h3>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px">
        <tr><td style="padding:6px 0"><b>Portfolio-Wert</b></td><td>${float(account['portfolio_value']):,.2f}</td></tr>
        <tr><td style="padding:6px 0"><b>Verfuegbares Kapital</b></td><td>${float(account['buying_power']):,.2f}</td></tr>
        <tr><td style="padding:6px 0"><b>Unrealisierter P&L</b></td><td>${float(account.get('unrealized_pl',0)):,.2f}</td></tr>
      </table>

      <h3>Top {TOP_N_POLITICIANS} Kongressmitglieder (letzte 12 Monate)</h3>
      <table style="width:100%;border-collapse:collapse;border:1px solid #eee;margin-bottom:20px">
        <thead><tr style="background:#f5f5f5;">
          <th style="padding:8px 12px">#</th>
          <th style="padding:8px 12px">Name</th>
          <th style="padding:8px 12px">Trades</th>
          <th style="padding:8px 12px">Score</th>
          <th style="padding:8px 12px">Letzte 5 Trades</th>
        </tr></thead>
        <tbody>{rows_pol if rows_pol else '<tr><td colspan="5" style="padding:16px;text-align:center;color:#888">Keine Daten verfuegbar</td></tr>'}</tbody>
      </table>

      <h3>Heutige Orders</h3>
      {"<p>Keine neuen Orders heute.</p>" if not orders else
        '<table style="width:100%;border-collapse:collapse;border:1px solid #eee"><thead><tr style="background:#f5f5f5"><th style="padding:8px 12px">Politiker</th><th style="padding:8px 12px">Ticker</th><th style="padding:8px 12px">Status</th></tr></thead><tbody>' + rows_orders + '</tbody></table>'}

      <p style="color:#888;font-size:12px;margin-top:32px;border-top:1px solid #eee;padding-top:12px">
        Congress Trader - Alpaca Paper Trading - Automatisch generiert
      </p>
    </body></html>"""
    return html

# ── Hauptprogramm ──────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"Congress Trader - {datetime.datetime.now():%d.%m.%Y %H:%M}")
    print("=" * 60)

    account = get_account()
    print(f"Portfolio: ${float(account['portfolio_value']):,.2f} | "
          f"Buying Power: ${float(account['buying_power']):,.2f}")

    market_open = is_market_open()

    # Politiker-Daten immer laden (auch wenn Markt zu)
    raw_trades = fetch_congress_trades()
    politicians = rank_politicians(raw_trades)

    if politicians:
        print(f"\nTop 5 Politiker:")
        for i, p in enumerate(politicians[:5], 1):
            print(f"  {i}. {p['name']} - Score {p['score']}%  "
                  f"Letzte Kaeufe: {', '.join(p['recent_buys'][:3]) or '-'}")

    orders = []
    if market_open:
        orders = mirror_positions(politicians, account)
        account = get_account()  # Aktualisiertes Konto nach Orders
    else:
        print("Markt ist geschlossen - kein Handel heute.")

    send_email(
        f"Congress Trader {datetime.date.today():%d.%m.%Y} - {'Markt offen' if market_open else 'Markt geschlossen'}",
        build_email(politicians[:TOP_N_POLITICIANS], orders, account, market_open)
    )

    print("\nFertig!")

if __name__ == "__main__":
    main()
