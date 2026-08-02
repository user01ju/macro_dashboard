"""資料正確性驗證（規劃見 VERIFICATION.md）。

用法：
    python verify.py --tier a      # 零外部呼叫，掛在 macro_update.yml 部署前
    python verify.py --tier b      # 交叉源（FRED / TWSE），verify.yml 每日跑
    python verify.py --tier all    # 預設

exit code：0 全過（或只有 SKIP）／1 至少一條 FAIL／2 沒 FAIL 但有 WARN。

「交易日」在本 repo 的在地化（共用 spec 第 6 節）：
本專案不是台股行情資料，沒有交易日概念，落後天數改用**各指標自己的公布頻率**
（CPI/NFP/PCE/ISM/出口 月頻、初領失業金 週頻、FOMC 不定期）加上假日順延的寬限，
門檻集中在 FRESHNESS_DAYS。資料檔不存在（本機沒還原 gh-pages）一律 SKIP 不 FAIL。
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
DATA = OUTPUT / "data"
EVENTS_FILE = DATA / "events.json"
CHARTS_FILE = DATA / "charts.json"
TSMC_FILE = DATA / "tsmc_rev.json"
ISM_FILE = DATA / "ism_history.json"
INDEX_HTML = OUTPUT / "index.html"
TPE = ZoneInfo("Asia/Taipei")

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"

# ── 門檻常數（改門檻只動這裡） ─────────────────────────────────────────

# 各指標「最新 actual 可以有多舊」。取「公布間隔的最壞情況 + 假日/延後寬限」，
# 訂太緊會在每次公布前一天誤報，訂太鬆則抓不到資料源斷供。
FRESHNESS_DAYS = {
    "us_cpi": 45,       # 月頻，隔月 10–15 日；下次公布前最壞約 31 天
    "us_nfp": 45,       # 月頻，隔月第一個週五
    "us_pce": 45,       # 月頻，月底
    "us_ism": 45,       # 月頻，隔月第一個營業日
    "tw_exports": 45,   # 月頻，隔月 7–9 日
    "us_claims": 14,    # 週頻（週四）；連假順延最多一天，最壞約 8 天
    "us_fomc": 100,     # 不定期：一年 8 次，最長間隔約 8 週（56 天）+ 寬限
    "tsmc_rev": 55,     # 月頻，但 sort_key 是「參考月-27」不是公布日，最壞約 44 天
    "tsmc_call": None,  # 純行事曆（無 actual），不做新鮮度檢查
}

# 未來事件總數下限。TradingView 斷供時 update.py 只印訊息不中斷，
# 既有 events 會被沿用、頁面看起來正常，但「未來行事曆」會先枯竭 → 這是最早的斷供訊號。
MIN_FUTURE_EVENTS = 3

# surprise 重算容忍度：存檔字串是 2 位小數四捨五入後再去零，最大誤差 0.005。
SURPRISE_TOL = 0.006

# weekly 指標每個「完整月份」至少要有幾筆。防漏設 weekly:true → 同月各週互相覆蓋成 1 筆。
MIN_WEEKLY_EVENTS_PER_MONTH = 3

# 公布後多久還沒 AI 判讀就算異常（workflow 每 3 小時一次，兩輪沒補上就有問題）。
AI_COVERAGE_GRACE_HOURS = 8

# 趨勢圖最新一點可以有多舊（FRED 落後 1–2 月、關務署落後 1–2 月都正常）。
CHART_STALE_DAYS = 120

# index.html 的「更新於」時間可以有多舊（每 3 小時一次；排程延遲/暫停時才會超過）。
PAGE_STALE_HOURS = 24

# tsmc_rev.json 自算 YoY vs 存檔 YoY 的容忍（存檔營收已四捨五入到 0.1 十億）。
TSMC_YOY_TOL = 0.5

# ── Tier B（外部呼叫）預算 ────────────────────────────────────────────

# 每次執行最多打幾次外部 API，呼叫之間至少 sleep 幾秒。
TIER_B_MAX_CALLS = 3
TIER_B_CALL_INTERVAL_SEC = 1.0
# 來源多於預算 → 依日期輪替，每個來源每 4 天至少跑 3 次；被輪掉的回 SKIP。
TIER_B_SOURCES = ["fred-cpi", "fred-nfp", "fred-pce", "twse-tsmc-rev"]

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
TWSE_REV_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
HTTP_TIMEOUT = 40
# FRED 對不帶 Mozilla 前綴的 UA 會直接吊死連線（實測 read timeout），沿用 fetchers/__init__.py 的寫法
UA = {"User-Agent": "Mozilla/5.0 (macro-dashboard verify; github.com/user01ju/macro_dashboard)"}

# 交叉源容忍度
CPI_YOY_TOL = 0.3    # FRED 用季調指數自算 YoY vs BLS 公布值，季調因子年度修正會有小差
NFP_DIFF_TOL_K = 100  # FRED 是修正後數字、TV 是初值；NFP 兩次修正 ±50K 是常態
PCE_YOY_TOL = 0.3
TSMC_REV_TOL_BN = 0.2
TSMC_TWSE_YOY_TOL = 0.2


# ── 共用小工具 ────────────────────────────────────────────────────────

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_MULT = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _load_json(path):
    """回傳 dict/list，檔案不存在或壞掉回 None（讓檢查自己決定 SKIP 還是 FAIL）。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _surface(s):
    """'110K' -> (110.0, 'K')；'0.3%' -> (0.3, '')；沒數字 -> (None, '')。

    刻意獨立於 processors/surprise.py 重寫一次——驗證的意義在於不共用被驗對象的實作。
    """
    if not s:
        return None, ""
    s = s.replace(",", "")
    m = _NUM_RE.search(s)
    if not m:
        return None, ""
    tail = s[m.end():m.end() + 1].upper()
    return float(m.group()), (tail if tail in _MULT else "")


def _expected_surprise(actual, forecast):
    """回傳 (delta 數值, 尾綴) 或 None。單位相同時用表面值（-53K），不同時退化成絕對值。"""
    a, sa = _surface(actual)
    f, sf = _surface(forecast)
    if a is None or f is None:
        return None
    if sa != sf:
        return (a * _MULT.get(sa, 1) - f * _MULT.get(sf, 1), "")
    return (a - f, sa)


def _parse_delta(s):
    """把存檔的 delta 字串拆回 (數值, 尾綴)，例 '-53K' -> (-53.0, 'K')。"""
    return _surface(s)


def _dt(sort_key):
    """sort_key 轉 aware datetime（'2026-06-27' 這種純日期補台北時區）。"""
    dt = datetime.fromisoformat(sort_key)
    return dt.replace(tzinfo=TPE) if dt.tzinfo is None else dt


def _yoy(series, ym):
    """series: {'YYYY-MM': value}。回傳 ym 相對去年同月的 YoY %。"""
    y, m = ym.split("-")
    prev = series.get(f"{int(y) - 1}-{m}")
    cur = series.get(ym)
    if prev in (None, 0) or cur is None:
        return None
    return (cur / prev - 1) * 100


def _prev_ym(ym):
    y, m = (int(x) for x in ym.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def _months_between(a, b):
    """兩個 'YYYY-MM' 相差幾個月。"""
    ay, am = (int(x) for x in a.split("-")[:2])
    by, bm = (int(x) for x in b.split("-")[:2])
    return (by - ay) * 12 + (bm - am)


class Ctx:
    """一次性載入所有檔案，讓各檢查共用（Tier A 全程零 I/O 之外的動作）。"""

    def __init__(self, sources):
        cfg = yaml.safe_load((ROOT / "indicators.yml").read_text(encoding="utf-8"))
        self.indicators = cfg["indicators"]
        self.by_id = {i["id"]: i for i in self.indicators}
        store = _load_json(EVENTS_FILE)
        self.events = store.get("events") if isinstance(store, dict) else None
        self.charts = _load_json(CHARTS_FILE)
        self.tsmc = _load_json(TSMC_FILE)
        self.ism = _load_json(ISM_FILE)
        self.now = datetime.now(TPE)
        # Tier B 預算
        self.sources = set(sources)
        self.calls = 0
        self._last_call = 0.0

    def take_call(self, source):
        """要一次外部呼叫額度；輪替到或預算用完回 False。"""
        if source not in self.sources or self.calls >= TIER_B_MAX_CALLS:
            return False
        if self._last_call:
            gap = time.monotonic() - self._last_call
            if gap < TIER_B_CALL_INTERVAL_SEC:
                time.sleep(TIER_B_CALL_INTERVAL_SEC - gap)
        self.calls += 1
        self._last_call = time.monotonic()
        return True

    def latest_actual(self, indicator, title=None):
        """該指標（可再指定 TV title）最新一筆有 actual 的 (key, event)。"""
        best = None
        for key, ev in (self.events or {}).items():
            if ev.get("indicator") != indicator or not ev.get("actual"):
                continue
            if title is not None:
                parts = key.split("|")
                if len(parts) < 3 or parts[1] != title:
                    continue
            if best is None or ev.get("sort_key", "") > best[1].get("sort_key", ""):
                best = (key, ev)
        return best


NO_EVENTS = (SKIP, "events.json 不存在或格式不符（本機未從 gh-pages 還原 output/）")


# ── Tier A ────────────────────────────────────────────────────────────

def check_events_schema(ctx):
    """每筆事件必要欄位齊全、sort_key 可解析（build.py 會默默跳過解析不了的事件）。"""
    if ctx.events is None:
        return NO_EVENTS
    if not ctx.events:
        return SKIP, "events.json 是空的（冷啟動）"
    required = ("indicator", "title", "date_tw", "sort_key")
    bad = []
    for key, ev in ctx.events.items():
        miss = [f for f in required if not ev.get(f)]
        if miss:
            bad.append(f"{key}: 缺 {','.join(miss)}")
            continue
        try:
            _dt(ev["sort_key"])
        except ValueError:
            bad.append(f"{key}: sort_key 解析失敗 {ev['sort_key']!r}")
    if bad:
        return FAIL, f"{len(bad)}/{len(ctx.events)} 筆事件結構有問題：{'; '.join(bad[:3])}"
    return PASS, f"{len(ctx.events)} 筆事件欄位齊全、sort_key 皆可解析"


def check_events_indicator_registered(ctx):
    """事件的 indicator 必須在 indicators.yml 裡，且與 key 前綴一致。

    gh-pages 狀態是永久累積的：indicator 改 id 後舊事件會一直留著，
    在頁面上變成沒有邏輯卡、沒有 tier 的孤兒列。
    """
    if ctx.events is None:
        return NO_EVENTS
    orphan, mismatch = [], []
    for key, ev in ctx.events.items():
        ind = ev.get("indicator")
        if ind not in ctx.by_id:
            orphan.append(f"{key}({ind})")
        if key.split("|")[0] != ind:
            mismatch.append(key)
    if orphan or mismatch:
        return FAIL, (f"孤兒指標 {len(orphan)} 筆 {orphan[:3]}；"
                      f"key 前綴與 indicator 不符 {len(mismatch)} 筆 {mismatch[:3]}")
    return PASS, f"{len(ctx.events)} 筆事件的 indicator 都註冊在 indicators.yml"


def check_events_freshness(ctx):
    """各指標最新 actual 落後天數在該指標公布頻率的合理窗內。"""
    if ctx.events is None:
        return NO_EVENTS
    stale, unknown, checked = [], [], []
    for ind in ctx.indicators:
        iid = ind["id"]
        if iid not in FRESHNESS_DAYS:
            unknown.append(iid)
            continue
        window = FRESHNESS_DAYS[iid]
        if window is None:
            continue
        hit = ctx.latest_actual(iid)
        if hit is None:
            continue  # 冷啟動還沒有任何 actual，不當失敗
        age = (ctx.now - _dt(hit[1]["sort_key"])).days
        checked.append((iid, age, window))
        if age > window:
            stale.append(f"{iid} 落後 {age} 天（窗 {window}，最新 {hit[1].get('date_tw')}）")
    if unknown:
        return WARN, f"indicators.yml 有 FRESHNESS_DAYS 沒定義的指標：{unknown}，請補門檻"
    if stale:
        return FAIL, "資料落後超窗：" + "；".join(stale)
    if not checked:
        return SKIP, "所有指標都還沒有 actual（冷啟動）"
    worst = max(checked, key=lambda t: t[1] / t[2])
    return PASS, (f"{len(checked)} 個指標都在公布窗內，最緊的是 "
                  f"{worst[0]} 落後 {worst[1]} 天（窗 {worst[2]}）")


def check_calendar_lookahead(ctx):
    """未來還有沒有待公布事件——TradingView 斷供的最早訊號。"""
    if ctx.events is None:
        return NO_EVENTS
    future = []
    for ev in ctx.events.values():
        try:
            dt = _dt(ev["sort_key"])
        except (ValueError, KeyError):
            continue
        if dt > ctx.now and not ev.get("actual"):
            future.append(ev)
    if len(future) < MIN_FUTURE_EVENTS:
        return WARN, (f"未來待公布事件僅 {len(future)} 筆（門檻 {MIN_FUTURE_EVENTS}），"
                      "TradingView 行事曆可能已斷供，頁面正在沿用舊資料")
    nearest = min(future, key=lambda e: e["sort_key"])
    return PASS, f"未來待公布事件 {len(future)} 筆，最近一筆 {nearest.get('date_tw')} {nearest.get('title')}"


def check_surprise_recompute(ctx):
    """獨立重算 actual − forecast，比對存檔的 delta / verdict / good。"""
    if ctx.events is None:
        return NO_EVENTS
    bad, n = [], 0
    for key, ev in ctx.events.items():
        exp = _expected_surprise(ev.get("actual"), ev.get("forecast"))
        got = ev.get("surprise")
        if exp is None:
            if got is not None:
                bad.append(f"{key}: 無法算 surprise 卻有 {got}")
            continue
        if got is None:
            bad.append(f"{key}: actual={ev.get('actual')!r} forecast={ev.get('forecast')!r} 應有 surprise 卻是 null")
            continue
        n += 1
        delta, suffix = exp
        gv, gs = _parse_delta(got.get("delta"))
        if gv is None or abs(gv - delta) > SURPRISE_TOL or gs != suffix:
            bad.append(f"{key}: delta 存 {got.get('delta')!r}，重算應為 {delta:+.2f}{suffix}")
            continue
        want_verdict = "inline" if abs(delta) < 1e-9 else ("above" if delta > 0 else "below")
        if got.get("verdict") != want_verdict:
            bad.append(f"{key}: verdict 存 {got.get('verdict')!r}，應為 {want_verdict!r}")
            continue
        direction = ctx.by_id.get(ev["indicator"], {}).get("direction", "context")
        want_good = None
        if want_verdict != "inline":
            if direction == "higher_is_good":
                want_good = delta > 0
            elif direction == "higher_is_bad":
                want_good = delta < 0
        if got.get("good") != want_good:
            bad.append(f"{key}: good 存 {got.get('good')!r}，direction={direction} 應為 {want_good!r}")
    if bad:
        return FAIL, f"{len(bad)} 筆 surprise 與重算不符：{'; '.join(bad[:3])}"
    return PASS, f"{n} 筆 surprise 重算一致（delta/verdict/good）"


def check_surprise_surface_unit(ctx):
    """表面單位規則：actual/forecast 同尾綴時，delta 必須帶同一尾綴（-53K 而非 -53,000）。"""
    if ctx.events is None:
        return NO_EVENTS
    bad, n = [], 0
    for key, ev in ctx.events.items():
        a, sa = _surface(ev.get("actual"))
        f, sf = _surface(ev.get("forecast"))
        if a is None or f is None or not sa or sa != sf:
            continue
        got = (ev.get("surprise") or {}).get("delta")
        n += 1
        gv, gs = _parse_delta(got)
        if gs != sa:
            bad.append(f"{key}: actual/forecast 都是 {sa} 單位，delta 卻是 {got!r}")
        elif gv is not None and abs(gv) > max(abs(a), abs(f)) * 2 + 1:
            bad.append(f"{key}: delta {got!r} 量級不像表面單位（a={a}{sa} f={f}{sf}）")
    if bad:
        return FAIL, f"{len(bad)} 筆落差沒用表面單位：{'; '.join(bad[:3])}"
    if not n:
        return SKIP, "目前沒有 K/M/B 單位且 actual/forecast 齊全的事件"
    return PASS, f"{n} 筆帶單位的落差都用表面單位（例：NFP -53K 而非 -53000）"


def check_weekly_events_per_month(ctx):
    """weekly 指標每個完整月份至少 3 筆。

    防的是漏設 indicators.yml 的 weekly:true → tradingview.py 的 ref 只切到月，
    同月各週互相覆蓋成 1 筆的迴歸。只檢查「首尾之間」的完整月份，
    避免把 repo 冷啟動當月（抓取窗只有 40 天）當成失敗。
    """
    if ctx.events is None:
        return NO_EVENTS
    weekly_ids = [i["id"] for i in ctx.indicators if i.get("weekly")]
    if not weekly_ids:
        return SKIP, "indicators.yml 沒有 weekly 指標"
    bad, checked = [], []
    for iid in weekly_ids:
        per_month = {}
        for ev in ctx.events.values():
            if ev.get("indicator") != iid:
                continue
            ref = ev.get("ref") or ev.get("date_tw", "")
            if len(ref) < 7:
                continue
            per_month.setdefault(ref[:7], set()).add(ref)
        months = sorted(per_month)
        interior = months[1:-1]  # 首月/末月可能被抓取窗截斷
        if not interior:
            continue
        for ym in interior:
            cnt = len(per_month[ym])
            checked.append((iid, ym, cnt))
            if cnt < MIN_WEEKLY_EVENTS_PER_MONTH:
                bad.append(f"{iid} {ym} 只有 {cnt} 筆（門檻 {MIN_WEEKLY_EVENTS_PER_MONTH}）")
    if bad:
        return FAIL, ("週頻指標事件數不足，疑似漏設 weekly:true 導致同月覆蓋："
                      + "；".join(bad))
    if not checked:
        return SKIP, f"weekly 指標 {weekly_ids} 尚無完整月份可檢查（資料累積不足）"
    lo = min(checked, key=lambda t: t[2])
    return PASS, (f"weekly 指標 {len(checked)} 個完整月份都 ≥ {MIN_WEEKLY_EVENTS_PER_MONTH} 筆，"
                  f"最少的是 {lo[0]} {lo[1]} {lo[2]} 筆")


def check_ai_analysis_metadata(ctx):
    """ai_analysis 與 analyzed_at 必須同時存在。

    只有 analyzed_at 沒 ai_analysis → 下一輪會重打 Claude（燒 API 錢）；
    只有 ai_analysis 沒 analyzed_at → 無法追蹤何時分析、也擋不住重複呼叫。
    """
    if ctx.events is None:
        return NO_EVENTS
    only_ts, only_text, future_ts = [], [], []
    for key, ev in ctx.events.items():
        has_text, ts = bool(ev.get("ai_analysis")), ev.get("analyzed_at")
        if ts and not has_text:
            only_ts.append(key)
        if has_text and not ts:
            only_text.append(key)
        if ts:
            try:
                if _dt(ts) > ctx.now + timedelta(hours=1):
                    future_ts.append(f"{key}({ts})")
            except ValueError:
                future_ts.append(f"{key}(無法解析 {ts!r})")
    if only_ts or only_text or future_ts:
        return FAIL, (f"analyzed_at 有但 ai_analysis 缺 {len(only_ts)} 筆 {only_ts[:2]}（下輪會重打 API）；"
                      f"反向 {len(only_text)} 筆 {only_text[:2]}；時間戳異常 {future_ts[:2]}")
    n = sum(1 for ev in ctx.events.values() if ev.get("analyzed_at"))
    return PASS, f"{n} 筆已分析事件的 ai_analysis / analyzed_at 成對且時間戳合理"


def check_ai_disabled_not_analyzed(ctx):
    """indicators.yml 標 ai:false 的指標不該有任何 AI 判讀。

    us_claims 是週頻（一年 52 次），旗標被忽略等於每年多燒 52 次 Claude 呼叫，
    而且不會有任何錯誤訊息——只能靠這條抓。
    """
    if ctx.events is None:
        return NO_EVENTS
    off = [i["id"] for i in ctx.indicators if i.get("ai") is False]
    if not off:
        return SKIP, "沒有 ai:false 的指標"
    leaked = [k for k, ev in ctx.events.items()
              if ev.get("indicator") in off and (ev.get("ai_analysis") or ev.get("analyzed_at"))]
    if leaked:
        return FAIL, f"ai:false 的指標 {off} 竟有 {len(leaked)} 筆 AI 判讀（白燒 API）：{leaked[:3]}"
    return PASS, f"ai:false 指標 {off} 皆無 AI 判讀"


def check_ai_analysis_coverage(ctx):
    """反向檢查：該分析卻沒分析的事件。

    公布後 AI_COVERAGE_GRACE_HOURS 內不算（等下一輪 workflow）。
    全檔完全沒有任何分析 → 視為 AI 層未啟用（無 API key）→ SKIP，不做無意義的告警。
    """
    if ctx.events is None:
        return NO_EVENTS
    targets = []
    for key, ev in ctx.events.items():
        if ctx.by_id.get(ev.get("indicator"), {}).get("ai") is False:
            continue
        if not ev.get("actual"):
            continue
        targets.append((key, ev))
    if not targets:
        return SKIP, "沒有需要 AI 判讀的已公布事件"
    analyzed = [k for k, ev in targets if ev.get("ai_analysis")]
    if not analyzed:
        return SKIP, (f"{len(targets)} 筆待判讀事件全無分析，視為 AI 層未啟用"
                      "（無 ANTHROPIC_API_KEY 或狀態尚未更新），跳過覆蓋率檢查")
    cutoff = ctx.now - timedelta(hours=AI_COVERAGE_GRACE_HOURS)
    missing = []
    for key, ev in targets:
        if ev.get("ai_analysis"):
            continue
        try:
            if _dt(ev["sort_key"]) > cutoff:
                continue  # 剛公布，還在寬限期
        except (ValueError, KeyError):
            pass
        missing.append(key)
    if missing:
        return WARN, (f"{len(missing)}/{len(targets)} 筆已公布逾 {AI_COVERAGE_GRACE_HOURS} 小時"
                      f"的事件沒有 AI 判讀（API 失敗或被拒？）：{missing[:3]}")
    return PASS, f"{len(analyzed)}/{len(targets)} 筆待判讀事件都有 AI 判讀"


def check_charts_shape(ctx):
    """charts.json 每組 labels 與各 dataset 長度一致，且 indicators.yml 指到的圖都存在。"""
    if ctx.charts is None:
        return SKIP, "charts.json 不存在（本機未還原 output/）"
    bad = []
    for cid, c in ctx.charts.items():
        labels = c.get("labels")
        if not labels:
            bad.append(f"{cid}: labels 空")
            continue
        for ds in c.get("datasets", []):
            if len(ds.get("data", [])) != len(labels):
                bad.append(f"{cid}/{ds.get('label')}: data {len(ds.get('data', []))} 筆 vs labels {len(labels)} 筆")
    if bad:
        return FAIL, f"charts.json 結構不一致：{'; '.join(bad[:3])}"
    wanted = {i["chart"] for i in ctx.indicators if i.get("chart")}
    missing = sorted(wanted - set(ctx.charts))
    if missing:
        return WARN, f"indicators.yml 指到但 charts.json 沒有的圖：{missing}（FRED/來源抓取失敗？）"
    return PASS, f"{len(ctx.charts)} 組圖表 labels/datasets 長度一致，指標引用的圖都在"


def check_charts_freshness(ctx):
    """趨勢圖最新一點不能太舊——update.py 吞掉 FRED 失敗只印訊息，圖會靜靜地凍住。"""
    if ctx.charts is None:
        return SKIP, "charts.json 不存在（本機未還原 output/）"
    cur_ym = f"{ctx.now:%Y-%m}"
    stale, ages = [], []
    for cid, c in ctx.charts.items():
        labels = c.get("labels") or []
        if not labels or not re.match(r"^\d{4}-\d{2}", str(labels[-1])):
            continue
        months = _months_between(str(labels[-1])[:7], cur_ym)
        ages.append((cid, months))
        if months * 30 > CHART_STALE_DAYS:
            stale.append(f"{cid} 最新 {labels[-1]}（落後約 {months} 個月）")
    if stale:
        return WARN, f"趨勢圖資料過舊（門檻 {CHART_STALE_DAYS} 天）：{'; '.join(stale)}"
    if not ages:
        return SKIP, "沒有可判斷月份的圖表 labels"
    worst = max(ages, key=lambda t: t[1])
    return PASS, f"{len(ages)} 組圖表都在 {CHART_STALE_DAYS} 天內，最舊的是 {worst[0]} 落後 {worst[1]} 個月"


def check_tsmc_rev_yoy(ctx):
    """tsmc_rev.json 自我一致：存檔 YoY 要能由序列自己算回來。"""
    if ctx.tsmc is None:
        return SKIP, "tsmc_rev.json 不存在（本機未還原 output/）"
    series = {ym: rec.get("revenue_bn") for ym, rec in ctx.tsmc.items()
              if isinstance(rec, dict) and rec.get("revenue_bn")}
    bad, n = [], 0
    for ym, rec in sorted(ctx.tsmc.items()):
        if not isinstance(rec, dict) or rec.get("yoy") is None:
            continue
        calc = _yoy(series, ym)
        if calc is None:
            continue
        n += 1
        if abs(calc - rec["yoy"]) > TSMC_YOY_TOL:
            bad.append(f"{ym}: 存 {rec['yoy']}% vs 自算 {calc:.1f}%")
    if bad:
        return FAIL, f"{len(bad)} 個月的 YoY 與營收序列對不起來：{'; '.join(bad[:3])}"
    if not n:
        return SKIP, "tsmc_rev.json 還沒有可交叉驗證的月份（序列不足 13 個月）"
    return PASS, f"{n} 個月的月營收 YoY 與序列自算一致（容忍 {TSMC_YOY_TOL}pp）"


def check_ism_history_matches_events(ctx):
    """ism_history.json 是趨勢圖唯一的資料來源，必須與 events 的 actual 對得上。"""
    if ctx.events is None:
        return NO_EVENTS
    if ctx.ism is None:
        return SKIP, "ism_history.json 不存在（本機未還原 output/）"
    bad, n = [], 0
    for ev in ctx.events.values():
        if ev.get("indicator") != "us_ism" or not ev.get("actual"):
            continue
        ref = ev.get("ref") or ev.get("date_tw", "")[:7]
        val, _ = _surface(ev["actual"])
        if val is None:
            continue
        n += 1
        got = ctx.ism.get(ref)
        if got is None:
            bad.append(f"{ref}: events 有 actual {ev['actual']} 但 ism_history 沒這個月")
        elif abs(got - val) > 1e-6:
            bad.append(f"{ref}: ism_history {got} vs events actual {ev['actual']}")
    if bad:
        return FAIL, f"ISM 歷史序列與事件不符：{'; '.join(bad[:3])}"
    if not n:
        return SKIP, "還沒有已公布的 ISM 事件"
    return PASS, f"{n} 筆 ISM actual 都與 ism_history.json 一致"


def check_page_rendered(ctx):
    """index.html 存在、每個指標都有卡片（模板改壞會靜靜少掉整張卡）。"""
    if not INDEX_HTML.exists():
        return SKIP, "output/index.html 不存在（本機未還原 output/）"
    html = INDEX_HTML.read_text(encoding="utf-8")
    if len(html) < 5000:
        return FAIL, f"index.html 只有 {len(html)} 字元，疑似渲染失敗"
    missing = [i["name"] for i in ctx.indicators if i["name"] not in html]
    if missing:
        return FAIL, f"index.html 缺少指標卡：{missing}"
    return PASS, f"index.html {len(html):,} 字元，{len(ctx.indicators)} 個指標卡都在"


def check_page_generated_freshness(ctx):
    """index.html 的「更新於」時間。GitHub 排程被停用/大幅延遲時，這條會先亮。"""
    if not INDEX_HTML.exists():
        return SKIP, "output/index.html 不存在（本機未還原 output/）"
    m = re.search(r"更新於\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", INDEX_HTML.read_text(encoding="utf-8"))
    if not m:
        return WARN, "index.html 找不到「更新於」時間戳，模板可能改過"
    gen = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=TPE)
    hours = (ctx.now - gen).total_seconds() / 3600
    if hours > PAGE_STALE_HOURS:
        return WARN, f"index.html 更新於 {m.group(1)}，已過 {hours:.1f} 小時（門檻 {PAGE_STALE_HOURS}）"
    return PASS, f"index.html 更新於 {m.group(1)}（{hours:.1f} 小時前，門檻 {PAGE_STALE_HOURS}）"


# ── Tier B（外部交叉源） ──────────────────────────────────────────────

def _fred_series(sid):
    """{'YYYY-MM': float}；免 API key 的 fredgraph.csv。"""
    import csv
    import io

    import requests
    r = requests.get(FRED_CSV.format(sid=sid), timeout=HTTP_TIMEOUT, headers=UA)
    r.raise_for_status()
    out = {}
    for row in csv.reader(io.StringIO(r.text)):
        if len(row) != 2:
            continue
        try:
            out[row[0][:7]] = float(row[1])
        except ValueError:
            continue
    return out


def _fred_vs_tv(ctx, source, sid, indicator, title, compute, tol, unit, label):
    """共用骨架：抓一條 FRED 序列，跟 events.json 裡對應的 TV actual 比。

    compute(series, ref) -> FRED 換算後的數值（與 TV 表面單位同一單位）。
    """
    if ctx.events is None:
        return NO_EVENTS
    hit = ctx.latest_actual(indicator, title)
    if hit is None:
        return SKIP, f"events.json 還沒有 {title} 的 actual"
    key, ev = hit
    ref = ev.get("ref") or ev.get("date_tw", "")[:7]
    tv, _suffix = _surface(ev.get("actual"))
    if tv is None:
        return SKIP, f"{key} 的 actual {ev.get('actual')!r} 解析不出數字"
    if not ctx.take_call(source):
        return SKIP, f"本次輪替未包含 {source}（每次執行上限 {TIER_B_MAX_CALLS} 次外部呼叫）"
    try:
        series = _fred_series(sid)
    except Exception as e:  # noqa: BLE001
        return SKIP, f"FRED {sid} 取得失敗（對方問題不算我們資料錯）：{type(e).__name__}: {e}"
    fred_val = compute(series, ref)
    if fred_val is None:
        newest = max(series) if series else "?"
        return SKIP, f"FRED {sid} 尚無 {ref} 的資料（最新 {newest}），等下次公布"
    diff = tv - fred_val
    detail = (f"{label} {ref}：TV actual {tv:g}{unit} vs FRED {sid} {fred_val:.2f}{unit}，"
              f"差 {diff:+.2f}{unit}（容忍 {tol}{unit}）")
    return (FAIL, "TradingView 與 FRED 對不起來 → " + detail) if abs(diff) > tol else (PASS, detail)


def check_fred_cpi(ctx):
    """美國 CPI YoY：TV actual vs FRED CPIAUCSL 自算年增。"""
    return _fred_vs_tv(ctx, "fred-cpi", "CPIAUCSL", "us_cpi", "Inflation Rate YoY",
                       lambda s, ref: _yoy(s, ref), CPI_YOY_TOL, "%", "CPI YoY")


def check_fred_nfp(ctx):
    """非農就業：TV actual（K = 千人）vs FRED PAYEMS 月增（單位本來就是千人）。"""
    def compute(series, ref):
        cur, prev = series.get(ref), series.get(_prev_ym(ref))
        return None if cur is None or prev is None else cur - prev

    return _fred_vs_tv(ctx, "fred-nfp", "PAYEMS", "us_nfp", "Non Farm Payrolls",
                       compute, NFP_DIFF_TOL_K, "K", "NFP 月增")


def check_fred_pce(ctx):
    """核心 PCE YoY：TV actual vs FRED PCEPILFE 自算年增。"""
    return _fred_vs_tv(ctx, "fred-pce", "PCEPILFE", "us_pce", "Core PCE Price Index YoY",
                       lambda s, ref: _yoy(s, ref), PCE_YOY_TOL, "%", "核心 PCE YoY")


def check_twse_tsmc_rev(ctx):
    """台積電月營收：tsmc_rev.json vs TWSE openapi t187ap05 直打 2330。"""
    if ctx.tsmc is None:
        return SKIP, "tsmc_rev.json 不存在（本機未還原 output/）"
    local_ym = max((ym for ym in ctx.tsmc if re.match(r"^\d{4}-\d{2}$", ym)), default=None)
    if local_ym is None:
        return SKIP, "tsmc_rev.json 還沒有任何月份"
    if not ctx.take_call("twse-tsmc-rev"):
        return SKIP, f"本次輪替未包含 twse-tsmc-rev（每次執行上限 {TIER_B_MAX_CALLS} 次外部呼叫）"
    try:
        import requests
        r = requests.get(TWSE_REV_URL, timeout=HTTP_TIMEOUT, headers=UA)
        r.raise_for_status()
        rec = next((x for x in r.json() if x.get("公司代號") == "2330"), None)
    except Exception as e:  # noqa: BLE001
        return SKIP, f"TWSE t187ap05 取得失敗（對方問題）：{type(e).__name__}: {e}"
    if rec is None:
        return SKIP, "TWSE t187ap05 這批沒有 2330（月中尚未公告）"
    roc = rec["資料年月"]
    remote_ym = f"{int(roc[:3]) + 1911}-{int(roc[3:]):02d}"
    remote_bn = round(float(rec["營業收入-當月營收"]) / 1e6, 1)
    remote_yoy = round(float(rec["營業收入-去年同月增減(%)"]), 1)
    if remote_ym != local_ym:
        gap = _months_between(local_ym, remote_ym)
        if gap > 0:
            return WARN, (f"TWSE 已公告 {remote_ym}（{remote_bn} 十億），"
                          f"但 tsmc_rev.json 最新只到 {local_ym}——本輪 update.py 還沒跑到？")
        return FAIL, f"tsmc_rev.json 最新 {local_ym} 比 TWSE 公告的 {remote_ym} 還新，狀態檔可疑"
    local = ctx.tsmc[local_ym]
    d_bn = local.get("revenue_bn", 0) - remote_bn
    d_yoy = local.get("yoy", 0) - remote_yoy
    detail = (f"{local_ym}：本地 {local.get('revenue_bn')} 十億 / {local.get('yoy')}% YoY vs "
              f"TWSE {remote_bn} 十億 / {remote_yoy}%（差 {d_bn:+.1f} / {d_yoy:+.1f}）")
    if abs(d_bn) > TSMC_REV_TOL_BN or abs(d_yoy) > TSMC_TWSE_YOY_TOL:
        return FAIL, "台積電月營收與 TWSE 官方對不起來 → " + detail
    return PASS, detail


# ── 執行 ──────────────────────────────────────────────────────────────

CHECKS_A = [
    ("events-schema", check_events_schema),
    ("events-indicator-registered", check_events_indicator_registered),
    ("events-freshness", check_events_freshness),
    ("calendar-lookahead", check_calendar_lookahead),
    ("surprise-recompute", check_surprise_recompute),
    ("surprise-surface-unit", check_surprise_surface_unit),
    ("weekly-events-per-month", check_weekly_events_per_month),
    ("ai-analysis-metadata", check_ai_analysis_metadata),
    ("ai-disabled-not-analyzed", check_ai_disabled_not_analyzed),
    ("ai-analysis-coverage", check_ai_analysis_coverage),
    ("charts-shape", check_charts_shape),
    ("charts-freshness", check_charts_freshness),
    ("tsmc-rev-yoy-selfconsistent", check_tsmc_rev_yoy),
    ("ism-history-matches-events", check_ism_history_matches_events),
    ("page-rendered", check_page_rendered),
    ("page-generated-freshness", check_page_generated_freshness),
]

CHECKS_B = [
    ("fred-cpi-vs-tv", check_fred_cpi),
    ("fred-nfp-vs-tv", check_fred_nfp),
    ("fred-pce-vs-tv", check_fred_pce),
    ("twse-tsmc-rev-vs-local", check_twse_tsmc_rev),
]


def _rotate_sources(today):
    """來源數 > 呼叫預算時依日期輪替，讓每個來源都會被跑到。"""
    n = len(TIER_B_SOURCES)
    if n <= TIER_B_MAX_CALLS:
        return list(TIER_B_SOURCES)
    start = today.toordinal() % n
    return [TIER_B_SOURCES[(start + i) % n] for i in range(TIER_B_MAX_CALLS)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="macro_dashboard 資料驗證")
    ap.add_argument("--tier", choices=["a", "b", "all"], default="all")
    ap.add_argument("--sources", default=None,
                    help=f"覆蓋 Tier B 的來源輪替（逗號分隔，可選：{','.join(TIER_B_SOURCES)}）")
    ap.add_argument("--force-fail", action="store_true",
                    help="注入一條必定 FAIL 的檢查，用來驗證告警管道會不會寄信"
                         "（也可設環境變數 VERIFY_FORCE_FAIL=1）")
    args = ap.parse_args(argv)

    sources = (args.sources.split(",") if args.sources
               else _rotate_sources(datetime.now(TPE).date()))
    ctx = Ctx(sources)

    checks = []
    if args.tier in ("a", "all"):
        checks += CHECKS_A
    if args.tier in ("b", "all"):
        checks += CHECKS_B

    # 告警管道自我測試：注入一條必定 FAIL，用來確認
    # 「FAIL → exit 1 → workflow 紅 → GitHub 寄信」這條路徑真的通。
    # 這是整套驗證的單點故障——不寄信的話所有檢查都是白寫的，而它平常沒有任何
    # 訊號會告訴你它壞了（通知設定被改、email 變更、GitHub 行為調整都是無聲的）。
    # opt-in，預設不啟用；刻意留著不拆，之後想重驗隨時 dispatch 一次即可。
    if args.force_fail or os.environ.get("VERIFY_FORCE_FAIL", "").lower() in ("1", "true", "yes"):
        checks = list(checks) + [(
            "force-fail",
            lambda _ctx: (FAIL, "人為注入的失敗，用於驗證告警管道是否真的會寄信。"
                                "看到這行代表驗證流程本身正常運作。"),
        )]

    counts = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0}
    for cid, fn in checks:
        try:
            status, msg = fn(ctx)
        except Exception as e:  # noqa: BLE001
            status, msg = FAIL, f"檢查本身拋例外 {type(e).__name__}: {e}"
        counts[status] += 1
        print(f"[{status}] {cid} — {msg}")

    print(f"verify: {counts[PASS]} passed, {counts[FAIL]} failed, "
          f"{counts[WARN]} warned, {counts[SKIP]} skipped (tier={args.tier})")
    if counts[FAIL]:
        return 1
    return 2 if counts[WARN] else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
