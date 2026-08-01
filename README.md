# macro_dashboard — 總經數據儀表板

追蹤對股市有實質影響的經濟數據：**CPI · NFP · FOMC · ISM · 台灣出口 · 台積電月營收/法說**。
每筆數據顯示 預期/實際/落差，公布後由 Claude 自動生成當次判讀；每個指標附靜態邏輯卡（傳導機制）與歷史趨勢圖。

🔗 線上：https://user01ju.github.io/macro_dashboard/

## 架構

```
indicators.yml            指標註冊表（新增指標 = 加一段 config，tv ticker 就能自動抓）
fetchers/
  tradingview.py          行事曆 + forecast/actual/previous（美系全項 + 台灣出口美元 YoY）
  fred.py                 歷史序列（fredgraph.csv，免 API key）→ 趨勢圖
  mof_tw.py               台灣出口 NTD 序列（關務署 open data CSV，只餵趨勢圖）
  tsmc.py                 台積電月營收（TWSE openapi + FinMind 冷啟動回補）
processors/surprise.py    落差計算 + 好壞方向標記
analysis/ai_analysis.py   公布後當次分析（claude-opus-5，同指標同日打包一次呼叫）
generator/                Jinja2 → output/index.html（行事曆 / 近期公布 / 邏輯卡+圖）
manual/overrides.json     手動補值（台灣出口美元頭條、台積電法說日期）
output/data/*.json        狀態，靠 gh-pages 在 CI runner 間持久化
```

## 本機執行

```bash
pip install -r requirements.txt
python update.py          # 產出 output/index.html
```

AI 分析需要 `ANTHROPIC_API_KEY` 環境變數；沒有就自動跳過（其餘功能不受影響）。

## CI

`.github/workflows/update.yml`：每 3 小時（UTC 分鐘 17）+ `workflow_dispatch` + `repository_dispatch`。
狀態還原：runner 起來先 `git archive origin/gh-pages | tar -x -C output/`。
Secret：`ANTHROPIC_API_KEY`（`gh secret set ANTHROPIC_API_KEY`）。

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
