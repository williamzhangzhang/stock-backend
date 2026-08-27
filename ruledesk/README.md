# RuleDesk 风控补丁

线上系统是 [RuleDesk · 纸上交易台](https://lab.williamchang.site/)，源码在你本机的 `paper-trading-lab`（Next.js），**不在**这个 `stock-backend` 仓库里。GitHub 上也没有 `paper-trading-lab` 仓库，所以这次把引擎从生产 bundle 还原出来，按上次审计的 6 条改掉，并在这里用测试钉死。

把 `ruledesk/engine.ts` 拷进 `paper-trading-lab`（建议路径 `lib/risk.ts`），替换生产包里这些被压缩过的帮手函数。

| 生产包里的名字 | 换成 |
| --- | --- |
| `R(quote, entry)` | `markPrice(quote)` —— 返回 `null`，不再回退到成本价 |
| `O(quotes, held)` 全局熔断门 | 删掉。锁仓只看 `sessionOpen` + 有报价的仓位 |
| `C(positions, quotes)` | `grossExposure` —— 名义本金 × `leverage` |
| `M(account, quotes)` | `equityOf` |
| `x(...)` / `riskState` | `riskState(account, quotes, clock)` |
| `w` / `openPosition` | `openPosition`（SIM 仍禁止开仓） |
| `processTick` 里的月锁段 | `applyMonthLockAndFlatten` |

UI：仓位行如果 `priced === false` 或 `price === null`，标红，盈亏显示 `—`，不要显示 0%。下单按钮在 `quote.source !== "live"` 时直接禁用（现在只在引擎里拒单，按钮还能点）。

## 改了什么

1. **逐仓 hygiene（P0）**  
   某一只没有 LIVE 报价，只把那一仓标成「估值不可信」。其余 LIVE 仓位的回撤照常触发日/月锁。以前 `held.every(live)` 会让整本账的熔断停摆——持有 FNGU/FNGD 就会中招。

2. **缺价不再回退到成本（P0）**  
   `markPrice` 返回 `null`。回退到 `entry` 等于对外宣称「零盈亏」。

3. **敞口乘杠杆（P0）**  
   3× ETF 用满现金不再显示「100% 合规」，经济敞口是 300%。单票 20% / 总账 100% 的上限也按经济敞口算，所以 3× 最多大约只能用 6.7% 的现金。

4. **月锁改成粘性（P1）**  
   和日锁一样：一旦锁上，要等纽约时间的月份翻过才解除。以前月回撤掉回 10% 以下（包括用 SIM 价假恢复）就会自动解锁。

5. **日回撤 8% → 3.5%（P2）**  
   月锁仍是 10%，这样两级熔断才分层。一天打满日限不再吃掉月度风险预算的 80%。

6. **禁止非 LIVE 下单（P1）**  
   市价、加仓、挂限价都不允许。FNGU/FNGD 目前是 seed 模拟价，不能再进组合。

## 在本仓库跑测试

```bash
pip install pytest
pytest tests/test_ruledesk_engine.py
```

覆盖了上次审计的场景 A/C/D：LIVE 3× 亏损会锁仓且敞口 ×3；一只 SIM 仓位不会冻住整本账的熔断；满仓 3× 报 300% 经济敞口。

## 部署

改完 `paper-trading-lab` 之后，把仓库推到 GitHub，并在 Vercel 项目里接上 Git（仪表盘现在还显示「Connect Git Repository」）。这个 Cloud Agent 碰不到你本机的文件夹，也做不了交互式的 Vercel OAuth。
