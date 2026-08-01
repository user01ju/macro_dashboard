"""FRED 歷史序列（免 API key 的 fredgraph.csv 下載端點），供趨勢圖用。"""
import csv
import io

from . import http_get

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"


def _series(sid):
    """[(date_str, float), ...]，跳過缺值。"""
    text = http_get(URL.format(sid=sid)).text
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) != 2 or row[0] in ("DATE", "observation_date"):
            continue
        try:
            out.append((row[0][:7], float(row[1])))
        except ValueError:
            continue
    return out


def fetch_charts():
    """回傳 {chart_id: {labels: [...], datasets: [{label, data}, ...]}}，抓失敗的 chart 略過。"""
    charts = {}

    def yoy(rows):
        d = dict(rows)
        out = []
        for ym, v in rows:
            y, m = ym.split("-")
            prev = d.get(f"{int(y) - 1}-{m}")
            if prev:
                out.append((ym, round((v / prev - 1) * 100, 1)))
        return out

    try:
        cpi = yoy(_series("CPIAUCSL"))[-36:]
        core = dict(yoy(_series("CPILFESL")))
        charts["cpi_yoy"] = {
            "labels": [ym for ym, _ in cpi],
            "datasets": [
                {"label": "CPI YoY %", "data": [v for _, v in cpi]},
                {"label": "核心 CPI YoY %", "data": [core.get(ym) for ym, _ in cpi]},
            ],
        }
    except Exception as e:  # noqa: BLE001
        print(f"[fred] cpi 失敗: {e}")

    try:
        payems = _series("PAYEMS")
        diff = [(payems[i][0], round(payems[i][1] - payems[i - 1][1]))
                for i in range(1, len(payems))][-36:]
        unrate = dict(_series("UNRATE"))
        charts["nfp"] = {
            "labels": [ym for ym, _ in diff],
            "datasets": [
                {"label": "新增非農就業 (千人)", "data": [v for _, v in diff]},
                {"label": "失業率 %", "data": [unrate.get(ym) for ym, _ in diff], "y2": True},
            ],
        }
    except Exception as e:  # noqa: BLE001
        print(f"[fred] nfp 失敗: {e}")

    try:
        pce = yoy(_series("PCEPILFE"))[-36:]
        charts["pce"] = {
            "labels": [ym for ym, _ in pce],
            "datasets": [{"label": "核心 PCE YoY %", "data": [v for _, v in pce]}],
        }
    except Exception as e:  # noqa: BLE001
        print(f"[fred] pce 失敗: {e}")

    try:
        ff = _series("DFEDTARU")
        monthly = {}
        for d, v in ff:
            monthly[d] = v  # 月內最後值
        rows = sorted(monthly.items())[-60:]
        charts["fedfunds"] = {
            "labels": [ym for ym, _ in rows],
            "datasets": [{"label": "Fed 目標利率上緣 %", "data": [v for _, v in rows]}],
        }
    except Exception as e:  # noqa: BLE001
        print(f"[fred] fedfunds 失敗: {e}")

    return charts
