# reporter.py
from __future__ import annotations
from typing import Any

from signals import Signal, Position, carry_suggestion, Thresholds

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
    """平仓信号检查：PE 过高 + 展期失效，任一触发即平仓。

    平仓 = PE>85% 或 roll_yield≤0 两条件任一满足（与开仓的"三条件全满足"相反）。
    持仓状态下无论信号都展示，让平仓触发条件是否满足一目了然。
    """
    t = Thresholds()
    pe = metrics["pe_ttm_pct"].get("10y", {}).get("pct")
    roll = metrics.get("roll_yield", 0.0)

    def _zone(v):
        if v is None:
            return "N/A"
        if v > t.reduce_pe_pct:
            return "平仓区"
        if v > t.warn_reduce_pe_pct:
            return "预警区"
        return "安全区"

    pe_high = pe is not None and pe > t.reduce_pe_pct
    roll_bad = roll <= 0
    pe_str = f"{pe:.1f}%（{_zone(pe)}）" if pe is not None else "N/A"
    roll_str = (f"{roll:+.1f}%（价格 contango/平水，展期失效）" if roll_bad
                else f"{roll:+.1f}%（价格 back，展期健康）")
    triggers = []
    if pe_high:
        triggers.append("PE 过高")
    if roll_bad:
        triggers.append("展期失效")
    verdict = ("⚠️ 触发平仓信号（" + " + ".join(triggers) + "）" if triggers
               else "✅ 未触发平仓（2/2 安全，继续持有）")

    return (
        "| 条件 | 当前 | 门槛 | 触发 |\n"
        "|---|---|---|---|\n"
        f"| PE_TTM 10y 分位 | {pe_str} | >{t.reduce_pe_pct:.0f}% | {'✅' if pe_high else '✗'} |\n"
        f"| 展期收益 | {roll_str} | ≤0 | {'✅' if roll_bad else '✗'} |\n"
        f"\n**{verdict}**"
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


def _discount_coverage_panel(cov: dict) -> str:
    """贴水覆盖性 panel：持有 1 年的展期贴水 vs PB -1σ/-2σ 跌幅。

    回答"持有吃贴水 1 年，能否扛住一次 PB 杀跌"。展期收益线性累计（保守口径）。
    -1σ 是主判（常态杀跌，贴水应覆盖），-2σ 仅极端参考（黑天鹅级，不要求覆盖）。
    与 Carry Score 覆盖因子同源，本面板给具体 margin 数值。
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


def _carry_score_panel(cs: dict, state: str) -> str:
    """IM Carry Score panel（滚贴水持有评分，满分 100）。

    三因子：下季贴水年化(40) + PB 10y 分位(25) + 1年贴水覆盖-1σ(25)。
    滚贴水≠价值投资：最怕高估值+低贴水+波动上升。Carry Score 量化"收益(贴水)+
    安全(PB估值/贴水覆盖)"，区分极佳开仓/可持有观望/观望，并按持仓状态给具体建议。
    """
    total = cs["total"]
    band = cs["band"]
    band_label = {"excellent": "极佳", "holdable": "可持有", "wait": "观望"}[band]
    suggestion = carry_suggestion(band, state)

    # 各因子档位解读
    def _roll_tag(v, pts):
        if pts == 40:
            return f"{v:.1f}% ≥10%（极佳收益）"
        if pts == 30:
            return f"{v:.1f}% 5~10%（良好）"
        return f"{v:.1f}% <5%（收益不足）"
    def _pb_tag(v, pts):
        if pts == 25:
            return f"{v:.1f}% <30%（资产便宜）"
        if pts == 15:
            return f"{v:.1f}% 30~60%（中性）"
        return f"{v:.1f}% ≥60%（偏贵）"
    def _coverage_tag(v, pts):
        pct = v * 100
        if pts == 25:
            return f"{pct:.0f}% ≥100%（贴水覆盖 -1σ 下跌）"
        if pts == 15:
            return f"{pct:.0f}% 50~100%（部分覆盖）"
        return f"{pct:.0f}% <50%（覆盖不足）"

    lines = [
        f"**总分 {total}/100 — {band_label}**   {suggestion}",
        "",
        "| 因子 | 得分 | 当前值 | 档位 |",
        "|---|---|---|---|",
        f"| 下季贴水年化 | {cs['discount_pts']}/40 | "
        f"{cs['discount_value']:.1f}% | {_roll_tag(cs['discount_value'], cs['discount_pts'])} |",
        f"| PB 10y 分位 | {cs['pb_pts']}/25 | "
        f"{cs['pb_pct']:.1f}% | {_pb_tag(cs['pb_pct'], cs['pb_pts'])} |",
        f"| 1年贴水覆盖-1σ | {cs['coverage_pts']}/25 | "
        f"{cs['coverage_ratio']*100:.0f}% | {_coverage_tag(cs['coverage_ratio'], cs['coverage_pts'])} |",
    ]
    return "\n".join(lines)


def _expected_return_panel(er: dict) -> str:
    """三因子预期收益 panel（PDF 杨康平框架：ROE + 分红 + 估值变动）。

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
        f"OTM: {opt['otm_pct']:.1f}%  剩余: {opt['days_to_expire']}天  "
        f"到期: {opt['expire_date']}",
        "",
        f"| 权利金(点) | 权利金(元/张) | IV | 年化增厚(名义) | 行权概率 | 盈亏平衡 | 持仓量 |",
        f"|---|---|---|---|---|---|---|",
        f"| {opt['premium_points']:.1f} | {opt['premium_yuan']:.0f} | "
        f"{opt['iv']:.1f}% | **{opt['enhancement_nominal']:.1f}%** | "
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

    # 卖 Call 增厚分析（10% OTM）— 持仓专属：备兑卖 call 需先持有 IM 多头
    opt = metrics.get("otm_call")
    if opt and state == "holding":
        lines.append("## 卖 Call 增厚分析（10% OTM）")
        lines.append(_option_table(opt))
        lines.append("")

    # IM Carry Score（滚贴水持有评分）— 持仓专属：持有视角的滚贴水评分
    cs = metrics.get("carry_score")
    if cs and state == "holding":
        lines.append("## IM Carry Score（滚贴水持有评分）")
        lines.append(_carry_score_panel(cs, state))
        lines.append("")

    # 持仓盈亏（holding 状态）
    if state == "holding" and position.entry_price:
        entry = position.entry_price
        pnl_pct = (close - entry) / entry * 100
        lines.append("## 持仓盈亏")
        lines.append(f"入场 {position.entry_date} @ {entry:.0f}，"
                     f"当前 {close:.0f}，浮盈 {pnl_pct:+.1f}%")
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
    signal_type: str, roll_yield: float,
) -> str:
    """status 子命令一行输出。roll_yield 由调用方通过 _extract_signal_metrics 算好
    （= 展期一次收益率 = (当月价 − 下月价)/当月价，价格是否 back）。emoji 直接从 signal_type 映射，
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
            f"展期收益 {roll_yield:+.1f}% | 信号: {signal_type}")
