---
name: csi1000-monitor
description: Use when user mentions 中证1000 / 中证 1000 / csi1000 / IM 期货贴水 / MO 期权 / 贴水策略, asks to 跑一下监控/拉数据/看报告/看估值/看贴水/查信号, or wants to check whether to enter/reduce/switch IM futures positions based on valuation percentile + basis + call enhancement. Triggers on keywords: csi1000, 中证1000, 贴水, 基差, IM当月/下月/当季/下季, MO2607/2608/2609/2612, PE_TTM 分位, 估值分位.
---

# csi1000-monitor

中证 1000 贴水策略监控器。一键拉数据（PE/PB 历史 + IM 期货 + MO 期权）→ 算多区间估值分位 / 期货基差年化贴水 / 10% OTM call 增厚 → 输出 7 种信号 + Markdown 报告。

不负责：实盘下单、回测、其他指数（沪深 300 / 中证 500 用对应工具）。

## 项目目录

`D:/workspace/csi1000-monitor/` （GitHub: alicexl/csi1000-monitor）

## 用法

入口：`python monitor.py <子命令>`

| 子命令 | 作用 |
|---|---|
| `run` | 自动判断：DB ≥ 目标交易日则跳过拉取，否则先拉数据（网络 30-60 秒）；然后生成报告 |
| `status` | 一行快速查当前信号 |

### 默认执行

用户说"跑一下" / "看看" / 没指定子命令 → 跑 `run`：

```bash
cd D:/workspace/csi1000-monitor && python monitor.py run 2>&1 | grep -v "UserWarning\|from pandas"
```

周末/假日 `get_futures_daily` 返回 0 合约是正常的，工作日重试。

## 策略逻辑（解读数据用）

- **入场条件**：PE_TTM 10 年分位 <50% **且** roll_yield >0（曲线向下倾斜）
- **预警入场**：PE_TTM 分位 50–60%，或估值够低但 roll_yield ≤0（曲线异常）
- **减仓条件**：PE_TTM 10 年分位 >85% **或** roll_yield ≤0（曲线扁平/倒挂）
- **预警减仓**：PE_TTM 分位 75–85%
- **合约切换**：当月剩余天数 <7 天时切下月
- **卖 Call 增厚**：持有 IM 多头时卖 10% OTM 当月 call 增厚收益

策略本质：低估时入场，吃 backwardation 下的展期收益（roll_yield）+ 估值上涨。曲线斜率 >0 才持有；曲线扁平/倒挂立即离场。

## 估值反推：预测入场点位（2026-07-19 测算）

用户问"指数还要跌多少才能入场"时，用以下口径反推。

### 核心公式

```
当前隐含盈利 E = close / PE_TTM
当前隐含净资产 B = close / PB

要触发 PE_TTM 分位 <X%：close = (历史 PE_TTM X% 分位) × E
要触发 PB    分位 <X%：close = (历史 PB    X% 分位) × B
```

### 2026-07-19 测算结果（当前 close=7168, PE_TTM=30.6, PB=2.28）

| 口径 | 触发阈值 | 对应指数 | 距 7168 |
|---|---|---|---|
| **PE_TTM 10y 50% 分位**（入场线）| PE=26.09 | **6116** | −14.7% |
| **PB 10y 10% 分位**（稳健底）| PB=1.93 | **6068** | −15.3% |
| PB 10y 5% 分位（深度底）| PB=1.83 | 5753 | −19.7% |
| PB=1.8 历史极端底 | PB=1.80 | 5659 | −21.0% |

**两个独立口径（PE 看 E、PB 看 B）都指向 6000-6100**——这是估值共振底，不是巧合。

### 三档建仓建议

- **第一档（轻仓试探，6500 附近，−9%）**：需配合盈利拐点（E 涨 10% 抵消 P 跌 5%）
- **第二档（半仓，6100 附近，−15%）**：PE/PB 共振触发分位阈值，策略入场信号
- **第三档（重仓，5500 附近，−23%）**：PB <5% 分位历史极端，"别人恐惧时贪婪"

### 关键认知

1. **PE_TTM 分位 ≠ 价格分位**：PE = P/E，价格不动时盈利涨跌会改变 PE。当前 PE 72% 分位但 PB 仅 35% 分位（+37pp 背离），说明**盈利周期在低位**——单看 PE 会悲观，但 PB 显示没那么贵
2. **历史底部 PB < 1.8**（占历史 3.6% 交易日，价格区间 4149-5270，中位 4721）
3. **从减仓区（85%）到入场区（50%）通常需要 1-2 年 + 30% 跌幅**（参考 2015、2021 顶部）

## 7 种信号

| 持仓状态 | 信号 | 含义 |
|---|---|---|
| 空仓 | `entry` | 双条件达标，建议入场 |
| 空仓 | `warn_entry` | 接近入场区 |
| 空仓 | `wait` | 估值偏高或贴水不足 |
| 持仓 | `reduce` | 估值过高，减仓 |
| 持仓 | `warn_reduce` | 接近减仓区 |
| 持仓 | `switch` | 当月临近交割，切下月 |
| 持仓 | `hold` | 继续持有 |

## 展示结果

报告路径：`D:/workspace/csi1000-monitor/reports/csi1000_YYYY-MM-DD.md`

读取后向用户展示：

1. **信号 + 状态**：空仓 🟡 / 持仓 🟢 + 信号名
2. **估值面板**：PE_TTM / PE 静态 / PB 的多区间分位
3. **底部点位回归**：PBS=close/pb（隐含净资产）对历史局部低点对数回归拟合底部抬升趋势线，展示 R²、年化抬升、当前 PBS 距底 %；附回归框架理论点位表（PBS 偏离比分位 × 趋势线 × 当前PB，长期估值底视角，与横向分位互补）
4. **PB 分位情景点位**：不同 PB 分位情景对应点位与跌幅（估值杀跌风险）
5. **IM Carry Score**：下季贴水(40)+PB分位(25)+PBS_score(25)=满分100，区分极佳开仓/可持有观望/观望，按持仓状态给建议
6. **期货合约**：IM 当月/下月/当季/下季的基差和年化贴水率
7. **卖 Call 增厚**：10% OTM call 的权利金、IV、年化增厚率、行权概率
8. **操作建议**：根据信号给出具体建议 + Carry Score 档位建议（区分持仓/空仓）

## 配置

阈值在 `D:/workspace/csi1000-monitor/monitor.py` 顶部（`THRESHOLDS` / `PCT_WINDOWS`）。

**持仓状态用 CLI 子命令持久化到 SQLite**：

```bash
python monitor.py open IM2608 7000 2026-07-18   # 开仓
python monitor.py close                          # 平仓
```

所有命令读 DB 决定空仓/持仓分支；报告逻辑会自动切换（空仓看入场条件，持仓看减仓/切换/盈亏）。

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| scan 返回 0 合约 | 周末/假日 | 工作日重试 |
| option 部分缺失 | 周末无实时数据 | 工作日重试 |
| PE/PB 数据空 | 网络问题 | 重试 `python monitor.py run` |
| `KeyError` 列名 | akshare 改了列名 | 检查 `data_fetcher.py` 的 rename 映射 |
| `stock_index_pb_lg` 报 `pb_w` | 第 4 列是等权不是加权 | 自己算分位（详见 `memory/akshare-pitfalls.md`）|

## 历史背景与设计文档

- 设计：`docs/superpowers/specs/2026-07-12-csi1000-monitor-design.md`
- 实施计划：`docs/superpowers/plans/2026-07-12-csi1000-monitor.md`
- akshare 数据接口陷阱：`memory/akshare-pitfalls.md`
