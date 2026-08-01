# macro_dashboard — 總經數據儀表板

追蹤對股市有實質影響的經濟數據。每筆顯示 預期/實際/落差，公布後由 Claude 自動生成當次判讀；
每個指標附靜態邏輯卡（傳導機制）與歷史趨勢圖。

| Tier | 指標 |
|---|---|
| T1 | 美國 CPI · 非農就業 · FOMC · 台灣出口 · 台積電月營收 · 台積電法說 |
| T2 | ISM 製造業 PMI · 核心 PCE · 初領失業金（週頻，無 AI 判讀） |

🔗 線上：https://user01ju.github.io/macro_dashboard/

## 架構

```
indicators.yml            指標註冊表（新增指標 = 加一段 config，見下方「新增指標」）
fetchers/
  tradingview.py          行事曆 + forecast/actual/previous（美系全項 + 台灣出口美元 YoY）
  fred.py                 歷史序列（fredgraph.csv，免 API key）→ 趨勢圖
  mof_tw.py               台灣出口 NTD 序列（關務署 open data CSV，只餵趨勢圖）
  tsmc.py                 台積電月營收（TWSE openapi + FinMind 冷啟動回補）
processors/surprise.py    落差計算 + 好壞方向標記
analysis/ai_analysis.py   公布後當次分析（claude-opus-5，同指標同日打包一次呼叫）
generator/                Jinja2 → output/index.html（行事曆 / 近期公布 / 邏輯卡+圖）
manual/overrides.json     手動補值（台積電法說日期、任意事件修正）
output/data/*.json        狀態，靠 gh-pages 在 CI runner 間持久化
```

## 新增指標

多數情況只要在 `indicators.yml` 加一段，不用動程式：

```yaml
  - id: us_retail
    name: 美國零售銷售
    tier: 2
    country: US
    source: tradingview
    tv:
      - {ticker: "ECONOMICS:USRSM"}   # 從 TV 行事曆 API 的 ticker 欄位取得
    direction: higher_is_good         # higher_is_bad | higher_is_good | context
    chart: retail                     # 選用，需在 fetchers/fred.py 補對應序列
    logic: |
      **傳導機制**：…（這張卡是手寫的靜態邏輯，會顯示在頁面下方）
```

可選旗標：

| 欄位 | 用途 |
|---|---|
| `ai: false` | 只進表格，不做 AI 判讀（高頻指標用，省成本兼免洗版） |
| `weekly: true` | 事件 key 保留完整日期。**週頻指標必加**，否則同月各週會互相覆蓋只剩一筆；前端會自動把該指標合併成一張多列卡片 |

查 ticker：`curl -H "Origin: https://www.tradingview.com" "https://economic-calendar.tradingview.com/events?from=<ISO>&to=<ISO>&countries=US,TW"`，
在回傳中找目標事件的 `ticker` 欄位。**沒有 ticker 的事件**改用 `{title: "…"}` 比對（會加上 `country` 條件）。

## 本機執行

```bash
pip install -r requirements.txt
python update.py          # 產出 output/index.html
```

AI 分析需要 `ANTHROPIC_API_KEY` 環境變數；沒有就自動跳過（其餘功能不受影響）。

## CI

`.github/workflows/macro_update.yml`：每 3 小時（UTC 分鐘 17）+ `workflow_dispatch` + `repository_dispatch`（`types: [update]`）。
狀態還原：runner 起來先 `git archive origin/gh-pages | tar -x -C output/`。
Secret：`ANTHROPIC_API_KEY`（`gh secret set ANTHROPIC_API_KEY`）。

AI 呼叫量：每組（指標 × 公布日）只呼叫一次，`analyzed_at` 標記隨 events.json 持久化 → 正常月份約 6–10 次。

## 手動維護點（低頻）

| 項目 | 頻率 | 位置 |
|---|---|---|
| 台積電法說會日期 | 一季一次 | `manual/overrides.json` → `tsmc_calls` |
| 任意事件補值/修正 | 需要時 | `manual/overrides.json` → `events`（key 同 events.json） |

## 已知限制

- **TradingView 行事曆是非官方 endpoint**：斷供時新事件會缺，頁面沿用既有 events（曾評估過的替代品：
  ForexFactory 免費 feed 無 actual 欄位、DBnomics ISM 資料損壞、TE guest API 不含美台）。
- **ISM 歷史圖**：FRED 無此序列（ISM 專有資料），從 TV actual 隨時間累積。
- **台積電月營收**：TWSE openapi 只回最新月，歷史靠 FinMind 冷啟動回補 + 逐月累積；無市場 consensus，只 vs 前值。
- 趨勢圖的台灣出口用關務署 NTD 序列（歷史長），與行事曆的美元 YoY 頭條在匯率大幅波動時有落差。
- **`ECONOMICS:USINTR` ticker 掛在所有 Fed 官員演講/Beige Book 上**，FOMC 那段用 ticker + title 雙條件過濾，
  新增 Fed 相關事件時要照做，否則會撈進幾十筆噪音。
- 評估過不加：**台灣 CPI**（台股非通膨交易市場；TV 上無 forecast，算不出 surprise）。
