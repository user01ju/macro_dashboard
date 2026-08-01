"""數據公布後的當次 AI 分析：同一指標同一天的新 actual 打包成一次 Claude 呼叫。

- 金鑰走 ANTHROPIC_API_KEY 環境變數；沒有金鑰就跳過（頁面顯示無分析，不擋 build）。
- 每組事件只分析一次（analyzed 標記存在 events.json 內），成本 ≈ 每月數十次呼叫。
"""
import os
from collections import defaultdict
from datetime import datetime, timezone

MODEL = "claude-opus-5"

SYSTEM = (
    "你是台股/美股總經分析師。使用者是量化投資專家，繁體中文、terse、先講結論。"
    "根據給定的經濟數據 surprise（實際 vs 預期 vs 前值）寫當次判讀，120-250 字："
    "①surprise 方向與幅度的意義 ②對利率預期/美股的影響邏輯 ③對台股的傳導（若相關）。"
    "沒有 forecast 的數據就 vs 前值判讀。不要免責聲明、不要廢話。高度推測要標註。"
)


def run(events, indicators_by_id):
    """對尚未分析且已有 actual 的事件產生分析，寫回 event['ai_analysis']。回傳呼叫次數。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[ai] 無 ANTHROPIC_API_KEY，跳過 AI 分析")
        return 0

    groups = defaultdict(list)  # (indicator, date) -> [(key, event)]
    for key, ev in events.items():
        if indicators_by_id.get(ev["indicator"], {}).get("ai") is False:
            continue  # 週頻等高頻指標只進表格，不做 AI 判讀
        if ev.get("actual") and not ev.get("ai_analysis"):
            date = ev.get("date_tw", "")[:10]
            groups[(ev["indicator"], date)].append((key, ev))
    if not groups:
        return 0

    import anthropic
    client = anthropic.Anthropic()
    calls = 0
    for (ind_id, date), items in groups.items():
        ind = indicators_by_id.get(ind_id, {})
        lines = [f"指標：{ind.get('name', ind_id)}（{date} 公布）", "背景邏輯：", ind.get("logic", ""), "", "本次數據："]
        for _, ev in items:
            lines.append(
                f"- {ev['title']}: 實際 {ev['actual'] or 'N/A'} / 預期 {ev['forecast'] or 'N/A'} / 前值 {ev['previous'] or 'N/A'}"
            )
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=1000,
                system=SYSTEM,
                messages=[{"role": "user", "content": "\n".join(lines)}],
            )
            if resp.stop_reason == "refusal":
                print(f"[ai] {ind_id} {date} 被拒絕，跳過")
                continue
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception as e:  # noqa: BLE001
            print(f"[ai] {ind_id} {date} 失敗: {e}")
            continue
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for _, ev in items:
            ev["ai_analysis"] = text
            ev["analyzed_at"] = ts
        calls += 1
        print(f"[ai] {ind_id} {date} 分析完成")
    return calls
