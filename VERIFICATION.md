# 資料正確性驗證

> 2026-08-01 規劃、2026-08-02 實作。四專案通用框架（Tier A 每次 CI / Tier B 每日交叉源 / Tier C golden）。
> 本檔描述**現況**，不是計畫。

## 怎麼跑

```bash
python verify.py --tier a      # 零外部呼叫，<1 秒
python verify.py --tier b      # 交叉源，會打 FRED / TWSE
python verify.py               # 預設 all
```

exit code：`0` 全過（或只有 SKIP）／`1` 至少一條 FAIL／`2` 沒 FAIL 但有 WARN。CI 只把 `1` 當失敗。

本站資料在 `output/`（從 gh-pages 還原）。本機執行時 `output/` 可能不存在，該情況全部回 SKIP 而非 FAIL。

**驗證不呼叫 Anthropic API**（會燒錢）。AI 判讀層只驗到 metadata 層級。

## CI 掛法

- **Tier A** → `.github/workflows/macro_update.yml`，位置在 `update.py` 之後、gh-pages 部署之前。FAIL 擋掉部署並讓 workflow 變紅。
- **Tier B** → `.github/workflows/verify.yml`，每日台北 23:30（UTC 15:30）+ `workflow_dispatch`。

`verify.yml` 跑的是 `--tier all` 而非只有 `--tier b`：它還原的是 **gh-pages 上實際部署中**的狀態，在那裡跑 Tier A 是零網路零成本，卻能驗到「線上頁面」而不只是「剛生成的檔案」（`page-generated-freshness` 只有在這裡才有意義）。它的 FAIL 一樣只讓 verify.yml 變紅。

## 新鮮度的在地化

本 repo 沒有交易日概念，台股那套「落後 N 交易日」不適用。改成**依各指標公布頻率**設窗（`FRESHNESS_DAYS`）：

| 指標 | 窗 | 理由 |
|---|---|---|
| 月頻（CPI/NFP/ISM/出口…） | 45 天 | 公布頻率 + 假日寬限 |
| `us_claims`（週頻） | 14 天 | |
| `us_fomc`（不定期） | 100 天 | |
| `tsmc_rev` | 55 天 | 見下 |

`tsmc_rev` 的 `sort_key` 是「參考月-27」而非公布日（`fetchers/tsmc.py`），6 月營收（實際 7/10 公布）從 6/27 起算，最壞會到 44 天，所以窗放寬到 55。若之後想跟其他指標一致，改成用公布日當 sort_key 比較乾淨（會動到既有 key 格式）。

`indicators.yml` 加了新指標但沒補門檻 → WARN 提醒，不會誤 FAIL。

## Tier A（16 條，零外部呼叫）

| check-id | 驗什麼 |
|---|---|
| `events-schema` | 事件欄位齊全、`sort_key` 可解析 |
| `events-indicator-registered` | 事件的 indicator 都註冊在 `indicators.yml` |
| `events-freshness` | 各指標在公布窗內 |
| `calendar-lookahead` | 未來待公布事件不得枯竭 |
| `surprise-recompute` | surprise = actual − forecast 重算（含 delta/verdict/good） |
| `surprise-surface-unit` | 表面單位規則：`-53K` 而非 `-53,000` |
| `weekly-events-per-month` | weekly 指標每個**完整**月份 ≥3 筆 |
| `ai-analysis-metadata` | `ai_analysis` 與 `analyzed_at` 成對、時間戳合理 |
| `ai-disabled-not-analyzed` | `ai: false` 的指標不得被分析 |
| `ai-analysis-coverage` | 反向抓「該分析卻沒分析」 |
| `charts-shape` | 圖表 labels/datasets 長度一致 |
| `charts-freshness` | 圖表資料在 120 天內 |
| `tsmc-rev-yoy-selfconsistent` | 月營收 YoY 與序列自算一致 |
| `ism-history-matches-events` | `ism_history.json` 與事件 actual 一致 |
| `page-rendered` | `index.html` 各指標卡都在 |
| `page-generated-freshness` | 頁面更新時間 ≤24 小時 |

幾個要點：

**surprise 重算是獨立實作**，不 import `processors/surprise.py`——驗證不共用被驗對象的程式，否則邏輯錯了兩邊會一起錯。表面單位另立一條 check，直接抓 `-53K` 退化成 `-53,000` 這種。

**`weekly-events-per-month` 只檢查首尾之間的完整月份**：repo 冷啟動時 TV 只抓過去 40 天，首月一定不完整，硬檢查會天天紅。interior 規則自動排除首末月。這條是防「漏設 `weekly: true` → 同月各週互相覆蓋」的迴歸。

**`analyzed_at` 雙向驗**：`ai-analysis-metadata` 抓「有 `analyzed_at` 但缺 `ai_analysis`」（下輪必定重打 Claude = 燒錢）與反向；`ai-disabled-not-analyzed` 抓 `ai: false` 的指標竟被分析（旗標失效 = 每年白燒 52 次）；`ai-analysis-coverage` 反向抓該分析卻沒分析，公布後 8 小時寬限，全檔零分析時判為 AI 層未啟用而 SKIP（避免無 key 環境天天告警）。

**`events-indicator-registered` 是 gh-pages 持久化模式特有的雷**：狀態永久累積，`indicators.yml` 改過 id 之後舊事件會一直留在 events.json，在頁面上變成沒有邏輯卡、tier 預設 2 的孤兒列，沒有任何機制會清掉它。

**`calendar-lookahead` 是最早的斷供訊號**：`update.py` 把 TV 失敗吞成一行 print 然後繼續跑，頁面照常部署；資料要落後 45 天才會被 `events-freshness` 抓到，但「未來行事曆枯竭」在斷供後幾天內就會出現。`charts-*` 同理——FRED 失敗被吞成「沿用舊圖」，趨勢圖可以凍住好幾個月而完全無感。

## Tier B（4 條，交叉源）

| check-id | 驗什麼 |
|---|---|
| `fred-cpi-vs-tv` | CPI YoY：FRED CPIAUCSL vs events.json 的 TV actual |
| `fred-nfp-vs-tv` | NFP 月增：FRED PAYEMS vs TV actual |
| `fred-pce-vs-tv` | 核心 PCE YoY：FRED PCEPILFE vs TV actual |
| `twse-tsmc-rev-vs-local` | `tsmc_rev.json` vs TWSE openapi t187ap05 直打 2330 |

**FRED 交叉驗證是本專案的天然第二源**：TV 是非官方 endpoint，哪天改版餵錯資料，這條當天抓到。`fredgraph.csv` 免 key、專案本來就在用（趨勢圖）。注意單位換算：NFP 千人、CPI YoY %。

**⚠️ FRED UA 雷**（2026-08-02 實測踩到）：`fredgraph.csv` 對不帶 `Mozilla/5.0` 前綴的 UA 會**直接吊死連線**（read timeout 45 秒，不是回 403），要沿用 `fetchers/__init__.py` 的 UA。

**呼叫預算**：4 個來源 > 3 次上限，依日期**輪替**（每來源 4 天內跑滿 3 次），被輪掉的回 SKIP 並在訊息裡講明原因。呼叫間隔 ≥1 秒。外部源掛掉／超時／非 200 一律 SKIP。

## Cross-repo

本 repo 資料源（TV / FRED / 關務署）與其他三個台股專案不重疊，**不參與 cross-repo 互驗**；FRED 即為本專案的獨立第二源。

## 沒做的

- **Tier C golden regression**：AI 輸出非決定性，golden 只驗得到 **prompt 組裝為止**（固定 events snapshot → 跑到 ai_analysis 的 prompt 生成 → diff prompt 文字）；fetch/processor 層可用固定 TV 回應 fixture 驗 surprise 計算。這輪刻意沒做。
- **`|surprise|` 異常值監控**：見下面的 forecast 單點失效。repo 才上線一個月、樣本不足以定門檻，建議累積 3–6 個月後再加。

## 已知的結構風險

**① forecast 沒有第二源，是 surprise 的單點失效。** FRED 只有 actual、沒有 consensus，所以 TV 若把 forecast 餵錯（改版、欄位錯位），`surprise` 會算出一個**看起來完全合理但錯誤**的落差，AI 判讀還會照著它寫一段煞有介事的分析。目前無解（免費源裡沒有第二個 consensus 來源）。Tier B 抓得到「actual 錯」，抓不到「forecast 錯」。

**② GitHub 會在 repo 連續 60 天沒有 commit 活動後自動停用 scheduled workflow。** 本 repo 是純資料更新、原始碼可能好幾個月不動，很容易踩到。踩到時 `macro_update.yml` 和 `verify.yml` 會**一起**被停用，所以「用 verify.yml 監控 macro_update 有沒有在跑」這招在這個失效模式下沒用。`page-generated-freshness`（24 小時門檻）能在「排程還活著但 update 一直失敗」時亮，但擋不了排程整個被停。真正的解是掛外部 cron 打 `repository_dispatch`（不受 60 天規則影響），順便讓那個外部 cron 也打一發到 verify.yml。

## ⚠️ 未驗證的前提

整套的告警依賴「FAIL → exit 1 → workflow 紅 → GitHub 寄信」。**這條路徑還沒實測過**，而且本 repo 也走 `repository_dispatch`——GitHub 的失敗通知是綁 scheduled run 的，dispatch 觸發的失敗會不會寄信未確認。不寄信的話這些檢查全是白寫的。
