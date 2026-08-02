"""主流程：抓資料 → upsert events → 手動覆蓋 → surprise → AI 分析 → 生成頁面。

exit 0 = 成功；exit 1 = 錯誤。
狀態（events.json / tsmc_rev.json / charts.json）靠 gh-pages 持久化，
CI 開頭會從 origin/gh-pages 還原 output/。
"""
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
EVENTS_FILE = OUTPUT / "data" / "events.json"

sys.path.insert(0, str(ROOT))

from fetchers import load_json, save_json  # noqa: E402
from fetchers import fred, mof_tw, tradingview, tsmc  # noqa: E402
from processors import surprise  # noqa: E402
from analysis import ai_analysis  # noqa: E402
from generator import build  # noqa: E402


def upsert(events, new_events):
    """非空值才覆蓋；actual 一旦寫入不被空值清掉。"""
    for key, ev in new_events.items():
        cur = events.get(key)
        if cur is None:
            events[key] = ev
            continue
        for field in ("forecast", "previous", "actual", "date_tw", "sort_key", "title"):
            if ev.get(field):
                cur[field] = ev[field]


def apply_overrides(events, overrides):
    """manual/overrides.json：key '<indicator>|<YYYY-MM>' 對到月頻事件；欄位非 null 就覆蓋。"""
    for okey, fields in overrides.get("events", {}).items():
        if okey.startswith("_"):
            continue
        target = events.get(okey)
        if target is None:
            # 月頻 key 直接比對；找不到就建殼（例如出口新聞稿先於 open data）
            ind, ym = okey.split("|", 1)
            target = events.setdefault(okey, {
                "indicator": ind, "title": f"{ind} {ym}", "date_tw": ym,
                "sort_key": ym + "-28", "forecast": "", "previous": "", "actual": "",
            })
        for f in ("forecast", "actual", "previous", "title", "date_tw"):
            if fields.get(f):
                if f == "actual" and fields[f] != target.get("actual"):
                    target.pop("ai_analysis", None)  # 換了實際值就重新分析
                    target.pop("analyzed_at", None)  # 時間戳也要清，否則指向已作廢的分析
                target[f] = fields[f]


def main():
    cfg = yaml.safe_load((ROOT / "indicators.yml").read_text(encoding="utf-8"))
    indicators = cfg["indicators"]
    by_id = {i["id"]: i for i in indicators}
    overrides = load_json(ROOT / "manual" / "overrides.json", {})

    store = load_json(EVENTS_FILE, {"events": {}})
    events = store["events"]

    # 1. TradingView 行事曆（美系 + 台灣出口：forecast/actual/previous 一次到位）
    try:
        upsert(events, tradingview.fetch(indicators))
    except Exception as e:  # noqa: BLE001
        print(f"[tv] 失敗（沿用既有 events）: {e}")

    # 2. 關務署出口序列（只餵趨勢圖；頭條數字走 TradingView）
    try:
        mof_series = mof_tw.fetch()
    except Exception as e:  # noqa: BLE001
        print(f"[mof] 失敗: {e}")
        mof_series = []

    # 3. 台積電月營收
    tsmc_series, tsmc_ev = tsmc.fetch()
    if tsmc_ev:
        upsert(events, {tsmc_ev[0]: tsmc_ev[1]})

    # 4. 手動覆蓋（美元計價出口、法說重點等）
    apply_overrides(events, overrides)

    # 5. surprise 計算
    for ev in events.values():
        surprise.annotate(ev, by_id.get(ev["indicator"], {}).get("direction", "context"))

    # 6. AI 當次分析（只分析新 actual）
    ai_analysis.run(events, by_id)

    save_json(EVENTS_FILE, store)

    # 7. 趨勢圖資料（FRED + 台灣序列 + ISM 從 actual 累積）
    charts = load_json(OUTPUT / "data" / "charts.json", {})
    try:
        charts.update(fred.fetch_charts())
    except Exception as e:  # noqa: BLE001
        print(f"[fred] 失敗（沿用舊圖）: {e}")
    if mof_series:
        rows = [r for r in mof_series if r["yoy"] is not None][-36:]
        charts["tw_exports"] = {
            "labels": [r["ym"] for r in rows],
            "datasets": [{"label": "出口 YoY %（NTD）", "data": [r["yoy"] for r in rows]}],
        }
    if tsmc_series:
        rows = [r for r in tsmc_series if r.get("yoy") is not None][-36:]
        charts["tsmc_rev"] = {
            "labels": [r["ym"] for r in rows],
            "datasets": [{"label": "月營收 YoY %", "data": [r["yoy"] for r in rows]}],
        }
    ism = sorted(
        (ev for ev in events.values()
         if ev["indicator"] == "us_ism" and ev.get("actual")),
        key=lambda e: e.get("sort_key", ""),
    )
    if ism:
        cur = load_json(OUTPUT / "data" / "ism_history.json", {})
        for ev in ism:
            v = surprise.parse_value(ev["actual"])
            if v is not None:
                cur[ev.get("ref", ev["date_tw"][:7])] = v
        save_json(OUTPUT / "data" / "ism_history.json", cur)
        rows = sorted(cur.items())[-36:]
        charts["ism"] = {
            "labels": [k for k, _ in rows],
            "datasets": [{"label": "ISM 製造業 PMI", "data": [v for _, v in rows]}],
        }
    save_json(OUTPUT / "data" / "charts.json", charts)

    # 8. 生成頁面
    build.render(
        events=events,
        indicators=indicators,
        charts=charts,
        tsmc_calls=overrides.get("tsmc_calls", []),
        generated_at=datetime.now(timezone.utc),
    )
    print("完成")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
