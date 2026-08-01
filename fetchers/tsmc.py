"""台積電月營收：TWSE openapi t187ap05_L（只回最新一個月，全上市公司）。

歷史序列：history file 隨每月執行累積；冷啟動（<12 筆）時用 FinMind 免 token 回補。
"""
from pathlib import Path

from . import http_get, load_json, save_json

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
FINMIND_URL = (
    "https://api.finmindtrade.com/api/v4/data"
    "?dataset=TaiwanStockMonthRevenue&data_id=2330&start_date={start}"
)
HISTORY = Path(__file__).resolve().parent.parent / "output" / "data" / "tsmc_rev.json"


def _seed_from_finmind(history):
    from datetime import date
    start = date.today().replace(day=1)
    start = f"{start.year - 4}-01-01"
    try:
        rows = http_get(FINMIND_URL.format(start=start), timeout=30).json().get("data", [])
    except Exception as e:  # noqa: BLE001
        print(f"[tsmc] FinMind 回補失敗（可忽略，序列會隨時間累積）: {e}")
        return history
    for r in rows:
        y, m = r["revenue_year"], r["revenue_month"]
        ym = f"{y}-{m:02d}"
        history.setdefault(ym, {})["revenue_bn"] = round(r["revenue"] / 1e9, 1)
    # 用序列自算 YoY
    for ym, rec in history.items():
        y, m = ym.split("-")
        prev = history.get(f"{int(y) - 1}-{m}")
        if prev and prev.get("revenue_bn"):
            rec["yoy"] = round((rec["revenue_bn"] / prev["revenue_bn"] - 1) * 100, 1)
    return history


def fetch():
    """回傳 (series 由舊到新, 最新事件 tuple 或 None)。"""
    history = load_json(HISTORY, {})

    try:
        rows = http_get(TWSE_URL, timeout=40).json()
        rec = next((r for r in rows if r.get("公司代號") == "2330"), None)
    except Exception as e:  # noqa: BLE001
        print(f"[tsmc] TWSE 月營收抓取失敗: {e}")
        rec = None

    if rec:
        roc_ym = rec["資料年月"]  # e.g. 11506
        y, m = int(roc_ym[:3]) + 1911, int(roc_ym[3:])
        ym = f"{y}-{m:02d}"
        history[ym] = {
            "revenue_bn": round(float(rec["營業收入-當月營收"]) / 1e6, 1),  # 千元 -> 十億
            "yoy": round(float(rec["營業收入-去年同月增減(%)"]), 1),
            "mom": round(float(rec["營業收入-上月比較增減(%)"]), 1),
        }

    if len(history) < 12:
        history = _seed_from_finmind(history)

    save_json(HISTORY, history)
    series = [{"ym": ym, **rec} for ym, rec in sorted(history.items())]

    event = None
    if series and series[-1].get("yoy") is not None:
        last = series[-1]
        prev = series[-2] if len(series) > 1 else None
        event = (f"tsmc_rev|{last['ym']}", {
            "indicator": "tsmc_rev",
            "title": f"月營收 {last['ym']}",
            "date_tw": last["ym"],
            "sort_key": last["ym"] + "-27",
            "forecast": "",
            "previous": (f"{prev['yoy']:+.1f}% YoY" if prev and prev.get("yoy") is not None else ""),
            "actual": f"{last['yoy']:+.1f}% YoY（{last['revenue_bn'] * 10:,.0f} 億）",
        })
    return series, event
