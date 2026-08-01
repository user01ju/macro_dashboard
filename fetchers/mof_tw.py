"""台灣出口：財政部關務署 open data CSV（新台幣千元，月頻）。

限制（見專案 README）：
- 新台幣計價；財政部新聞稿頭條是「美元計價年增率」，兩者匯率波動大時有落差。
- 此 CSV 落後新聞稿約一個月 → 當月頭條數字用 manual/overrides.json 補。
自動產出：NTD 出口值 + YoY 序列（畫趨勢圖），以及最新一筆的月度事件。
"""
import csv
import io

from . import http_get

CSV_URL = "https://opendata.customs.gov.tw/data/6053/csv.csv"


def fetch():
    """回傳 series: [{ym: 'YYYY-MM', exports_ntd_bn: float, yoy: float|None}, ...] 由舊到新。"""
    raw = http_get(CSV_URL).content
    for enc in ("utf-8-sig", "cp950"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("mof csv 編碼無法辨識")

    rows = list(csv.reader(io.StringIO(text)))
    # 欄位：年度(民國), 月份, 出口總值(新臺幣千元), ...
    data = {}
    for r in rows[1:]:
        if len(r) < 3 or not r[0].strip().isdigit():
            continue
        year = int(r[0]) + 1911
        month = int(r[1])
        exports_k = float(r[2])
        data[(year, month)] = exports_k

    series = []
    for (y, m) in sorted(data):
        prev = data.get((y - 1, m))
        yoy = round((data[(y, m)] / prev - 1) * 100, 1) if prev else None
        series.append({
            "ym": f"{y}-{m:02d}",
            "exports_ntd_bn": round(data[(y, m)] / 1e6, 1),  # 千元 -> 十億元
            "yoy": yoy,
        })
    return series
