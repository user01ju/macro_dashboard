"""TradingView 經濟行事曆（非官方 endpoint）：forecast/actual/previous/period 全都有。

事件 key：<indicator_id>|<title>|<參考月 YYYY-MM 或事件日期>
視窗：過去 40 天（補 actual）+ 未來 21 天（行事曆）。
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import http_get

URL = ("https://economic-calendar.tradingview.com/events"
       "?from={frm}&to={to}&countries=US,TW")
TPE = ZoneInfo("Asia/Taipei")


def _fmt(value, unit, scale):
    if value is None:
        return ""
    s = f"{value:g}"
    if scale:
        s += scale
    if unit:
        s += unit
    return s


def fetch(indicators):
    matchers = []  # (indicator_cfg, ticker or None, title or None)
    for ind in indicators:
        for m in ind.get("tv", []):
            matchers.append((ind, m.get("ticker"), m.get("title")))
    if not matchers:
        return {}

    now = datetime.now(timezone.utc)
    frm = (now - timedelta(days=40)).strftime("%Y-%m-%dT00:00:00.000Z")
    to = (now + timedelta(days=21)).strftime("%Y-%m-%dT00:00:00.000Z")
    r = http_get(URL.format(frm=frm, to=to),
                 headers={"Origin": "https://www.tradingview.com",
                          "User-Agent": "Mozilla/5.0"})
    rows = r.json().get("result", [])

    out = {}
    for row in rows:
        ind = None
        for cfg, ticker, title in matchers:
            if ticker and row.get("ticker") != ticker:
                continue
            if title and row.get("title") != title:
                continue
            if not ticker and row.get("country") != cfg["country"]:
                continue
            ind = cfg
            break
        if ind is None:
            continue

        dt = datetime.fromisoformat(row["date"].replace("Z", "+00:00")).astimezone(TPE)
        ref = (row.get("referenceDate") or "")[:7] or f"{dt:%Y-%m-%d}"
        key = f"{ind['id']}|{row['title']}|{ref}"
        unit, scale = row.get("unit"), row.get("scale")
        out[key] = {
            "indicator": ind["id"],
            "ref": ref,
            "title": row["title"] + (f"（{row['period']}）" if row.get("period") else ""),
            "date_tw": f"{dt:%Y-%m-%d %H:%M}",
            "sort_key": dt.isoformat(),
            "forecast": _fmt(row.get("forecast"), unit, scale),
            "previous": _fmt(row.get("previous"), unit, scale),
            "actual": _fmt(row.get("actual"), unit, scale),
        }
    return out
