"""
Congress Trader - Spiegelt Positionen aktiver Kongressmitglieder
Datenquelle: capitaltrades.com
Broker: Alpaca Paper Trading
"""

import os
import time
import smtplib
import requests
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup

# ── Konfiguration ──────────────────────────────────────────────────────────────
ALPACA_KEY      = os.environ["ALPACA_KEY"]
ALPACA_SECRET   = os.environ["ALPACA_SECRET"]
ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"

EMAIL_FROM      = os.environ["EMAIL_ADDRESS"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_TO        = os.environ.get("EMAIL_TO", EMAIL_FROM)

CAPITAL_TRADES_URL = "https://www.capitaltrades.com/politicians"
TOP_N_POLITICIANS  = 3
MAX_POSITION_PCT   = 0.05
LOOKBACK_DAYS      = 365

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
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

# ── Capital Trades Scraping ────────────────────────────────────────────────────

def fetch_politicians():
    print("Lade Politikerliste von capitaltrades.com ...")
    try:
        resp = requests.get(CAPITAL_TRADES_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Fehler beim Laden der Seite: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    politicians = []

    rows = soup.select("table tbody tr") or soup.select(".politician-row")
    if not rows:
        rows = [a for a in soup.find_all("a", href=True)
                if "/politicians/" in a["href"] and a["href"].count("/") >= 3]

    seen = set()
    for row in rows[:30]:
        href = row.get("href") if row.name == "a" else None
        if not href:
            link = row.find("a", href=True)
            href = link["href"] if link else None
        if not href:
            continue
        if not href.startswith("http"):
            href = "https://www.capitaltrades.com" + href
        if href in seen:
            continue
        seen.add(href)
        pol = fetch_politician_detail(href)
        if pol:
            politicians.append(pol)
        time.sleep(1)

    politicians.sort(key=lambda x: x.get("return_pct", 0), reverse=True)
    return politicians

def fetch_politician_detail(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Fehler bei {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    name_el = soup.select_one("h1") or soup.select_one(".politician-name")
    name = name_el.get_text(strip=True) if name_el else url.split("/")[-1]

    cutoff = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)
    trades = []
    for row in soup.select("table tbody tr"):
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cols) < 4:
            continue
        try:
            trade_date = datetime.date.fromisoformat(cols[0][:10])
        except Exception:
            continue
        if trade_date < cutoff:
            continue
        trades.append({
            "date":   cols[0][:10],
            "ticker": cols[1].upper().strip(),
            "type":   cols[2].lower(),
            "size":   cols[3]
        })

    if not trades:
        return None

    buy_count  = sum(1 for t in trades if "buy" in t["type"] or "purchase" in t["type"])
    sell_count = sum(1 for t in trades if "sell" in t["type"])
    return_est = (buy_count - sell_count * 0.5) / max(len(trades), 1) * 100

    # Letzte 5 Kaeufe (unabhaengig vom Datum)
    recent_buys = list(dict.fromkeys([
        t["ticker"] for t in trades
        if ("buy" in t["type"] or "purchase" in t["type"])
        and t["ticker"].isalpha()
    ]))[:5]

    last_5_trades = trades[:5]

    print(f"  {name}: {len(trades)} Trades, ~{return_est:.1f}% Score, {len(recent_buys)} letzte Kaeufe")
    return {
        "name":        name,
        "url":         url,
        "return_pct":  return_est,
        "trade_count": len(trades),
        "recent_buys": recent_buys,
        "last_5_trades": last_5_trades
    }

# ── Portfolio-Spiegelung ───────────────────────────────────────────────────────

def mirror_positions(top_politicians, account):
    portfolio_value = float(account["portfolio_value"])
    budget_per_pol  = portfolio_value * MAX_POSITION_PCT
    orders = []
    existing = {p["symbol"]: p for p in get_positions()}

    for pol in top_politicians[:TOP_N_POLITICIANS]:
        print(f"\nSpiegele Positionen von {pol['name']} ...")
        for ticker in pol["recent_buys"][:5]:
            if ticker in existing:
                print(f"  {ticker} bereits im Portfolio - ueberspringe")
                continue
            try:
                order = place_order(ticker, budget_per_pol / max(len(pol["recent_buys"][:5]), 1))
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

def build_email(politicians, orders, account):
    today = datetime.date.today().strftime("%d.%m.%Y")
    rows_pol = ""
    for i, p in enumerate(politicians[:TOP_N_POLITICIANS], 1):
        rows_pol += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;">{i}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;"><b>{p['name']}</b></td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;">{p['trade_count']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px">{' | '.join([f"{t['date']} {t['ticker']} ({t['type']})" for t in p.get('last_5_trades', [])]) or '-'}</td>
        </tr>"""

    rows_orders = ""
    for o in orders:
        status = "Ausgefuehrt" if "order" in o else f"Fehler: {o.get('error','')}"
        rows_orders += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;">{o['politician']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;"><b>{o['ticker']}</b></td>
          <td style="padding:8px 12px;border-bottom:1px solid #eee;">{status}</td>
        </tr>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;color:#333;">
      <h2 style="background:#1a1a2e;color:white;padding:16px 24px;border-radius:8px;">
        Congress Trader - Tagesbericht {today}
      </h2>
      <h3>Konto-Uebersicht</h3>
      <table style="width:100%;border-collapse:collapse;">
        <tr><td><b>Portfolio-Wert</b></td><td>${float(account['portfolio_value']):,.2f}</td></tr>
        <tr><td><b>Verfuegbares Kapital</b></td><td>${float(account['buying_power']):,.2f}</td></tr>
        <tr><td><b>Tages-P&L</b></td><td>${float(account.get('unrealized_pl',0)):,.2f}</td></tr>
      </table>
      <h3>Top {TOP_N_POLITICIANS} Kongressmitglieder (letzte 12 Monate)</h3>
      <table style="width:100%;border-collapse:collapse;border:1px solid #eee;">
        <thead><tr style="background:#f5f5f5;">
          <th style="padding:8px 12px;">#</th>
          <th style="padding:8px 12px;">Name</th>
          <th style="padding:8px 12px;">Trades gesamt</th>
          <th style="padding:8px 12px;">Letzte 5 Trades</th>
        </tr></thead>
        <tbody>{rows_pol}</tbody>
      </table>
      <h3>Heutige Orders</h3>
      {"<p>Keine neuen Orders heute.</p>" if not orders else f'<table style="width:100%;border-collapse:collapse;border:1px solid #eee;"><thead><tr style="background:#f5f5f5;"><th style="padding:8px 12px;">Politiker</th><th style="padding:8px 12px;">Ticker</th><th style="padding:8px 12px;">Status</th></tr></thead><tbody>' + rows_orders + '</tbody></table>'}
      <p style="color:#888;font-size:12px;margin-top:32px;">
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

    if not is_market_open():
        print("Markt ist geschlossen - kein Handel heute.")
        politicians = fetch_politicians()
        send_email(
            f"Congress Trader {datetime.date.today():%d.%m.%Y} - Markt geschlossen",
            build_email(politicians[:TOP_N_POLITICIANS], [], account)
        )
        return

    politicians = fetch_politicians()
    if not politicians:
        print("Keine Politiker-Daten geladen - Abbruch.")
        return

    print(f"\nTop 5 Politiker:")
    for i, p in enumerate(politicians[:5], 1):
        print(f"  {i}. {p['name']} - Score {p['return_pct']:.1f}%  "
              f"Neue Kaeufe: {', '.join(p['recent_buys'][:3]) or '-'}")

    orders = mirror_positions(politicians, account)

    account_updated = get_account()
    send_email(
        f"Congress Trader {datetime.date.today():%d.%m.%Y} - {len(orders)} Orders",
        build_email(politicians[:TOP_N_POLITICIANS], orders, account_updated)
    )

    print("\nFertig!")

if __name__ == "__main__":
    main()
