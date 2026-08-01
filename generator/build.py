"""Jinja2 渲染 output/index.html。"""
import calendar
import html as html_mod
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
TPE = ZoneInfo("Asia/Taipei")

UPCOMING_DAYS = 14
RECENT_DAYS = 45


def _md(text):
    """logic 卡的極簡 markdown：**bold** + 換行，先 escape 再轉。"""
    esc = html_mod.escape(text or "")
    esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
    return esc.replace("\n", "<br>")


def _tw_monthly_upcoming(indicators, now, tsmc_calls):
    """台灣月頻指標的預估公布日 + 法說會，落在未來 14 天內的。"""
    rows = []
    horizon = now + timedelta(days=UPCOMING_DAYS)
    for ind in indicators:
        day = ind.get("schedule_day")
        if not day:
            continue
        for delta in (0, 1):
            y, m = now.year, now.month + delta
            if m > 12:
                y, m = y + 1, m - 12
            d = min(day, calendar.monthrange(y, m)[1])
            dt = datetime(y, m, d, 16, 0, tzinfo=TPE)
            if now <= dt <= horizon:
                prev_m = m - 1 or 12
                rows.append({
                    "indicator": ind["id"], "name": ind["name"], "tier": ind["tier"],
                    "title": f"{prev_m} 月數據",
                    "date_tw": f"{dt:%Y-%m-%d}", "sort_key": dt.isoformat(),
                    "forecast": "", "previous": "", "estimated": True,
                })
    for ds in tsmc_calls:
        try:
            dt = datetime.fromisoformat(ds).replace(hour=14, tzinfo=TPE)
        except ValueError:
            continue
        if now <= dt <= horizon:
            rows.append({
                "indicator": "tsmc_call", "name": "台積電法說會", "tier": 1,
                "title": "台積電法說會", "date_tw": f"{dt:%Y-%m-%d}",
                "sort_key": dt.isoformat(), "forecast": "", "previous": "", "estimated": False,
            })
    return rows


def render(events, indicators, charts, tsmc_calls, generated_at):
    by_id = {i["id"]: i for i in indicators}
    now = datetime.now(TPE)

    upcoming, recent = [], []
    for key, ev in events.items():
        sk = ev.get("sort_key", "")
        try:
            dt = datetime.fromisoformat(sk)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TPE)
        except ValueError:
            continue
        ind = by_id.get(ev["indicator"], {})
        row = {**ev, "name": ind.get("name", ev["indicator"]), "tier": ind.get("tier", 2)}
        if ev.get("actual"):
            if now - timedelta(days=RECENT_DAYS) <= dt <= now + timedelta(days=2):
                recent.append(row)
        elif now - timedelta(hours=12) <= dt <= now + timedelta(days=UPCOMING_DAYS):
            upcoming.append(row)

    upcoming += _tw_monthly_upcoming(indicators, now, tsmc_calls)
    upcoming.sort(key=lambda r: r["sort_key"])
    recent.sort(key=lambda r: r["sort_key"], reverse=True)

    # 已公布區塊按（指標, 日期）分組，AI 分析同組共用
    groups, seen = [], {}
    for row in recent:
        weekly = by_id.get(row["indicator"], {}).get("weekly")
        gk = (row["indicator"],) if weekly else (row["indicator"], row["date_tw"][:10])
        if gk not in seen:
            seen[gk] = {"name": row["name"], "tier": row["tier"],
                        "date": "近幾週" if weekly else row["date_tw"][:10],
                        "indicator": row["indicator"], "rows": [],
                        "ai_analysis": row.get("ai_analysis", "")}
            groups.append(seen[gk])
        seen[gk]["rows"].append(row)

    env = Environment(loader=FileSystemLoader(ROOT / "generator" / "templates"),
                      autoescape=True)
    env.filters["md"] = _md
    html = env.get_template("index.html.j2").render(
        upcoming=upcoming,
        groups=groups,
        indicators=indicators,
        charts_json=json.dumps(charts, ensure_ascii=False),
        generated_at=generated_at.astimezone(TPE).strftime("%Y-%m-%d %H:%M"),
    )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "index.html").write_text(html, encoding="utf-8")
    print(f"[build] index.html 完成（近期 {len(groups)} 組 / 未來 {len(upcoming)} 筆）")
