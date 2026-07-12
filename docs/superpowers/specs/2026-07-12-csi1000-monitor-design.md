# 中证1000 贴水策略监控系统设计

**日期**: 2026-07-12
**项目目录**: `D:\workspace\csi1000-monitor\`

## 一、项目目的

为纯多头吃贴水策略提供估值 + 贴水监控，等待中证1000指数进入合理估值区域后入场买入 IM 股指期货，吃贴水收敛收益。

**策略逻辑**：
- 收益 = 指数涨跌 + 贴水收敛收益
- 入场条件：估值低（PE_TTM 10y 分位 <50%）+ 贴水够厚（年化贴水率 >5%）
- 减仓条件：估值过高（PE_TTM 10y 分位 >85%）
- 合约管理：持有当月合约，剩余天数 <7 天时考虑切下月

**收益认知**：纯多头没对冲，指数涨跌仍影响总收益。入场点低降低下跌风险，贴水做缓冲；策略上可忽略短期波动，但非"完全无关"。

## 二、项目结构

```
csi1000-monitor/
├── monitor.py          # CLI 入口（argparse 子命令）
├── db.py               # SQLite 连接管理（复用 a-share-financials 模式）
├── config.py           # 阈值/状态 dataclass + YAML 配置
├── data_fetcher.py     # akshare 数据拉取（现货指数 + 期货合约）
├── valuation.py        # 估值分位计算（复用 csi1000_analysis_v2 逻辑）
├── basis.py            # 基差/年化贴水率计算
├── signals.py          # 信号判断（入场/减仓/切换）
├── reporter.py         # Markdown 报告生成（状态驱动）
├── config.yaml         # 阈值 + 持仓状态配置
├── csi1000_monitor.db  # SQLite 数据库（运行后生成）
├── reports/            # Markdown 报告输出目录
│   └── csi1000_YYYY-MM-DD.md
└── tests/              # 单元测试
```

## 三、子命令

| 子命令 | 作用 | 频率 |
|---|---|---|
| `scan` | 拉数据入库（现货估值 + 期货合约 + 算分位/贴水） | 每次跑先 scan |
| `report` | 读 DB 最新数据 + 信号判断 → 生成 Markdown 报告 | scan 后跑 |
| `status` | 快速一行输出当前状态 | 随时快查 |

合并用法：`python monitor.py scan && python monitor.py report` 或加 `--scan-and-report` 一键。

## 四、DB Schema

设计原则：原始数据只追加不更新；分位等衍生指标在 report 时现算（避免每次 scan 重算历史分位）。

### 表 1：`daily_valuation`（每日估值原始数据）

```sql
CREATE TABLE daily_valuation (
    date          TEXT PRIMARY KEY,    -- YYYY-MM-DD
    close         REAL NOT NULL,       -- 指数收盘
    pe_static     REAL,
    pe_ttm        REAL,
    pe_ttm_eq     REAL,                -- 等权TTM
    pe_static_med REAL,                -- 静态PE中位数(成分股)
    pe_ttm_med    REAL,                -- TTM PE中位数
    pb            REAL,
    pb_med        REAL,
    pb_w          REAL,                -- 加权PB
    fetched_at    TEXT NOT NULL
);
```

EPS_TTM = close/pe_ttm、BPS = close/pb 不入库（report 时现算，避免冗余）。分位不入库（report 时从全历史序列现算）。

### 表 2：`daily_contracts`（每日期货合约数据）

```sql
CREATE TABLE daily_contracts (
    date                TEXT NOT NULL,
    symbol              TEXT NOT NULL,    -- 如 IM2407 或 IM0(主力连续)
    name                TEXT,
    contract_type       TEXT,             -- 当月/下月/当季/下季/主力
    close               REAL,
    settle              REAL,
    volume              REAL,
    open_interest       REAL,
    expire_date         TEXT,
    days_to_expire      INTEGER,
    basis               REAL,             -- 期货close - 现货close
    annualized_discount REAL,             -- 年化贴水率%
    fetched_at          TEXT NOT NULL,
    PRIMARY KEY (date, symbol)
);
```

### 表 3：`signals`（信号触发历史）

```sql
CREATE TABLE signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    signal_type   TEXT NOT NULL,         -- entry/warn_entry/reduce/warn_reduce/switch/wait/hold
    condition     TEXT,
    current_value TEXT,
    threshold     TEXT,
    suggestion    TEXT,
    created_at    TEXT NOT NULL
);
```

### DB 连接管理

复用 a-share-financials 的 `db.py` 模式：
- thread-local 连接
- WAL + busy_timeout=5000
- `_init_lock` 串行化 PRAGMA/DDL（WAL pragma 不遵守 busy_timeout，多 worker 并发会死锁）
- DB 不可用永久降级（路径无效时 `_disabled_flag=True`）

## 五、持仓状态与阈值配置（config.yaml，不入 DB）

```yaml
# 持仓状态由用户手动维护
position:
  status: empty        # empty(空仓) | holding(持仓)
  contract: null       # 持仓合约，如 IM2407
  entry_date: null
  entry_price: null

# 阈值配置
thresholds:
  entry_pe_pct: 50     # 入场：PE_TTM 10y分位 <
  entry_discount: 5    # 入场：年化贴水率 > %
  warn_entry_pe_pct: 60
  reduce_pe_pct: 85    # 减仓：PE_TTM 10y分位 >
  warn_reduce_pe_pct: 75
  switch_days: 7       # 切换提醒：当月剩余天数 <

# 历史分位区间
pct_windows:
  - 10y
  - 5y
  - all
```

持仓状态是主观决策，放 config.yaml 人类可读可编辑；DB 只存客观市场数据。阈值也放配置，调整策略不用改代码。

## 六、数据源与拉取策略

### 现货估值数据

| 数据 | akshare 接口 | 说明 |
|---|---|---|
| PE 历史 | `stock_index_pe_lg(symbol="中证1000")` | 8 列：静态/TTM/等权/中位数 + 指数收盘 |
| PB 历史 | `stock_index_pb_lg(symbol="中证1000")` | 5 列：PB + 加权 + 中位数 + 指数收盘 |

**拉取策略**：每次 scan 全量拉取（akshare 一次返回全历史 ~2850 天，约 2 秒），与 DB 现有数据比对，只追加 DB 中没有的最新日期（通常每次 scan 只新增 1 行）。PE/PB 的"指数"列就是收盘点位，不需要额外调 `stock_zh_index_daily`。

### 期货合约数据

| 数据 | akshare 接口 | 用途 |
|---|---|---|
| 主力连续历史 | `futures_main_sina(symbol="IM0")` | 看主力贴水率历史分位 |
| 当日在市合约 | `get_futures_daily(start, end, market="CFFEX")` | 筛 IM 开头 → 当月/下月/当季/下季 |

**拉取策略**：
- 首次 scan：拉主力连续全历史（~960 天）入库 `daily_contracts`（contract_type="主力"）
- 每次 scan：拉当天 `get_futures_daily` → 筛 IM 开头 → 识别合约类型 → 入库

### 期货合约识别规则

中金所股指期货合约：`IM` + 4位数字（YYMM），交割日 = 合约月份第三个周五。

可交易合约 4 个：当月、下月、当季（3/6/9/12）、下季。例 2026-07：
- 当月 IM2607（7月交割）
- 下月 IM2608（8月交割）
- 当季 IM2609（9月，Q3）
- 下季 IM2612（12月，Q4）

`data_fetcher.py` 内置 `classify_contract(symbol, today)` → 返回合约类型 + 交割日 + 剩余天数。交割日计算：合约月份的第三个周五（遇法定假日顺延，简化处理：纯周五计算，假日场景标记 `data_quality=warn`）。

### 基差与年化贴水率计算

```python
# basis.py 核心公式
basis = futures_close - spot_close          # 负值=贴水，正值=升水
discount_rate = (spot_close - futures_close) / spot_close * 100  # 正值=贴水幅度%
annualized_discount = discount_rate * 365 / days_to_expire       # 年化贴水率%
```

年化贴水率 >0：买入期货到期收敛的年化收益（正值越大越划算）。年化贴水率 <0：升水（买入期货反而亏）。

### 错误处理

| 场景 | 处理 |
|---|---|
| akshare 网络超时 | 30 秒超时，重试 3 次间隔 2/4/8 秒指数退避 |
| 接口返回空 | 跳过当日 + stderr 警告，不阻断 scan |
| PE/PB 异常值（负值或 >200） | 记录但标记 `data_quality=warn`，不入库 |
| 期货价格 0 或基差 >10% 现货价 | 同上，标记异常 |
| `get_futures_daily` 缺合约 | 记录缺失合约，用主力连续兜底 |

## 七、信号逻辑

### 信号矩阵（状态驱动）

**边界值规则**：所有比较用严格不等号（< / >），刚好等于阈值不触发。例：PE 分位 = 50.0% 不触发 entry（需严格 <50%），= 85.0% 不触发 reduce（需严格 >85%）。

| 状态 | 信号类型 | 触发条件 | 建议操作 |
|---|---|---|---|
| **空仓** | `entry` ★ | PE_TTM 10y分位 < entry_pe_pct(50) **且** 当月年化贴水 > entry_discount(5) | 买入当月合约入场 |
| 空仓 | `warn_entry` | (entry_pe_pct ≤ PE分位 < warn_entry_pe_pct) **或** (PE分位 < entry_pe_pct **且** 当月贴水 ≤ entry_discount) | 密切跟踪，准备入场 |
| 空仓 | `wait` | 其他（PE分位 ≥ warn_entry_pe_pct） | 继续等待 |
| **持仓** | `reduce` ★ | PE_TTM 10y分位 > reduce_pe_pct(85) | 减仓/平仓止盈 |
| 持仓 | `warn_reduce` | warn_reduce_pe_pct(75) < PE分位 ≤ reduce_pe_pct | 准备减仓 |
| 持仓 | `switch` ★ | 当月合约剩余天数 < switch_days(7) | 平当月、开下月 |
| 持仓 | `hold` | 其他（PE分位 ≤ warn_reduce_pe_pct 且 剩余天数 ≥ switch_days） | 继续持有吃贴水 |

★ = 强信号（需要操作），其他 = 软信号（提醒/观望）

### 信号优先级

同一次 scan 可能触发多个信号，按优先级排序展示：
1. `reduce`（最高，落袋为安）
2. `entry`（入场时机）
3. `switch`（合约切换）
4. `warn_*`（预警）
5. `wait`/`hold`（状态确认）

### 信号判断函数

```python
# signals.py 核心
@dataclass
class Signal:
    type: str           # entry/warn_entry/reduce/warn_reduce/switch/wait/hold
    priority: int       # 1(最高) ~ 5
    condition: str      # 触发条件描述
    current: dict       # 当前值快照
    threshold: dict     # 阈值
    suggestion: str     # 建议操作

def evaluate(state: str, metrics: dict, thresholds: dict) -> list[Signal]:
    """根据持仓状态 + 当前指标 + 阈值 → 返回触发的信号列表"""
```

### 辅助信息（非触发条件，报告展示用）

- **贴水率历史分位**：主力连续(IM0)的年化贴水率在近 2 年的分位。高分位 = 当前贴水比历史厚；低分位 = 贴水偏薄。作为入场决策辅助参考（不强制纳入触发条件）
- **PE-PB 背离度**：PE分位 − PB分位，正值=盈利低位，负值=净资产膨胀。作为估值质量参考

## 八、报告格式（状态驱动）

报告输出到 `reports/csi1000_YYYY-MM-DD.md`，空仓和持仓状态展示不同重点。

### 报告骨架

```markdown
# 中证1000 贴水策略监控 YYYY-MM-DD

## 状态：🟡 空仓等待  |  当前 N 点

## ⚡ 信号
> **wait** — PE_TTM XX% 分位，未达入场条件（需 <50%）
> 距入场差：PE 还需回落 Npp，或等指数跌至 ~N 点

## 估值面板
| 指标 | 当前 | 近10年分位 | 近5年 | 全历史 |
|---|---|---|---|---|
| PE_TTM | ... | ... | ... | ... |
| PE 静态 | ... | ... | ... | ... |
| PB | ... | ... | ... | ... |

PE-PB 背离：+/- Npp（盈利阶段性低位/净资产膨胀）

## 期货合约（IM 当日）
| 合约 | 类型 | 收盘 | 剩余天数 | 交割日 | 基差 | 年化贴水 |
|---|---|---|---|---|---|---|
| IMXXXX | 当月 | ... | ... | ... | ... | ... |
| ... | 下月/当季/下季 | ... |

主力连续贴水分位：N%（近2年）

## 估值箱体（基于当前 EPS_TTM=N）
[分位点 → 对应点位表，标注当前位置 ★]

## 操作建议
[根据信号给出具体建议]

## 历史信号记录
近 30 天信号：...
```

### 状态驱动的差异

| 报告章节 | 空仓 | 持仓 |
|---|---|---|
| 信号区 | 入场条件是否满足 + 距离差 | 减仓/切换是否触发 |
| 估值面板 | 完整展示（入场决策依据）| 精简（只看是否 >85%）|
| 期货合约 | 当月+下月（入场选哪个）| 当月为主（持仓）+ 下月（切换目标）|
| 操作建议 | 等待/入场 | 持有/减仓/切换 |
| 持仓盈亏 | 不显示 | 显示（入场价 vs 现价 + 贴水已吃收益）|

### status 子命令（一行快速查）

```
$ python monitor.py status
2026-07-12 | 空仓 | 8198点 | PE_TTM 34.6 (81.8%⚠) | 当月贴水 13.5% | 信号: wait
```

## 九、错误处理、测试与依赖

### 错误处理分层

| 层 | 错误类型 | 处理 |
|---|---|---|
| 数据拉取 | 网络超时/接口失败 | 重试 3 次（2/4/8 秒指数退避），仍失败 → 跳过当日 + stderr 警告 |
| 数据校验 | PE/PB 负值或 >200、期货价 0、基差 >10% | 记录 `data_quality_warns`，不入库但报告标注 |
| DB 操作 | SQLite 锁/磁盘满 | WAL + busy_timeout=5000 防锁；磁盘满 → 退出 + 明确错误 |
| 配置 | config.yaml 缺字段 | config.py dataclass 默认值兜底 + 警告 |
| CLI | 参数错误 | argparse 自动报错 |

**不做的事**（YAGNI）：
- 不发邮件/webhook 告警（纯手动）
- 不做数据源 fallback（akshare 是唯一源，失败等下次）
- 不做事务回滚（单日数据追加，失败重跑即可）

### 测试策略

分层测试（参考 c-drive-monitor 模式，标准库 unittest）：

```
tests/
├── test_valuation.py    # 分位计算：已知序列验证 + 边界值
├── test_basis.py        # 基差/年化贴水率公式 + 剩余天数计算
├── test_signals.py      # 7种状态信号矩阵全覆盖
├── test_config.py       # 配置加载/默认值/阈值解析
├── test_db.py           # schema/CRUD/去重/并发(WAL)
├── test_fetcher.py      # mock akshare 返回 → 合约分类/数据校验
└── test_e2e.py          # scan + report 全流程（用 fixture DB）
```

关键测试用例：
- `valuation.py`：已知 PE 序列验证分位算法 + 边界值（刚好 = 阈值时）
- `signals.py`：7 个信号分支 × 边界值全覆盖
- `basis.py`：交割日是第三个周五的计算（跨月场景）
- `db.py`：重复 scan 同一天不产生重复行（PK 去重）

覆盖率目标：signals/basis/valuation 三个核心模块 >90%，其他 >70%。

### 日志策略（保持简单）

不引入 `logging` 模块，用 `print` + stderr：
- `scan`：打印进度（"拉取 PE/PB... OK 2852 行"、"拉取期货... OK 4 合约"、"入库... 追加 1 行"）
- `report`：只打印报告路径
- 错误/警告 → stderr

### 依赖

| 包 | 用途 | 版本 |
|---|---|---|
| akshare | 数据拉取 | 1.18.64（已装）|
| pandas | 数据处理 | 已装 |
| pyyaml | 配置解析 | 需确认 |
| sqlite3 | DB | 标准库 |
| matplotlib | 报告图表（可选）| 已装 |

标准库优先，第三方仅 3 个。

### 项目初始化检查清单

scan 首次运行时检查：
1. `config.yaml` 存在？不存在 → 从模板生成 + 提示用户编辑
2. `csi1000_monitor.db` 存在？不存在 → 创建 schema + 首次全量拉取
3. `reports/` 目录存在？不存在 → 创建

## 十、复用清单

从现有项目复用的模式：
- **a-share-financials `db.py`**：thread-local + WAL + `_init_lock` 连接管理
- **c-drive-monitor `categories.py`**：dataclass + 数据驱动配置模式（本项目中 config.py 用类似模式）
- **csi1000_analysis_v2.py**：`pct()` 分位计算函数 + 箱体映射逻辑

## 十一、运行频率建议（状态依赖）

| 状态 | 频率 | 监控重点 |
|---|---|---|
| 空仓等待期 | 每周五盘后 | 估值是否 <50% + 贴水率是否够大 |
| 持仓期 | 每月一次 / 交割日前一周 | 估值是否 >85% + 交割日切换提醒 |
| 触发减仓 | 回到空仓等待期 | 重新等入场信号 |

项目不强制定时（用户选了手动跑），`config.yaml` 的 `position.status` 决定报告详细度。
