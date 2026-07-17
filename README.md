# 中证1000 贴水策略监控

监控中证1000指数估值分位 + IM 股指期货贴水率 + MO 股指期权增厚分析，为纯多头吃贴水策略提供入场/减仓/切换信号。

## 策略逻辑

**纯多头吃贴水**：在估值低位买入 IM 股指期货，吃贴水收敛收益。

| 操作 | 条件 |
|---|---|
| 入场 | PE_TTM 10年分位 <50% **且** 当月年化贴水 >0（有贴水即可）|
| 减仓 | PE_TTM 10年分位 >85% **或** 当月年化贴水 ≤0（升水）|
| 合约切换 | 当月剩余天数 <7 天 |
| 卖 Call 增厚 | 持仓时卖 10% OTM 当月 call，额外收权利金 |

策略本质：低估时入场，吃贴水收敛收益 + 估值上涨收益。只要还有贴水（disc >0）就继续持有；期货转升水时策略前提失效，立即离场。

## 安装

```bash
git clone https://github.com/alicexl/csi1000-monitor.git
cd csi1000-monitor
pip install akshare pandas pyyaml
```

可选：安装 Claude Code skill（在 Claude Code 中说"跑下贴水监控"即可自动执行）：

```bash
cp csi1000.skill.md ~/.claude/commands/csi1000.md
```

## 快速开始

```bash
# 首次：拉数据入库（约 30-60 秒）
python monitor.py scan

# 生成 Markdown 报告
python monitor.py report

# 或一键 scan + report
python monitor.py run

# 快速一行查状态
python monitor.py status
```

报告输出到 `reports/csi1000_YYYY-MM-DD.md`。

## 子命令

| 命令 | 作用 |
|---|---|
| `run` | 自动判断：DB 数据是最新（≥目标交易日）则跳过拉取，否则先拉数据，再生成报告 |
| `status` | 一行快速查当前信号 |
| `open <contract> <entry_price> [entry_date]` | 开仓：记录合约/入场价/日期到 DB |
| `close` | 平仓：清空持仓 |

持仓状态持久化在 SQLite 的 `position` 表（单行），所有命令读 DB 决定空仓/持仓分支。开仓后 `run` 会自动在报告里显示持仓盈亏。

`run` 是幂等的：同一天重复跑只会在第一次拉数据，之后直接读 DB 离线生成报告。

## 配置

阈值和分位窗口在 `monitor.py` 顶部：

```python
THRESHOLDS = Thresholds()             # 策略阈值（默认值见 signals.py）
PCT_WINDOWS = ["10y", "5y", "all"]
```

`Thresholds` 包含估值分位 + 合约切换天数。贴水阈值统一为 0（客观定义：>0 有贴水，≤0 升水），不作为可调参数。

**持仓状态不在代码里**——用 `open`/`close` 子命令持久化到 DB。

## Claude Code Skill（可选）

本项目附带 Claude Code skill 文件 `csi1000.skill.md`（安装方式见上方「安装」章节）。安装后在 Claude Code 中说"跑下贴水监控"即可自动执行，触发词：`csi1000` / `贴水监控` / `中证1000`。

## 运行频率建议

| 状态 | 频率 | 监控重点 |
|---|---|---|
| 空仓等待 | 每周五盘后 | 估值 <50% + 贴水够厚 |
| 持仓 | 每月 / 交割日前一周 | 估值 >85% + 合约切换 |

## 项目结构

```
csi1000-monitor/
├── monitor.py          # CLI 入口 + 用户配置常量 + 多区间分位算法
├── db.py               # SQLite（WAL + thread-local 连接）
├── data_fetcher.py     # akshare 拉取 + 基差/贴水/合约分类 + BS 定价/IV
├── signals.py          # 7 种信号判断矩阵 + Thresholds/Position 数据类
├── reporter.py         # Markdown 报告生成
├── tests/              # 83 个单元测试 + E2E
└── reports/            # Markdown 报告输出
```

## 测试

```bash
python -m unittest discover tests -v
# 76 tests, all pass
```

## 数据源

| 数据 | akshare 接口 | 说明 |
|---|---|---|
| PE 历史 | `stock_index_pe_lg("中证1000")` | 8 列：静态/TTM/等权/中位数 |
| PB 历史 | `stock_index_pb_lg("中证1000")` | 5 列：PB/等权PB/中位数 |
| 主力连续 | `futures_main_sina("IM0")` | 贴水率历史分位 |
| 当日合约 | `get_futures_daily(market="CFFEX")` | 筛 IM 开头 |
| 期权 | `option_cffex_zz1000_spot_sina` | MO 当月/下月期权链 |

## 文档

- [设计 spec](docs/superpowers/specs/2026-07-12-csi1000-monitor-design.md)
- [实现计划](docs/superpowers/plans/2026-07-12-csi1000-monitor.md)

**免责声明**：研究学习辅助工具，非投资建议。
