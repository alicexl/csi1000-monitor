# 中证1000 贴水策略监控

监控中证1000指数估值分位 + IM 股指期货贴水率 + MO 股指期权增厚分析，为**纯多头吃贴水**策略提供每日入场/减仓/切换信号。

整个项目回答一个问题：**今天该买、该卖、还是该等？**

---

## 一、它在解决什么问题

中证 1000 是 A 股 1001-1800 名的小盘股指数。**IM 期货**是中金所跟踪该指数的股指期货。

**核心观察**：IM 期货长期**低于**现货指数（这叫"贴水"）。原因很多（小盘股分红率高、对冲需求、做空需求等），但结果很实在——

> 买入 IM 期货多头，**不停展期（卖近月、买远月）**，期货曲线长期向下倾斜（远月比近月更深贴水），**每展期一次吃到一份远月深度贴水**。

但不是无脑多头。两个风险：

1. **估值过高时买入** → 指数本身下跌把贴水收益亏光
2. **期货曲线变升水**（远月也升水）→ 展期收益消失，策略前提失效

所以策略是：

> **估值低（PE 历史分位 <50%）且远月仍有贴水（下月期货 < 现货，展期能吃）时买；估值过高（>85%）或远月也升水时卖。**

**关键：看的是远月（下月）年化贴水，不是当月基差**。临近交割时当月基差必然收敛到 0，那是噪音不是失效信号。下月贴水才代表"下一次展期能吃到多少"。

本项目每天跑一次，自动告诉你今天该怎么操作。

---

## 二、五个核心概念

| 概念 | 例子 | 含义 |
|---|---|---|
| **PE_TTM** | 30.6 | 滚动市盈率，越低越便宜 |
| **PE 分位** | 71.9% | 当前 PE 在历史样本里的排位（0%=最便宜，100%=最贵）|
| **基差** | -49.2 点 | 期货收盘 - 现货收盘；**负值=贴水**（期货便宜）|
| **年化贴水** | +7.2% | 把基差按剩余天数年化，便于跨合约比较 |
| **展期收益** | +12.8% | 下月年化贴水——每展期一次吃到的远月深度贴水 |

**合约月份规则**（CFFEX 定义）：**当月、下月、当季、下季** 共 4 个合约同时在交易。每第三个周五交割一次。

**基差 vs 展期收益**（容易混淆）：
- **基差** = 任意月份期货与现货的差。每个合约都有自己的基差（IM2607/2608/2609/2612 都各自 vs 现货）
- **展期收益** = 近月与远月的价差斜率。下月比当月更深贴水 → 展期能吃到差价
- 吃贴水策略看的是**下月年化贴水**（决定下一次展期能吃多少），**不是当月基差**（临交割时会归零，是噪音）

---

## 三、安装

```bash
git clone https://github.com/alicexl/csi1000-monitor.git
cd csi1000-monitor
pip install akshare pandas
```

Python ≥ 3.9。Windows / macOS / Linux 均可。

---

## 四、快速开始

```bash
# 默认推荐：自动拉数据 + 生成报告（幂等，同一天重复跑只拉一次）
python monitor.py run

# 一行快速查状态（离线，读 DB）
python monitor.py status

# 开仓后记录持仓（写到 SQLite position 表）
python monitor.py open IM2608 7000 2026-07-18

# 平仓
python monitor.py close
```

报告输出到 `reports/csi1000_YYYY-MM-DD.md`。

---

## 五、代码架构

5 个模块，**`monitor.py` 是唯一集成层**——其他 4 个模块互不 import：

```
csi1000-monitor/
├── monitor.py        ← CLI + 流水编排 + 多区间分位算法 + 用户配置常量
├── data_fetcher.py   ← akshare 拉取 + 基差/贴水/合约分类 + BS 定价/IV
├── signals.py        ← 纯算法：指标 → 7 种信号（可单测）
├── reporter.py       ← Markdown 报告 + 一行状态渲染
├── db.py             ← SQLite WAL，4 张表 CRUD
└── tests/            ← 144 个测试（单元 + E2E）
```

**为什么这么拆？** 单一调用方的模块拆分避免网状依赖：所有跨模块协调都在 `monitor.py` 里发生。`signals.py` 纯算法，输入指标输出信号，便于单测；`db.py` 只管存取；`data_fetcher.py` 只管拉数和定价；`reporter.py` 只管展示。

### 4 张 SQLite 表

| 表 | 主键 | 用途 |
|---|---|---|
| `daily_valuation` | `date` | 每日 PE/PB/收盘（2014 至今 ~2900 行）|
| `daily_contracts` | `(date, symbol)` | 每日 IM 合约行情 + 基差/贴水 |
| `signals` | `UNIQUE(date, signal_type, condition)` | 每日触发的信号（重复 INSERT OR IGNORE）|
| `position` | `id=1`（单行 CHECK 约束）| 持仓状态 |

**UPSERT 语义**：估值/合约用 `ON CONFLICT DO UPDATE`（数据源修正历史时新值覆盖）；信号用 `INSERT OR IGNORE`（同一信号一天只记一次）；持仓用 `INSERT OR REPLACE`。

老 DB 的 signals 表没 UNIQUE 约束会堆重复数据——`init_db` 检测到老 schema 时**自动 migration**（清理重复 + 建唯一索引），用户无需手动处理。

---

## 六、`run` 命令的完整数据流

整个系统的主线：

```
[1] _scan() — 拉数据入库（幂等：DB 已是最新则跳过）
    ├─ fetch_valuation()         → akshare 拉全历史 PE/PB → UPSERT
    ├─ fetch_main_continuous()   → akshare 拉主力连续 IM0 → UPSERT
    └─ fetch_daily_contracts()   → akshare 拉当日合约 → 分类 + 算基差 → UPSERT

[2] _generate_report() — 读 DB → 评估信号 → 写 signals 表 + 渲染报告
    ├─ _build_metrics(conn)
    │   ├─ compute_pct_for_windows()  — 多区间 PE/PB 分位（10y/5y/all）
    │   ├─ _window_median()           — PE_TTM 10 年中位数（估值回归用）
    │   ├─ _compute_expected_return() — 三因子预期收益（贴水+ROE+分红+估值变动）
    │   ├─ fetch_otm_call()           — 实时拉期权算 BS 增厚
    │   └─ → {pe_ttm, pe_ttm_pct, expected_return, contracts, otm_call, ...}
    │
    ├─ _extract_signal_metrics()
    │   抽出 signals 需要的 4 个数：pe_ttm_pct_10y / current_month_discount /
    │   current_month_days / next_month_discount（下月贴水是策略判断主指标）
    │
    ├─ evaluate(state, metrics, thresholds)  → 返回 Signal 列表
    ├─ 写入 signals 表
    └─ generate_report()  → reports/csi1000_YYYY-MM-DD.md
```

**幂等设计**：`cmd_run` 先查 DB 最新估值日期，如果 ≥ 目标交易日（周末回退到周五）则跳过拉取，直接读 DB 离线生成报告。你可以反复跑 `run`，不会重复拉数据。

---

## 七、信号体系（持仓 + 空仓）

策略根据 `position.status` 走**空仓侧**或**持仓侧**分支。**贴水判断全部基于下月合约**（展期收益来源），当月基差仅用于近月异常提醒：

| 持仓 | 信号 | priority | 触发条件 |
|---|---|---|---|
| 空仓 | `entry` | 2 | PE<50% 且 当月贴水>0 且 下月贴水>0（双贴水，clean entry）|
| 空仓 | `warn_entry` | 4 | PE 50-60%；或 PE 够低但下月升水；或 PE 够低 + 下月贴水但当月升水（可等修复或直接买下月）|
| 空仓 | `wait` | 5 | 兜底（其他情况）|
| 持仓 | `reduce` | 1 | PE>85% **或** 下月贴水≤0（远月升水，展期失效；双触发都展示）|
| 持仓 | `warn_reduce` | 4 | PE 75-85%；**或** 当月升水但下月仍贴水（近月异常提醒）|
| 持仓 | `switch` | 3 | 当月剩余 <7 天，该换月 |
| 持仓 | `hold` | 5 | 兜底（继续持有）|

### 信号冲突解决

`evaluate()` 有后处理过滤——**兜底信号（wait/hold）只在没有任何具体动作时才展示**：

- 空仓触发 `entry` / `warn_entry` → 过滤掉 `wait`（避免"建议入场 + 继续等待"矛盾）
- 持仓触发 `reduce` / `warn_reduce` / `switch` → 过滤掉 `hold`

### 操作建议

报告里的"操作建议"列出**所有 priority 最小的 suggestion**。比如 `reduce_pe` 和 `reduce_basis` 同 priority=1 双触发时，两条建议都展示，不会丢一条。

---

## 八、几个关键的设计细节

### 1. PE 分位算法（count-based，非 min-max）

`monitor.py` 的 `percentile()` 用**经验分布函数**（ECDF）：

```python
count_le = sum(1 for v in series if v <= current)
pct = count_le / len(series) * 100.0
```

含义：**当前值比历史样本中多少比例的点便宜（或相等）**。

不用 `(current - min) / (max - min)` 这种 min-max 区间法，因为：
- **抗离群点**：单个极端值（2015 牛市顶峰、2024 小盘股危机）不会污染整个分位
- **统计意义清晰**："过去 10 年只有 X% 的日子比现在便宜"是直接可解释的
- **不假设分布**：不需要正态分布假设，直接用经验分布
- **行业口径**：wind / choice / 雪球的"PE 分位"基本都是这个算法

**实测差异**（当前 PE=30.6，2014-至今 2857 个样本）：
- count-based: **61.3%**
- min-max: 13.9%（被 max=114.01 离群点严重扭曲）

### 2. 分位的"样本置信度"

`compute_pct_for_windows` 不只返回分位值，还返回**样本数和预期样本数**：

```python
{"10y": {"pct": 71.9, "n": 2428, "expected": 2440}}
```

报告里展示成 `71.9% (n=2428/2440)`。三档置信度：
- 正常：样本数 ≥ 预期 80%
- 低覆盖 ⚠：样本不足 80% 但 ≥ 100
- N/A ⚠：样本绝对不足（<100，约 5 个月交易日）

**为什么？** 防止"用 1 年数据算 10 年分位"这种 silent bug——数据少时分位显得很集中，但其实是假的精度。预期样本按 A 股每年 244 个交易日估算（10y=2440，5y=1220）。

### 3. 交割日标签滚动（`classify_contract`）

当 `today ≥ 本月第三个周五`时（CFFEX 日数据是盘后发布的，数据存在 = 已收盘）：
- 旧当月合约（如已交割的 IM2607）`ctype=None` → `fetch_daily_contracts` 自然过滤
- 旧下月（IM2608）自动升为新当月
- 季度合约标签独立滚动，不受月度交割影响

**为什么？** 周六重跑周五（交割日）数据时，IM2607 显示 `days=0, basis=+97.4, 年化贴水=+0.0%` 是纯噪音——期货已收敛，basis 没意义。修复后 IM2607 被过滤，IM2608 升为当月展示真实可交易的近月贴水。

### 4. 贴水判断基于下月合约（不是当月）

吃贴水策略本质是**展期收益**：每展期一次（卖近月、买远月）吃到一份远月深度贴水。所以：

- **判断指标用下月年化贴水**（决定下一次展期能吃多少）
- **不用当月基差**（临交割时必然收敛到 0，是噪音）
- 当月基差仅在"近月升水但远月仍贴水"时触发 warn_reduce（提醒关注期限结构变化，但不减仓）

历史上 `_extract_signal_metrics` 曾用 days<7 fallback 补丁处理"当月临交割"场景。重构后直接独立返回 `next_month_discount` 字段，不再需要 fallback——下月贴水天然就是策略判断指标。

### 5. PE-PB 背离

```
pe_pct - pb_pct:
  正值 = PE 相对 PB 更贵 = E 弱 + B 强 → 盈利阶段性低位
  负值 = PB 相对 PE 更贵 = E 强 + B 弱 → 盈利强劲或净资产收缩
```

**反直觉陷阱**：PB 高 ≠ 净资产高。PB = P/B，**PB 高 = 单位净资产卖得贵 = B 相对 P 偏低**。

### 6. 预期收益三因子模型（PDF 框架）

报告中的「预期收益」panel 基于**杨康平《股指期货吃贴水策略》PDF** 的核心框架：

```
预期年化 = 展期收益(下月贴水) + ROE + 分红 + 估值变动
```

| 分量 | 计算 | 数据源 |
|---|---|---|
| **ROE** | `PB / PE_TTM` = (P/B) / (P/E) = E/B | 现有 PE/PB 反推，零新数据源 |
| **分红率** | 默认 1.0% | 中证1000 历史约 1-2%，取保守下限 |
| **展期收益** | 下月年化贴水 | 实时期货数据 |
| **估值回归** | `(pe_median_10y - pe_now) / pe_now` | 历史 PE 中位数 |

**为什么 ROE 可以这样反推？** PB = P/B，PE = P/E。PB/PE = (P/B) / (P/E) = E/B = ROE。

展示两种预期：
- **估值不变年化** = ROE + 分红 + 贴水（假设 PE 保持在当前水平）；附 3 年/5 年复利
- **含估值回归 1 年预期** = 估值不变年化 + 估值回归（假设 PE 1 年内回到 10 年中位数）

**当前实测**（2026-07-17，PE_TTM=30.6, PB=2.28, 下月贴水=+8.3%）：
- 估值不变年化 **+16.8%**（3 年 +59% / 5 年 +117%）
- 含估值回归 1 年 **+2.1%**（当前 PE 高于 10 年中位 26.1，有回落风险）

---

---

## 九、子命令完整参考

| 命令 | 作用 |
|---|---|
| `run` | 自动判断：DB 数据是最新（≥目标交易日）则跳过拉取，否则先拉数据，再生成报告 |
| `status` | 一行快速查当前信号（离线）|
| `open <contract> <entry_price> [entry_date]` | 开仓：记录合约/入场价/日期到 DB |
| `close` | 平仓：清空持仓 |

开仓后 `run` 会自动在报告里显示持仓盈亏（入场价 vs 当前价）。

---

## 十、配置

阈值和分位窗口在 `monitor.py` 顶部：

```python
THRESHOLDS = Thresholds()             # 默认阈值（定义见 signals.py）
PCT_WINDOWS = ["10y", "5y", "all"]
```

`Thresholds` 字段（`signals.py`）：

```python
entry_pe_pct: float = 50        # 入场 PE 分位上限
warn_entry_pe_pct: float = 60   # 预警入场上限
reduce_pe_pct: float = 85       # 减仓 PE 分位上限
warn_reduce_pe_pct: float = 75  # 预警减仓上限
switch_days: int = 7            # 合约切换阈值（剩余天数）
```

**贴水阈值统一为 0**（客观定义：>0 有贴水，≤0 升水），不作为可调参数。

**持仓状态不在代码里**——用 `open`/`close` 子命令持久化到 DB。

---

## 十一、数据源

全部来自 [akshare](https://akshare.akfamily.xyz/)：

| 数据 | akshare 接口 | 说明 |
|---|---|---|
| PE 历史 | `stock_index_pe_lg("中证1000")` | 8 列：静态/TTM/等权/中位数 |
| PB 历史 | `stock_index_pb_lg("中证1000")` | 5 列：PB/等权PB/中位数 |
| 主力连续 | `futures_main_sina("IM0")` | 贴水率历史分位 |
| 当日合约 | `get_futures_daily(market="CFFEX")` | 筛 IM 开头 |
| 期权 | `option_cffex_zz1000_spot_sina` | MO 当月/下月期权链 |

**已知坑**：
- `stock_index_pb_lg` 第 4 列是「等权市净率」**不是**「加权市净率」 → 必须自己算分位
- `prob_above_strike` P(S_T>K) = N(d2) 不是 1-N(d2)
- 周末 / 节假日 CFFEX 当日数据返回空，工作日重试

---

## 十二、测试

```bash
python -m pytest tests/ -q
# 144 tests, all pass
```

测试分布：
- `test_db.py` — DB CRUD + migration（自动清理老 schema 重复行）
- `test_data_fetcher.py` — akshare mock + 数据结构
- `test_basis.py` — 基差/年化贴水/classify_contract（含交割日滚动）
- `test_signals.py` — 7 种信号各路径 + 冲突过滤
- `test_valuation.py` — 分位算法 + 多窗口置信度
- `test_reporter.py` — Markdown 渲染 + 操作建议 + 预期收益 panel
- `test_monitor.py` — CLI 集成 + 预期收益算法（ROE 反推/复利/估值回归）
- `test_e2e.py` — 端到端对比报告

**重要教训**：单元测试可能漏集成层 silent bug。改 `query_valuation_history` 的 `days` 参数语义（行数→天数）时单测全绿，但实际把 2014-2016 数据裁掉了，"all" 窗口实际只剩 10 年。**E2E 对比报告数值差异比单元测试更能发现这类 bug**。

---

## 十三、运行频率建议

| 状态 | 频率 | 监控重点 |
|---|---|---|
| 空仓等待 | 每周五盘后 | 估值 <50% + 贴水够厚 |
| 持仓 | 每月 / 交割日前一周 | 估值 >85% + 合约切换 |

---

## 十四、Claude Code Skill（可选）

本项目根目录的 `SKILL.md` 是 Claude Code skill 定义。在 `~/.claude/skills/csi1000-monitor/` 建 directory junction 指向项目根：

```bash
# Windows (需要管理员权限的 cmd)
mklink /J "%USERPROFILE%\.claude\skills\csi1000-monitor" "D:\workspace\csi1000-monitor"
```

安装后在 Claude Code 中说"跑下贴水监控"即可自动执行，触发词：`csi1000` / `贴水监控` / `中证1000` / `IM 期货` / `MO 期权`。

---

**免责声明**：研究学习辅助工具，非投资建议。期货交易有重大亏损风险。
