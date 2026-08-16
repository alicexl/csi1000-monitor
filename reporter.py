# reporter.py
from __future__ import annotations
from typing import Any

from signals import Signal, Position, Thresholds

STATE_LABEL = {
    "empty": "🟡 空仓等待",
    "holding": "🟢 持仓",
}

# 信号 → 一行 status 用的 emoji（统一从信号系统映射，避免重复判断阈值）
SIGNAL_EMOJI = {
    "entry": "🟢",
    "warn_entry": "🔔",
    "wait": "",
    "reduce": "⚠",
    "warn_reduce": "🔔",
    "switch": "🔄",
    "hold": "✅",
}


def format_signals_section(signals: list[Signal], state: str) -> str:
    if not signals:
        return "> 无信号"
    lines = []
    for s in signals:
        star = "★" if s.priority <= 3 else "·"
        lines.append(f"> **{star} {s.type}** — {s.condition}\n> {s.suggestion}")
    return "\n".join(lines)


def _fmt_pct_window(entry) -> str:
    """格式化单个窗口的分位文本。entry = {pct, n}。

    - 正常：  "72.3% (n=2427)"
    - 样本绝对不足（pct=None，n < MIN_SAMPLES）:  "N/A ⚠ (n=50)"
    """
    if entry is None:
        return "N/A"
    pct = entry.get("pct")
    n = entry.get("n", 0)
    if pct is None:
        return f"N/A ⚠ (n={n})"
    return f"{pct:.1f}% (n={n})"


def _valuation_table(metrics: dict) -> str:
    pe_pct = metrics["pe_ttm_pct"]
    pb_pct = metrics["pb_pct"]
    return (
        "| 指标 | 当前 | 近10年分位 | 近5年 | 全历史 |\n"
        "|---|---|---|---|---|\n"
        f"| PE_TTM | {metrics['pe_ttm']:.1f} | **{_fmt_pct_window(pe_pct.get('10y'))}** | "
        f"{_fmt_pct_window(pe_pct.get('5y'))} | {_fmt_pct_window(pe_pct.get('all'))} |\n"
        f"| PB | {metrics['pb']:.2f} | {_fmt_pct_window(pb_pct.get('10y'))} | "
        f"{_fmt_pct_window(pb_pct.get('5y'))} | {_fmt_pct_window(pb_pct.get('all'))} |"
    )


def _entry_check_panel(metrics: dict) -> str:
    """开仓信号检查：PE/PB 10y 分位区间 + 展期收益，三条件达标与否。

    入场 = PE<50% + PB<50% + roll_yield>0 三条件全满足。空仓状态下无论信号都展示，
    让 PE/PB 所处分位区间与展期收益状态、是否构成开仓信号一目了然。
    """
    t = Thresholds()
    pe = metrics["pe_ttm_pct"].get("10y", {}).get("pct")
    pb = metrics.get("pb_pct", {}).get("10y", {}).get("pct")
    roll = metrics.get("roll_yield", 0.0)

    def _zone(v):
        if v is None:
            return "N/A"
        if v < 50:
            return "入场区"
        if v < 60:
            return "接近区"
        if v < 75:
            return "观望区"
        if v < 85:
            return "偏高区"
        return "过高区"

    pe_ok = pe is not None and pe < t.entry_pe_pct
    pb_ok = pb is not None and pb < t.entry_pb_pct
    roll_ok = roll > 0
    pe_str = f"{pe:.1f}%（{_zone(pe)}）" if pe is not None else "N/A"
    pb_str = f"{pb:.1f}%（{_zone(pb)}）" if pb is not None else "N/A"
    met = pe_ok + pb_ok + roll_ok
    verdict = ("✅ 符合开仓信号" if pe_ok and pb_ok and roll_ok
               else f"❌ 未达开仓（{met}/3 满足）")

    return (
        "| 条件 | 当前 | 门槛 | 达标 |\n"
        "|---|---|---|---|\n"
        f"| PE_TTM 10y 分位 | {pe_str} | <{t.entry_pe_pct:.0f}% | {'✅' if pe_ok else '✗'} |\n"
        f"| PB 10y 分位 | {pb_str} | <{t.entry_pb_pct:.0f}% | {'✅' if pb_ok else '✗'} |\n"
        f"| 展期收益 | {roll:+.1f}% | >0 | {'✅' if roll_ok else '✗'} |\n"
        f"\n**{verdict}**"
    )


def _exit_check_panel(metrics: dict) -> str:
    """平仓信号检查：PE 过高 + 展期双段失效，任一触发即平仓。

    平仓 = PE>85% 或（当月→下月<0 且 当月→下季<0）。展期需双段同时 contango
    才算失效（单段 contango 可能只是交割噪音）；与开仓的"三条件全满足"相反。
    持仓状态下无论信号都展示，让平仓触发条件是否满足一目了然。
    """
    t = Thresholds()
    pe = metrics["pe_ttm_pct"].get("10y", {}).get("pct")
    roll = metrics.get("roll_yield", 0.0)      # 当月→下月
    roll_q = metrics.get("roll_yield_q", 0.0)  # 当月→下季
    contracts = {c["contract_type"]: c["close"] for c in metrics.get("contracts", [])}
    cur, nxt, nq = contracts.get("当月"), contracts.get("下月"), contracts.get("下季")

    def _zone(v):
        if v is None:
            return "N/A"
        if v > t.reduce_pe_pct:
            return "平仓区"
        if v > t.warn_reduce_pe_pct:
            return "预警区"
        return "安全区"

    pe_high = pe is not None and pe > t.reduce_pe_pct
    basis_bad = roll < 0 and roll_q < 0  # 双段 contango 才算展期失效
    pe_str = f"{pe:.1f}%（{_zone(pe)}）" if pe is not None else "N/A"
    def seg(v, hi, lo):  # hi→lo 为该展期段两端合约价（当月锚点+远月），让 % 更直观
        label = "contango" if v < 0 else "back"
        if hi is not None and lo is not None:
            return f"{v:+.1f}%（{label}，{hi:.0f}→{lo:.0f}）"
        return f"{v:+.1f}%（{label}）"

    triggers = []
    if pe_high:
        triggers.append("PE 过高")
    if basis_bad:
        triggers.append("展期双段失效")
    verdict = ("⚠️ 触发平仓信号（" + " + ".join(triggers) + "）" if triggers
               else "✅ 未触发平仓（继续持有）")

    return (
        "| 条件 | 当前 | 门槛 | 触发 |\n"
        "|---|---|---|---|\n"
        f"| PE_TTM 10y 分位 | {pe_str} | >{t.reduce_pe_pct:.0f}% | {'✅' if pe_high else '✗'} |\n"
        f"| 展期 当月→下月 | {seg(roll, cur, nxt)} | <0 | {'✅' if roll < 0 else '✗'} |\n"
        f"| 展期 当月→下季 | {seg(roll_q, cur, nq)} | <0 | {'✅' if roll_q < 0 else '✗'} |\n"
        f"\n**{verdict}**\n"
        f"> 展期需双段均<0（双段 contango）才算失效；单段 contango 视为噪音不离场"
    )


def _contracts_table(metrics: dict) -> str:
    rows = []
    for c in metrics.get("contracts", []):
        rows.append(
            f"| {c['symbol']} | {c['contract_type']} | {c['close']:.0f} | "
            f"{c['days_to_expire']} | {c['expire_date']} | "
            f"{c['basis']:+.1f} | {c['annualized_discount']:+.1f}% |"
        )
    header = (
        "| 合约 | 类型 | 收盘 | 剩余天数 | 交割日 | 基差 | 年化贴水 |\n"
        "|---|---|---|---|---|---|---|"
    )
    return header + "\n" + "\n".join(rows) if rows else header + "\n| 无数据 |"


def _bottom_trend_panel(bt: dict) -> str:
    """BPS 净资产趋势回归 panel（附录：净资产底缓慢抬升的方法论证明）。

    BPS = close/pb = 隐含每股净资产。对**全量 BPS 点**做对数回归 ln(BPS)=a+b*t，
    证明"净资产长期复利增长、底部不归零"——这是"中证 1000 有基本面支撑"的论据，
    供策略底层假设参考，非每次决策的操作信号（BPS 偏离趋势线反映盈利周期非估值）。
    另附全量点线性回归（每年加多少点）作为净资产整体增长的直观口径。
    """
    r2 = bt["r2"]
    annual = bt["annual_pct"]
    trend = bt["trend_now"]
    cur = bt["current_bps"]
    n = bt["n"]

    # 线性回归行（全量点，每年加多少点；缺失则不展示）
    linear_str = ""
    lin = bt.get("linear")
    if lin:
        linear_str = (
            f"\n| 线性趋势 +{lin['slope_pt_per_year']:.0f} 点/年 | "
            f"R²={lin['r2']:.2f}（全 {lin['n']} 点）|"
        )

    # BPS 趋势图（base64 内联 PNG，不落盘；缺失则不嵌）
    img_str = ""
    png = bt.get("bps_trend_png")
    if png:
        img_str = (
            "\n\n![BPS 趋势](data:image/png;base64,"
            f"{png} \"{bt.get('_img_date', '')}\")"
        )

    return (
        f"趋势线：ln(BPS) = a + b×t   R²={r2:.2f}   "
        f"年化增长率 {annual:+.1f}%   基于全量 {n} 个 BPS 点\n"
        f"\n"
        f"| 项目 | 值 |\n"
        f"|---|---|\n"
        f"| 当前 BPS | {cur:.0f}（close/pb）|\n"
        f"| 净资产趋势线 BPS_fair | {trend:.0f} |"
        f"{linear_str}"
        f"{img_str}"
    )


def _pb_compression_panel(rows: list) -> str:
    """PB 压缩空间 panel。

    固定资产 B（=当前 BPS=close/pb），看不同 PB 分位情景对应的点位与跌幅。
    回答交易者核心问题："资产已便宜，但估值还有多少杀跌空间"。
    与 BPS 底部回归互补：BPS 看资产便宜不便宜，本面板看估值下行风险。
    """
    lines = [
        "| PB | 对应点位 | 跌幅 | 情景 |",
        "|---|---|---|---|",
    ]
    for r in rows:
        pb = r["pb"]
        price = r["price"]
        drop = r["drop_pct"]
        tag = r.get("tag", "")
        drop_str = f"{drop:+.0f}%" if drop != 0 else "—"
        lines.append(f"| {pb:.2f} | {price:.0f} | {drop_str} | {tag} |")
    return "\n".join(lines)


def _capital_panel(cap: dict) -> str:
    """资金测算 panel：1 手 IM 的名义价值 / 保证金（15%）/ 下跌补缴资金。

    空仓以现价为基准（假设现价开仓），持仓以入场价为基准。开仓即风险度
    100%（权益 = 保证金占用，无下跌缓冲），任何浮亏风险度就 >100% 触发追保；
    补足至 100% 风险度需追加 = 浮亏×85%（新保证金占用随价格同步下降 15%）。
    权益归零线 = 基准价×85%。
    """
    b = cap["base_price"]
    label = cap.get("base_label", "现价")
    lines = [
        f"| 项目 | 金额 |",
        f"|---|---|",
        f"| 合约名义价值（1 手） | {cap['notional'] / 1e4:.1f} 万（{b:.0f} 点 × 200 元/点）|",
        f"| 开仓保证金（15% 估算） | **{cap['margin'] / 1e4:.1f} 万**（开仓即风险度 100%，无下跌缓冲）|",
        f"| 追保触发 | 价格低于{label}即触发（浮亏侵蚀保证金，风险度 >100%）|",
        f"| 权益归零线 | {cap['zero_price']:.0f} 点（不加钱跌 15% 权益归零，再跌即穿仓）|",
    ]
    if cap["scenarios"]:
        lines += [
            "",
            f"下跌资金缺口（自{label}，每跌 1 点浮亏 200 元/手）：",
            "",
            f"| 情景 | 点位 | 自{label}跌幅 | 浮亏（1 手）| 补足至 100% 风险度需追加 |",
            f"|---|---|---|---|---|",
        ]
        for s in cap["scenarios"]:
            lines.append(
                f"| {s['tag']} | {s['price']:.0f} | {s['drop_pct']:.1f}% | "
                f"{s['loss_yuan'] / 1e4:.1f} 万 | {s['topup_yuan'] / 1e4:.1f} 万 |")
    if cap.get("total_1sigma"):
        risk = cap.get("risk_1sigma_pct")
        risk_str = (f"（备足后跌至 -1σ 风险度 ≈{risk:.0f}%，无需追加）"
                    if risk else "")
        lines += [
            "",
            f"**建议总资金（扛 -1σ 不再追加）**：保证金 + 浮亏 ≈ "
            f"**{cap['total_1sigma'] / 1e4:.1f} 万**{risk_str}",
        ]
    lines += [
        "",
        "> 追加金额 = 浮亏×85%：追加后权益 = 新保证金占用，风险度恰好回 100%；"
        "按浮亏全额备足则风险度压到 100% 以下。多手持仓按倍数放大。",
    ]
    return "\n".join(lines)


def _discount_coverage_panel(cov: dict) -> str:
    """贴水覆盖性 panel：持有 1 年的展期贴水 vs PB -1σ/-2σ 跌幅。

    回答"持有吃贴水 1 年，能否扛住一次 PB 杀跌"。展期收益线性累计（保守口径）。
    -1σ 是主判（常态杀跌，贴水应覆盖），-2σ 仅极端参考（黑天鹅级，不要求覆盖）。
    本面板给具体 margin 数值。
    """
    disc = cov["discount_annual"]
    header = "| 持有年限 | 累计贴水 |"
    sep = "|---|---|"
    for s in cov["scenarios"]:
        header += f" {s['label']}（跌{abs(s['drop_pct']):.0f}%） |"
        sep += "---|"
    lines = [header, sep]
    for y in cov["years"]:
        cum = disc * y
        row = f"| {y} 年 | +{cum:.1f}% |"
        for s in cov["scenarios"]:
            margin = cum + s["drop_pct"]  # drop_pct 负值；≥0 即覆盖
            tag = "✅ 已覆盖" if margin >= 0 else "❌ 未覆盖"
            sign = "+" if margin >= 0 else ""
            row += f" {tag} {sign}{margin:.1f}% |"
        lines.append(row)
    return "\n".join(lines)


def _expected_return_panel(er: dict) -> str:
    """三因子预期收益 panel（ROE + 分红 + 估值变动）。

    展期收益（roll_yield = 展期一次收益率，价格是否 back）单独看 status_line 和期货合约表的基差，
    不作为多年复利收益的预测分量（期限结构会变化，难以长期预测）。
    """
    roe = er["roe_pct"]
    div = er["dividend_yield_pct"]
    val = er["valuation_change_pct"]
    base = er["annual_no_valuation_pct"]
    pe_med = er.get("pe_median_10y")

    pe_med_str = f"{pe_med:.1f}" if pe_med else "N/A"
    val_sign = "+" if val >= 0 else ""

    lines = [
        '> **持仓视角**：持有 IM 多头时长期年化回报的来源拆解（ROE 涨幅 + 分红 + 估值回归）。',
        "",
        "| 分量 | 值 | 说明 |",
        "|---|---|---|",
        f"| ROE（PB/PE 反推） | {roe:+.1f}% | 估值不变时的长期涨幅代理 |",
        f"| 分红率 | +{div:.1f}% | 经验默认值（中证1000 约 1-2%）|",
        f"| 估值回归（PE→10y 中位 {pe_med_str}） | {val_sign}{val:.1f}% | 1 年假设回归 |",
        "",
        f"**估值不变年化预期**：`{base:+.1f}%` "
        f"（3 年复利 **{er['c3y_no_valuation_pct']:+.1f}%**，"
        f"5 年复利 **{er['c5y_no_valuation_pct']:+.1f}%**）",
        "",
        f"**含估值回归 1 年预期**：`{er['annual_with_mean_reversion_pct']:+.1f}%` "
        f"（假设 PE 1 年内回到 10 年中位数）",
        "",
        "> 展期收益（贴水收益，来自期货折价 + 期限结构）与上表三因子（持有现货的基本面回报）是不同维度，见状态行/期货合约表；期限结构会变，不计入多年复利预测。",
    ]
    return "\n".join(lines)


def _option_table(opt: dict) -> str:
    """卖 call 增厚分析表。"""
    lines = [
        f"合约: {opt['symbol']}  执行价: {opt['strike']:.0f}  "
        f"≈{opt['sigma_mult']:.1f}σ (ATM IV {opt['atm_iv']:.1f}%，OTM {opt['otm_pct']:.1f}%)  "
        f"剩余: {opt['days_to_expire']}天  到期: {opt['expire_date']}",
        "",
        f"| 权利金(点) | 权利金(元/张) | ATM IV | 年化增厚(名义) | 行权概率 | 盈亏平衡 | 持仓量 |",
        f"|---|---|---|---|---|---|---|",
        f"| {opt['premium_points']:.1f} | {opt['premium_yuan']:.0f} | "
        f"{opt['atm_iv']:.1f}% | **{opt['enhancement_nominal']:.1f}%** | "
        f"{opt['assign_prob']:.1f}% | {opt['breakeven']:.0f} | "
        f"{opt['oi']:.0f} |",
    ]
    return "\n".join(lines)


def generate_report(
    report_date: str,
    position: Position,
    metrics: dict[str, Any],
    signals: list[Signal],
) -> str:
    """生成完整 Markdown 报告。"""
    state = position.status
    label = STATE_LABEL.get(state, state)
    close = metrics.get("close", 0)

    lines = [
        f"# 中证1000 贴水策略监控 {report_date}",
        "",
        f"## 状态：{label}  |  当前 {close:.0f} 点",
        "",
        "## ⚡ 信号",
        format_signals_section(signals, state),
        "",
    ]

    # 空仓状态：开仓信号检查（PE/PB 分位区间 + 展期收益，三条件是否达标）
    if state == "empty":
        lines.append("### 开仓信号检查")
        lines.append(_entry_check_panel(metrics))
        lines.append("")
    # 持仓状态：平仓信号检查（PE 过高 + 展期失效，任一触发即平仓）
    elif state == "holding":
        lines.append("### 平仓信号检查")
        lines.append(_exit_check_panel(metrics))
        lines.append("")
        # 持仓盈亏紧跟信号检查（持仓时最关心当前浮盈）
        if position.entry_price:
            entry = position.entry_price
            pnl_pct = (close - entry) / entry * 100
            lines.append("## 持仓盈亏")
            lines.append(f"入场 {position.entry_date} @ {entry:.0f}，"
                         f"当前 {close:.0f}，浮盈 {pnl_pct:+.1f}%")
            lines.append("")

    lines.append("## 估值面板")
    lines.append(_valuation_table(metrics))
    lines.append("")

    div = metrics.get("pe_pb_divergence", 0)
    if div > 10:
        lines.append(
            f"PE-PB 背离：+{div:.1f}pp — PE 分位显著高于 PB：盈利回暖的预期已经反映在股价里，"
            f"但按净资产看仍不算贵"
        )
        lines.append(
            "> 若盈利无法兑现恢复，PE 高分位可能转为估值压力"
        )
    elif div < -10:
        lines.append(
            f"PE-PB 背离：{div:.1f}pp — PB 分位显著高于 PE：资产定价偏贵而盈利预期偏弱，"
            f"或盈利强劲拉低 PE"
        )
    else:
        lines.append(f"PE-PB 背离：{div:+.1f}pp（基本一致）")
    lines.append("")

    # PB 分位情景点位（主体，可操作：跌幅驱动贴水覆盖判断；底部回归趋势图挪附录）
    bt = metrics.get("bottom_trend")
    if bt:
        pb_rows = bt.get("pb_compression")
        if pb_rows:
            lines.append("## PB 分位情景点位")
            lines.append(_pb_compression_panel(pb_rows))
            lines.append("")

    # 资金测算（复用 PB 分位情景点位，把跌幅换算成 1 手 IM 实际资金量）
    cap = metrics.get("capital")
    if cap:
        lines.append("## 资金测算（1 手 IM）")
        lines.append(_capital_panel(cap))
        lines.append("")

    # 贴水覆盖性：PB 杀跌跌幅 × 下季贴水年限（跌幅来自 pb_compression）
    cov = metrics.get("discount_coverage")
    if cov:
        lines.append("## 贴水覆盖性（1 年展期贴水 vs PB 跌幅）")
        lines.append(_discount_coverage_panel(cov))
        lines.append("")

    # 期货合约（IM 当日市场数据）
    lines.append("## 期货合约（IM 当日）")
    lines.append(_contracts_table(metrics))
    lines.append("")

    # 卖 Call 增厚分析（1σ OTM）— 持仓专属：备兑卖 call 需先持有 IM 多头
    opt = metrics.get("otm_call")
    if opt and state == "holding":
        lines.append("## 卖 Call 增厚分析（1σ OTM）")
        lines.append(_option_table(opt))
        lines.append("")

    # 持仓预期收益（持有 IM 多头时长期年化回报拆解）— 持仓专属
    er = metrics.get("expected_return")
    if er and state == "holding":
        lines.append("## 持仓预期收益（三因子：ROE + 分红 + 估值变动）")
        lines.append(_expected_return_panel(er))
        lines.append("")

    # 附录：BPS 底部回归（净资产底缓慢抬升的方法论证明，非每次决策的操作信号）
    if bt:
        lines.append("## 附录：BPS 底部回归（净资产底抬升证明）")
        lines.append(_bottom_trend_panel(bt))
        lines.append("")

    return "\n".join(lines)


def render_status_line(
    report_date: str, position: Position, metrics: dict,
    signal_type: str, roll_yield: float, roll_yield_q: float,
) -> str:
    """status 子命令一行输出。roll_yield（当月→下月）/roll_yield_q（当月→下季）由调用方通过
    _extract_signal_metrics 算好（展期一次收益率，价格是否 back）。emoji 直接从 signal_type 映射，
    避免在这里重复判断阈值（升水/switch 状态也能正确反映）。
    """
    state = position.status
    state_cn = "空仓" if state == "empty" else "持仓"
    close = metrics.get("close", 0)
    pe = metrics.get("pe_ttm", 0)
    pe_pct = metrics.get("pe_ttm_pct", {}).get("10y", {}).get("pct") or 0
    emoji = SIGNAL_EMOJI.get(signal_type, "")

    return (f"{report_date} | {state_cn} | {close:.0f}点 | "
            f"PE_TTM {pe:.1f} ({pe_pct:.1f}%{emoji}) | "
            f"展期 月{roll_yield:+.1f}%/季{roll_yield_q:+.1f}% | 信号: {signal_type}")
