"""
US Stock Scanner - day-trade & long-term candidate screener
=============================================================

What this does
---------------
1. Builds a scan universe (S&P 500 + Nasdaq 100 by default).
2. Downloads recent daily price/volume history for the whole universe in
   batches (yfinance) and computes cheap technical indicators for everyone
   (RSI14, SMA50/200, ATR%, relative volume, 1mo/3mo momentum).
3. Narrows to the top N technical candidates, then fetches fundamentals
   (analyst target price, market cap, next earnings date) only for those,
   since per-ticker fundamental calls are slow / rate-limited.
4. Computes two 1-100 scores for each candidate:
     - opportunity_score  ("利益期待スコア")
     - risk_score         ("リスクスコア")
   These are transparent, rule-based heuristics built from public data.
   They are NOT a probability of profit and are NOT financial advice.
5. Splits candidates into:
     - day_trade_list : high volatility + high relative volume, suited to
                         intraday/short-term trading
     - long_term_list : uptrend + analyst upside + not extremely overbought,
                         suited to swing/longer-horizon positions
6. Renders an HTML email and sends it via Gmail SMTP.

IMPORTANT LIMITATIONS (read before relying on this)
-----------------------------------------------------
- Data comes from Yahoo Finance via the unofficial `yfinance` library. It
  can be delayed, incomplete, or occasionally wrong. It is NOT the same
  live feed as your broker (e.g. Webull). Always confirm current prices in
  your broker before placing any order.
- Earnings dates pulled here are best-effort and sometimes unavailable or
  approximate. Always double-check a stock's earnings date before holding
  it through a report.
- The scores are heuristics for screening, not predictions. No tool can
  reliably predict short-term stock price moves. Use this to narrow down
  candidates for your own further research, not as a buy/sell signal.
- This script is not financial advice and the author/operator of this
  script is not a financial advisor.
"""

import os
import sys
import time
import smtplib
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import pandas as pd
import yfinance as yf

# --------------------------------------------------------------------------
# Configuration (tweak freely)
# --------------------------------------------------------------------------

# How many technical-stage candidates to carry forward into the expensive
# fundamentals stage. Keep this modest to keep runtime/rate-limits sane.
FUNDAMENTALS_STAGE_TOP_N = 40

# How many names to actually put in each emailed list.
DAY_TRADE_LIST_SIZE = 12
LONG_TERM_LIST_SIZE = 12

# Skip anything trading below this price (penny stocks = execution risk,
# wide spreads, harder to size positions sensibly for most accounts).
MIN_PRICE = 5.0

# Skip anything with average dollar volume below this (liquidity floor).
MIN_AVG_DOLLAR_VOLUME = 20_000_000  # $20M/day

# Batch size for yfinance bulk downloads.
BATCH_SIZE = 100

HISTORY_PERIOD = "6mo"
HISTORY_INTERVAL = "1d"


# --------------------------------------------------------------------------
# Universe construction
# --------------------------------------------------------------------------

def get_sp500_tickers() -> list[str]:
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    tables = pd.read_html(url)
    df = tables[0]
    return df["Symbol"].astype(str).tolist()


def get_nasdaq100_tickers() -> list[str]:
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    tables = pd.read_html(url)
    for t in tables:
        if "Ticker" in t.columns:
            return t["Ticker"].astype(str).tolist()
        if "Symbol" in t.columns:
            return t["Symbol"].astype(str).tolist()
    return []


def build_universe() -> list[str]:
    tickers: set[str] = set()
    try:
        tickers |= set(get_sp500_tickers())
    except Exception as e:
        print(f"[warn] failed to load S&P 500 list: {e}")
    try:
        tickers |= set(get_nasdaq100_tickers())
    except Exception as e:
        print(f"[warn] failed to load Nasdaq-100 list: {e}")

    # yfinance wants dashes, not dots (e.g. BRK.B -> BRK-B)
    cleaned = sorted({t.replace(".", "-").strip() for t in tickers if t and isinstance(t, str)})

    if not cleaned:
        # Hard fallback so the script never has zero candidates.
        cleaned = [
            "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
            "AMD", "NFLX", "CRM", "ADBE", "INTC", "SMCI", "PATH", "PLTR",
        ]
    return cleaned


# --------------------------------------------------------------------------
# Technical indicators
# --------------------------------------------------------------------------

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    last_close = close.iloc[-1]
    if pd.isna(atr) or last_close == 0:
        return np.nan
    return float(atr / last_close * 100)


def compute_technical_row(symbol: str, df: pd.DataFrame) -> dict | None:
    if df is None or df.empty or len(df) < 60:
        return None
    df = df.dropna(subset=["Close", "Volume"])
    if len(df) < 60:
        return None

    close = df["Close"]
    volume = df["Volume"]
    last_price = float(close.iloc[-1])
    if last_price < MIN_PRICE:
        return None

    avg_dollar_vol = float((close.tail(20) * volume.tail(20)).mean())
    if avg_dollar_vol < MIN_AVG_DOLLAR_VOLUME:
        return None

    day_change_pct = float((close.iloc[-1] / close.iloc[-2] - 1) * 100) if len(close) > 1 else 0.0
    rel_volume = float(volume.iloc[-1] / volume.tail(20).mean()) if volume.tail(20).mean() > 0 else np.nan

    sma50 = close.rolling(50).mean().iloc[-1]
    sma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else np.nan
    rsi14 = float(rsi(close).iloc[-1])
    atrp = atr_pct(df)

    mom_1m = float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) > 21 else np.nan
    mom_3m = float((close.iloc[-1] / close.iloc[-63] - 1) * 100) if len(close) > 63 else np.nan

    return {
        "symbol": symbol,
        "price": last_price,
        "day_change_pct": day_change_pct,
        "rel_volume": rel_volume,
        "rsi14": rsi14,
        "atr_pct": atrp,
        "above_sma50": bool(last_price > sma50) if pd.notna(sma50) else None,
        "above_sma200": bool(last_price > sma200) if pd.notna(sma200) else None,
        "mom_1m": mom_1m,
        "mom_3m": mom_3m,
        "avg_dollar_vol": avg_dollar_vol,
    }


def download_history_batched(tickers: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i:i + BATCH_SIZE]
        print(f"[info] downloading history {i}-{i+len(chunk)} / {len(tickers)}")
        try:
            data = yf.download(
                chunk, period=HISTORY_PERIOD, interval=HISTORY_INTERVAL,
                group_by="ticker", threads=True, progress=False,
                auto_adjust=True,
            )
        except Exception as e:
            print(f"[warn] batch download failed: {e}")
            continue

        for t in chunk:
            try:
                if len(chunk) == 1:
                    df = data
                else:
                    df = data[t] if t in data.columns.get_level_values(0) else None
                if df is not None and not df.empty:
                    out[t] = df
            except Exception:
                continue
        time.sleep(1)  # be polite to the endpoint
    return out


# --------------------------------------------------------------------------
# Fundamentals (only fetched for the shortlisted stage-2 candidates)
# --------------------------------------------------------------------------

def fetch_fundamentals(symbol: str) -> dict:
    result = {
        "target_mean": None,
        "market_cap": None,
        "next_earnings": None,
        "recommendation": None,
        "sector": None,
    }
    try:
        tk = yf.Ticker(symbol)
        info = tk.info or {}
        result["target_mean"] = info.get("targetMeanPrice")
        result["market_cap"] = info.get("marketCap")
        result["recommendation"] = info.get("recommendationKey")
        result["sector"] = info.get("sector")
    except Exception as e:
        print(f"[warn] info fetch failed for {symbol}: {e}")

    try:
        tk = yf.Ticker(symbol)
        cal = tk.get_earnings_dates(limit=4)
        if cal is not None and not cal.empty:
            future = cal[cal.index >= pd.Timestamp.now(tz=cal.index.tz)]
            if not future.empty:
                result["next_earnings"] = future.index[0].strftime("%Y-%m-%d")
    except Exception as e:
        print(f"[warn] earnings fetch failed for {symbol}: {e}")

    return result


# --------------------------------------------------------------------------
# Scoring (1-100, heuristic — NOT a probability of profit)
# --------------------------------------------------------------------------

def clamp(v, lo=1, hi=100):
    return max(lo, min(hi, v))


def score_day_trade(row: dict) -> tuple[float, float]:
    """Returns (opportunity_score, risk_score) for short-term/day-trade fit."""
    atrp = row.get("atr_pct") or 0
    relvol = row.get("rel_volume") or 1
    rsi14 = row.get("rsi14") or 50
    day_chg = abs(row.get("day_change_pct") or 0)

    # Opportunity: reward volatility + relative volume + today's move,
    # but this is "tradeable action today", not "will go up".
    opp = (
        min(atrp, 15) / 15 * 40 +          # volatility, capped
        min(relvol, 5) / 5 * 35 +          # relative volume
        min(day_chg, 15) / 15 * 25         # today's realized move
    )

    # Risk: high volatility, RSI extremes (overbought/oversold), and thin
    # relative volume all raise risk.
    rsi_extreme = abs(rsi14 - 50) / 50 * 100  # 0 at RSI50, 100 at RSI0/100
    risk = (
        min(atrp, 15) / 15 * 50 +
        rsi_extreme * 0.3 +
        (20 if relvol < 0.8 else 0)
    )
    return clamp(opp), clamp(risk)


def score_long_term(row: dict, fund: dict) -> tuple[float, float]:
    """Returns (opportunity_score, risk_score) for longer-horizon fit."""
    mom1 = row.get("mom_1m") or 0
    mom3 = row.get("mom_3m") or 0
    above50 = row.get("above_sma50")
    above200 = row.get("above_sma200")
    rsi14 = row.get("rsi14") or 50
    price = row.get("price") or 0
    target = fund.get("target_mean")

    upside_pct = None
    if target and price:
        upside_pct = (target - price) / price * 100

    opp = 0
    opp += 15 if above50 else 0
    opp += 15 if above200 else 0
    opp += clamp(min(max(mom1, -20), 20) / 20 * 15 + 15, 0, 30)   # 1mo momentum, centered
    opp += clamp(min(max(mom3, -30), 30) / 30 * 10 + 10, 0, 20)   # 3mo momentum, centered
    if upside_pct is not None:
        opp += clamp(min(max(upside_pct, -10), 40) / 40 * 20, 0, 20)

    risk = 0
    risk += 25 if (above200 is False) else 5          # downtrend = higher risk
    risk += abs(rsi14 - 50) / 50 * 30                  # overbought/oversold
    mc = fund.get("market_cap")
    if mc:
        if mc < 2_000_000_000:
            risk += 30
        elif mc < 10_000_000_000:
            risk += 15
    else:
        risk += 10  # unknown market cap treated as mild extra risk

    return clamp(opp), clamp(risk)


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------

def run_scan():
    universe = build_universe()
    print(f"[info] universe size: {len(universe)}")

    history = download_history_batched(universe)
    print(f"[info] got history for {len(history)} tickers")

    tech_rows = []
    for sym, df in history.items():
        row = compute_technical_row(sym, df)
        if row:
            tech_rows.append(row)
    tech_df = pd.DataFrame(tech_rows)
    print(f"[info] passed liquidity/price filters: {len(tech_df)}")

    if tech_df.empty:
        raise RuntimeError("No tickers passed the technical filters — aborting.")

    # Stage-1 shortlist: rank by a simple activity score (volatility + rel
    # volume + |momentum|) to decide who is worth fetching fundamentals for.
    tech_df["activity_score"] = (
        tech_df["atr_pct"].fillna(0) * 1.0 +
        tech_df["rel_volume"].fillna(1) * 10 +
        tech_df["mom_1m"].abs().fillna(0) * 0.5
    )
    shortlist = tech_df.sort_values("activity_score", ascending=False).head(
        FUNDAMENTALS_STAGE_TOP_N
    )

    candidates = []
    for _, row in shortlist.iterrows():
        sym = row["symbol"]
        fund = fetch_fundamentals(sym)
        row_d = row.to_dict()
        dt_opp, dt_risk = score_day_trade(row_d)
        lt_opp, lt_risk = score_long_term(row_d, fund)
        candidates.append({
            **row_d,
            **fund,
            "day_opportunity": dt_opp,
            "day_risk": dt_risk,
            "long_opportunity": lt_opp,
            "long_risk": lt_risk,
        })
        time.sleep(0.3)

    cand_df = pd.DataFrame(candidates)

    day_trade_list = (
        cand_df.sort_values("day_opportunity", ascending=False)
        .head(DAY_TRADE_LIST_SIZE)
        .to_dict("records")
    )
    long_term_list = (
        cand_df.sort_values("long_opportunity", ascending=False)
        .head(LONG_TERM_LIST_SIZE)
        .to_dict("records")
    )

    return day_trade_list, long_term_list


# --------------------------------------------------------------------------
# Email rendering + sending
# --------------------------------------------------------------------------

def fmt_pct(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def fmt_num(v, digits=2):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:.{digits}f}"


def render_row_day(r):
    return f"""
    <tr>
      <td><b>{r['symbol']}</b></td>
      <td>${fmt_num(r['price'])}</td>
      <td>{fmt_pct(r.get('day_change_pct'))}</td>
      <td>{fmt_num(r.get('atr_pct'))}%</td>
      <td>{fmt_num(r.get('rel_volume'))}x</td>
      <td>{fmt_num(r.get('rsi14'), 0)}</td>
      <td style="color:#c0392b"><b>{fmt_num(r['day_opportunity'],0)}</b></td>
      <td style="color:#8e44ad"><b>{fmt_num(r['day_risk'],0)}</b></td>
      <td>{r.get('next_earnings') or '不明'}</td>
    </tr>"""


def render_row_long(r):
    upside = None
    if r.get("target_mean") and r.get("price"):
        upside = (r["target_mean"] - r["price"]) / r["price"] * 100
    return f"""
    <tr>
      <td><b>{r['symbol']}</b></td>
      <td>${fmt_num(r['price'])}</td>
      <td>{fmt_pct(r.get('mom_3m'))}</td>
      <td>{'○' if r.get('above_sma200') else '×'}</td>
      <td>{fmt_pct(upside)}</td>
      <td>{r.get('sector') or '—'}</td>
      <td style="color:#c0392b"><b>{fmt_num(r['long_opportunity'],0)}</b></td>
      <td style="color:#8e44ad"><b>{fmt_num(r['long_risk'],0)}</b></td>
      <td>{r.get('next_earnings') or '不明'}</td>
    </tr>"""


def render_email_html(day_list, long_list):
    now = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M JST")
    day_rows = "".join(render_row_day(r) for r in day_list)
    long_rows = "".join(render_row_long(r) for r in long_list)

    return f"""
    <html><body style="font-family:sans-serif;color:#222;">
    <h2>米国株スキャン結果 ({now})</h2>
    <p style="background:#fff3cd;border:1px solid #ffe08a;padding:10px;border-radius:6px;">
      「利益期待スコア」「リスクスコア」は出来高・値幅・トレンド・アナリスト評価などから
      算出した相対的な目安(1〜100)であり、将来の値動きや利益を保証するものではありません。
      本メールは投資助言ではありません。発注前に必ずご自身のブローカー(Webull等)で
      最新の価格・決算日をご確認ください。
    </p>

    <h3>デイトレード候補</h3>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px;">
      <tr style="background:#222;color:#fff;">
        <th>銘柄</th><th>現在値</th><th>前日比</th><th>ATR%</th><th>相対出来高</th>
        <th>RSI14</th><th>利益期待</th><th>リスク</th><th>次回決算</th>
      </tr>
      {day_rows}
    </table>

    <h3 style="margin-top:24px;">長期・成長期待候補</h3>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px;">
      <tr style="background:#222;color:#fff;">
        <th>銘柄</th><th>現在値</th><th>3ヶ月騰落率</th><th>200日線上</th>
        <th>目標株価乖離</th><th>セクター</th><th>利益期待</th><th>リスク</th><th>次回決算</th>
      </tr>
      {long_rows}
    </table>

    <p style="margin-top:24px;font-size:12px;color:#666;">
      データ出典: Yahoo Finance (yfinance) — 遅延・欠損の可能性があります。<br>
      決算日は取得できない場合があります。発表日をまたぐ保有はギャップリスクに注意してください。
    </p>
    </body></html>
    """


def send_email(html_body: str):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    gmail_to = os.environ.get("GMAIL_TO", gmail_user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"米国株スキャン結果 {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = gmail_user
    msg["To"] = gmail_to
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, [gmail_to], msg.as_string())
    print("[info] email sent")


def main():
    try:
        day_list, long_list = run_scan()
        html = render_email_html(day_list, long_list)
        send_email(html)
    except Exception:
        print("[error] scan failed:")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
