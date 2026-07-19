# 中证1000 贴水策略监控

监控中证1000指数估值分位 + IM 股指期货贴水率 + MO 股指期权增厚分析，为**纯多头吃贴水**策略提供每日入场/减仓/切换信号。

整个项目回答一个问题：**今天该买、该卖、还是该等？**

---

## 一、它在解决什么问题

中证 1000 是 A 股 1001-1800 名的小盘股指数。**IM 期货**是中金所跟踪该指数的股指期货。

**核心观察**：IM 期货曲线长期处于 **backwardation（近月贵、远月便宜）**——远月合约比近月更深贴水。每展期一次（卖近月、买远月），"卖贵买便宜"赚到一份价差，这就是策略的本质收益——**展期收益（roll_yield）**，不是单合约对现货的绝对贴水。

```
roll_yield = 下月年化贴水 − 当月年化贴水 = 期限结构斜率
           > 0  → 曲线向下倾斜（backwardation），展期能吃价差
           ≤ 0  → 曲线扁平或倒挂（contango），展期反向亏钱
```

**为什么不能只看单合约贴水？** 反例：当月年化贴水 +8%、下月 +5%，两合约都"有贴水"，但 roll_yield = −3%——这种曲线扁平/倒挂的情况下，每展期一次反向亏钱。**关键看曲线斜率，不是单点贴水**。绝对贴水存在 ≠ 展期能赚钱。

但不是无脑多头。两个风险：

1. **估值过高时买入** → 指数本身下跌把展期收益亏光
2. **曲线扁平或倒挂**（roll_yield ≤ 0）→ 展期收益消失甚至反向，策略前提失效

所以策略是：

> **估值低（PE 历史分位 <50%）且曲线健康（roll_yield > 0，backwardation）时买；估值过高（>85%）或曲线异常（roll_yield ≤ 0）时卖。**

本项目每天跑一次，自动告诉你今天该怎么操作。

---

## 二、五个核心概念

| 概念 | 例子 | 含义 |
|---|---|---|
| **PE_TTM** | 30.6 | 滚动市盈率，越低越便宜 |
| **PE 分位** | 71.9% | 当前 PE 在历史样本里的排位（0%=最便宜，100%=最贵）|
| **基差** | -49.2 点 | 期货收盘 - 现货收盘；**负值=贴水**（期货便宜）|
| **年化贴水** | +7.2% | 把基差按剩余天数年化，便于跨合约比较 |
| **展期收益** | +1.2% | roll_yield = 下月年化贴水 − 当月年化贴水（曲线斜率），>0 才能展期吃价差 |

**合约月份规则**（CFFEX 定义）：**当月、下月、当季、下季** 共 4 个合约同时在交易。每第三个周五交割一次。

---

## 三、安装

```bash
git clone https://github.com/alicexl/csi1000-monitor.git
cd csi1000-monitor
pip install akshare pandas
```

Python ≥ 3.9。Windows / macOS / Linux 均可。

**Claude Code 用户**（可选）：直接 clone 到 skills 目录，即可在所有会话里全局触发：

```bash
git clone https://github.com/alicexl/csi1000-monitor.git ~/.claude/skills/csi1000-monitor
```

触发词：`csi1000` / `中证1000` / `贴水监控` / `贴水策略` / `IM 期货` / `MO 期权`。说"跑下贴水监控"/"看看 1000 估值"会自动执行 `python monitor.py run` 并解读报告。

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
└── tests/            ← 141 个测试（单元 + E2E）
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
    │   抽出 signals 需要的 5 个数：pe_ttm_pct_10y / current_month_discount /
    │   current_month_days / next_month_discount / roll_yield（roll_yield 是策略判断主指标）
    │
    ├─ evaluate(state, metrics, thresholds)  → 返回 Signal 列表
    ├─ 写入 signals 表
    └─ generate_report()  → reports/csi1000_YYYY-MM-DD.md
```

**幂等设计**：`cmd_run` 先查 DB 最新估值日期，如果 ≥ 目标交易日（周末回退到周五）则跳过拉取，直接读 DB 离线生成报告。你可以反复跑 `run`，不会重复拉数据。

---

## 七、信号体系（持仓 + 空仓）

策略根据 `position.status` 走**空仓侧**或**持仓侧**分支。**贴水判断全部基于展期收益（roll_yield = 下月年化贴水 − 当月年化贴水 = 期限结构斜率）**，单合约绝对贴水仅作展示参考：

| 持仓 | 信号 | priority | 触发条件 |
|---|---|---|---|
| 空仓 | `entry` | 2 | PE<50% 且 roll_yield>0（曲线向下倾斜，展期能吃到价差）|
| 空仓 | `warn_entry` | 4 | PE 50-60%；或 PE 够低但 roll_yield≤0（曲线扁平/倒挂）|
| 空仓 | `wait` | 5 | 兜底（其他情况）|
| 持仓 | `reduce` | 1 | PE>85% **或** roll_yield≤0（曲线扁平/倒挂，展期失效；双触发都展示）|
| 持仓 | `warn_reduce` | 4 | PE 75-85% |
| 持仓 | `switch` | 3 | 当月剩余 <7 天，该换月 |
| 持仓 | `hold` | 5 | 兜底（继续持有）|

### 为什么看曲线斜率（roll_yield）而不是单合约贴水

详见 §一。绝对贴水存在 ≠ 展期能赚钱，关键看曲线斜率。

### 信号冲突解决

`evaluate()` 有后处理过滤——**兜底信号（wait/hold）只在没有任何具体动作时才展示**：

- 空仓触发 `entry` / `warn_entry` → 过滤掉 `wait`（避免"建议入场 + 继续等待"矛盾）
- 持仓触发 `reduce` / `warn_reduce` / `switch` → 过滤掉 `hold`

### 操作建议

报告里的"操作建议"列出**所有 priority 最小的 suggestion**。比如 `reduce_pe` 和 `reduce_basis` 同 priority=1 双触发时，两条建议都展示，不会丢一条。

---

## 八、几个关键的设计细节

### 1. 估值口径与分位算法

#### 数据源（akshare → 乐咕乐股 legulegu.com）

| 字段 | akshare 接口 | 中文列名 → 项目字段 |
|---|---|---|
| PE_TTM | `stock_index_pe_lg("中证1000")` | "滚动市盈率"（加权）→ `pe_ttm` |
| PE 静态 | 同上 | "静态市盈率"（加权）→ `pe_static` |
| PB | `stock_index_pb_lg("中证1000")` | "市净率"（加权）→ `pb` |

每日一行，2014-10 至今约 2857 行。项目只取**加权值**（乐咕的加权算法偏低于 Wind/中证官网，但分位算法自洽，对策略判断无影响）。

#### 分位算法（ECDF count-based）

`monitor.py` 的 `percentile()` 用**经验分布函数**：

```python
count_le = sum(1 for v in series if v <= current)
pct = count_le / len(series) * 100.0
```

含义：**过去 N 天里有多少比例的交易日估值低于或等于当前值**。

不用 `(current - min) / (max - min)` 这种 min-max 区间法：
- **抗离群点**：2015 牛市顶峰、2024 小盘股危机这种极端值不会污染整个分位
- **不假设分布**：不需要正态假设，直接用经验分布
- **行业口径**：wind / choice / 雪球的"PE 分位"基本都是这个算法

**实测差异**（当前 PE=30.6，2857 个样本）：
- count-based: **61.1%**
- min-max: 13.9%（被 max=114.01 离群点严重扭曲）

#### 三个窗口

| 窗口 | 天数 | 用途 |
|---|---|---|
| **10y** | 3652 天 | 主判断窗口（信号阈值 PE<50% / PE>85% 都基于此）|
| **5y** | 1826 天 | 近 5 年视角（注册制后小盘股活跃期，通常分位高于 10y）|
| **all** | 不限 | 全历史（含 2015 牛市极端值，分位通常低于 10y）|

cutoff 用今日午夜（不是 `now()`），避免下午跑时把今天 00:00 的行过滤掉。

#### 样本数展示

`compute_pct_for_windows` 返回 `{pct, n}`，报告里展示成 `71.9% (n=2427)`。两档：

| 条件 | 展示 |
|---|---|
| 样本 ≥ 100（约 5 个月交易日）| `71.9% (n=2427)` |
| 样本 < 100 | `N/A ⚠ (n=50)` |

不展示"预期样本数/覆盖率"——10 年预期 2440（244 交易日 × 10）是粗估，实际交易日受节假日/闰年影响天然波动，框成"覆盖率"会误导（数据源全量也会显示 99.5%）。绝对阈值 `MIN_SAMPLES=100` 作为唯一兜底。

### 2. 交割日标签滚动（`classify_contract`）

当 `today ≥ 本月第三个周五`时（CFFEX 日数据是盘后发布的，数据存在 = 已收盘）：
- 旧当月合约（如已交割的 IM2607）`ctype=None` → `fetch_daily_contracts` 自然过滤
- 旧下月（IM2608）自动升为新当月
- 季度合约标签独立滚动，不受月度交割影响

**为什么？** 周六重跑周五（交割日）数据时，IM2607 显示 `days=0, basis=+97.4, 年化贴水=+0.0%` 是纯噪音——期货已收敛，basis 没意义。修复后 IM2607 被过滤，IM2608 升为当月展示真实可交易的近月贴水。

### 3. PE-PB 背离

```
pe_pct - pb_pct:
  正值 = PE 相对 PB 更贵 = E 弱 + B 强 → 盈利阶段性低位
  负值 = PB 相对 PE 更贵 = E 强 + B 弱 → 盈利强劲或净资产收缩
```

**反直觉陷阱**：PB 高 ≠ 净资产高。PB = P/B，**PB 高 = 单位净资产卖得贵 = B 相对 P 偏低**。

### 4. 预期收益三因子模型

报告中的「预期收益」panel 把长期收益拆成三个独立分量：

```
预期年化 = ROE + 分红 + 估值变动
```

| 分量 | 计算 | 数据源 |
|---|---|---|
| **ROE** | `PB / PE_TTM` = (P/B) / (P/E) = E/B | 现有 PE/PB 反推，零新数据源 |
| **分红率** | 默认 1.0% | 中证1000 历史约 1-2%，取保守下限 |
| **估值回归** | `(pe_median_10y - pe_now) / pe_now` | 历史 PE 中位数 |

**为什么 ROE 可以这样反推？** PB = P/B，PE = P/E。PB/PE = (P/B) / (P/E) = E/B = ROE。

**展期收益不计入此 panel**（由 status_line 一行单独展示），因为曲线斜率会变化、难以多年预测。

展示两种预期：
- **估值不变年化** = ROE + 分红（假设 PE 保持在当前水平）；附 3 年/5 年复利
- **含估值回归 1 年预期** = 估值不变年化 + 估值回归（假设 PE 1 年内回到 10 年中位数）

**当前实测**（2026-07-17，PE_TTM=30.6, PB=2.28）：
- 估值不变年化 **+8.5%**（3 年 +28% / 5 年 +50%）
- 含估值回归 1 年 **−6.2%**（当前 PE 高于 10 年中位 26.1，有回落风险）
- status_line 显示 `展期收益 +1.2%`（下月 +8.3% − 当月 +7.2%）

---

---

## 九、子命令完整参考

| 命令 | 作用 |
|---|---|
| `run` | 自动判断：DB 数据是最新（≥目标交易日）则跳过拉取，否则先拉数据，再生成报告 |
| `status` | 一行快速查当前信号（**离线**，只读 DB，不预扣网络）|
| `open <contract> <entry_price> [entry_date]` | 开仓：用户手动录入实际仓位信息（合约/入场价/日期）到 DB |
| `close` | 平仓：清空持仓 |

`open`/`close` 是给用户记录**实际开仓行为**的，必须手动执行——工具不会自动下单。开仓后 `run` 会自动在报告里显示持仓盈亏（入场价 vs 当前价）。

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

全部来自 [akshare](https://akshare.akfamily.xyz/)（估值口径详见 §8.1）：

| 数据 | akshare 接口 | 说明 |
|---|---|---|
| PE 历史 | `stock_index_pe_lg("中证1000")` | 乐咕乐股，8 列：静态/TTM/等权/中位数 |
| PB 历史 | `stock_index_pb_lg("中证1000")` | 乐咕乐股，5 列：PB/等权PB/中位数 |
| 主力连续 | `futures_main_sina("IM0")` | 贴水率历史分位（基差用对应日期现货算）|
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
# 141 tests, all pass
```

---

## 十三、运行频率

**每天盘后跑一次**（不管空仓、持仓都看）。空仓等入场信号，持仓看是否触发减仓/换月/曲线异常。

---

**免责声明**：研究学习辅助工具，非投资建议。期货交易有重大亏损风险。
