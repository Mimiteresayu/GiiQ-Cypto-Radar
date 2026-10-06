# C48-2 Hummingbot — 鎖死 config 筆記（Forge · 2026-10-05 21:41 HKT）

Gate：`/workspace/giiq/cove/c48_2_gate_2026-10-05.md`  
卡：`/workspace/giiq/scout/C48-2_hummingbot_2026-10-05.md`  
Upstream pin 目標：`hummingbot/hummingbot` image **`version-2.16.0`**（或 PR 內鎖定 digest）。

## 硬鎖（必須全部滿足；Prism spot-check 對呢張）
| 鍵 | 鎖死值 | 原因 |
|---|---|---|
| strategy | `perpetual_market_making` | 只路徑 1；**禁止** `v2_funding_rate_arb` |
| derivative | `hyperliquid_perpetual_testnet`（優先）／主網要 Harbor+MMT | HL-only；無 BX |
| market | `BTC-USD`（HL perp） | 先 BTC 證明 fills |
| leverage | **2** | ≤2；script 預設 20 禁止 |
| order_levels | **1** | 禁多档加碼 |
| order_level_amount | **0** | 同上 |
| order_amount | 名義 ≈ **NAV 2%**（base 單位；見下） | 絕對 ≤ NAV 8% |
| stop_loss_spread | **>0 必開**（建議 1.0＝1%） | gate 硬鎖 |
| long/short_profit_taking_spread | **>0**（建議 0.5） | 合理止盈 |
| position_mode | `One-way` | 簡單淨敞口 |

## order_amount（base）換算
`order_amount` 係 **base 數量**（唔係 quote）。  
名義 USDT ≈ `order_amount × mid_price`。  
目標名義 = NAV × 0.02。

例（paper 假設）：
- NAV $2,500 → 名義 $50 → BTC@$100k → `order_amount ≈ 0.0005`
- NAV $5,000 → 名義 $100 → `order_amount ≈ 0.001`

實際 BTC 價變時，部署前用 `order_amount = (NAV * 0.02) / mid` 重算；超 8% 唔啟動。

## 停條件（Cove）
- 權益 DD ≥ **4%**
- 單幣名義 > NAV **8%**
- inventory 淨敞口 > 名義上限
→ 停、平、寫報告。

## Railway
- Image：`hummingbot/hummingbot:version-2.16.0`
- `HEADLESS_MODE=true`
- Volume：`/home/hummingbot/data`
- **無大 UI**；log／SQLite 出口畀 Prism／River
- Secrets：HL API／wallet 由 **MMT 自己** 填；唔入 chat／repo
- 真錢 deposit：$0；優先 testnet／paper fills（12–24h）

## 禁止
- funding-arb／第二所
- order_levels > 1
- leverage > 2
- 改 Hummingbot 源碼（只 config＋deploy wrapper）
- 未過 YELLOW 就真錢／merge 當 live

## 檔
- `conf_perpetual_market_making_c48_2.yml` — 鎖死策略樣板
- `assert_locks.py` — Prism／CI 可跑硬鎖 assert
- 正式 Railway／Dockerfile／railway 服務定義 → cloud agent PR（repo `GiiQ-Cypto-Radar`）
