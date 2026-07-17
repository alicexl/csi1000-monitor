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

- **入场条件**：PE_TTM 10 年分位 <50% **且** 当月年化贴水 >0（有贴水即可）
- **预警入场**：PE_TTM 分位 50–60%，或估值够低但期货升水
- **减仓条件**：PE_TTM 10 年分位 >85% **或** 当月年化贴水 ≤0（升水）
- **预警减仓**：PE_TTM 分位 75–85%
- **合约切换**：当月剩余天数 <7 天时切下月
- **卖 Call 增厚**：持有 IM 多头时卖 10% OTM 当月 call 增厚收益

策略本质：低估时入场，吃贴水收敛 + 估值上涨。有贴水就继续持有；期货转升水立即离场。

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
3. **期货合约**：IM 当月/下月/当季/下季的基差和年化贴水率
4. **卖 Call 增厚**：10% OTM call 的权利金、IV、年化增厚率、行权概率
5. **操作建议**：根据信号给出具体建议

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
