# reporter.py
from __future__ import annotations
from typing import Any

from signals import Signal, Position, carry_suggestion

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
    """PBS 底部回归 panel。

    PBS = close/pb ≈ 隐含净资产。对历史局部低点对数回归 ln(PBS)=a+b*t 拟合
    "净资产底部抬升趋势线"。当前 PBS 距趋势线的指标是长期估值底视角。

    pbs_score = 趋势线/当前：>1 资产折价(便宜)，=1 正常，<1 资产溢价(贵)。
    """
    r2 = bt["r2"]
    annual = bt["annual_pct"]
    trend = bt["trend_now"]
    cur = bt["current_pbs"]
    score = bt["pbs_score"]
    n = bt["n"]
    recent = bt.get("recent_low")

    # 距底解读（基于 pbs_score：>1 便宜，<1 贵）
    if score > 1.05:
        score_tag = "资产折价，长期视角偏便宜"
    elif score > 0.95:
        score_tag = "贴近趋势线，长期视角接近底部"
    elif score > 0.85:
        score_tag = "资产微溢价，长期视角中性"
    else:
        score_tag = "资产溢价，长期视角偏贵"

    recent_str = ""
    if recent:
        recent_str = f"\n| 最近低点 | {recent[0]}  PBS={recent[1]:.0f} |"

    # 回归框架理论点位表：PBS 偏离比分位 × 趋势线 × PB
    theory_str = ""
    tp = bt.get("theory_points")
    if tp:
        theory_str = (
            "\n\n**回归框架理论点位**（PBS 偏离比分位 × 趋势线 × 当前PB）：\n"
            "\n"
            "| PBS偏离比 | 理论点位 | 距趋势线 | 情景 |\n"
            "|---|---|---|---|\n"
        )
        for r in tp:
            drop = r["drop_pct"]
            drop_str = f"{drop:+.0f}%" if abs(drop) > 0.1 else "—"
            theory_str += (
                f"| {r['ratio']:.2f} | {r['price']:.0f} | "
                f"{drop_str} | {r['tag']} |\n"
            )
        theory_str = theory_str.rstrip()  # 去尾换行

    return (
        f"趋势线：ln(PBS) = a + b×t   R²={r2:.2f}   "
        f"底部抬升率 {annual:+.1f}%   基于 {n} 个历史低点（±{bt['window']} 日窗口）\n"
        f"\n"
        f"| 项目 | 值 |\n"
        f"|---|---|\n"
        f"| 当前 PBS | {cur:.0f}（close/pb）|\n"
        f"| 底部趋势线 PBS_fair | {trend:.0f} |\n"
        f"| PBS_score（fair/now）| {score:.2f} — {score_tag} |{recent_str}"
        f"{theory_str}"
    )


def _pb_compression_panel(rows: list) -> str:
    """PB 压缩空间 panel。

    固定资产 B（=当前 PBS=close/pb），看不同 PB 分位情景对应的点位与跌幅。
    回答交易者核心问题："资产已便宜，但估值还有多少杀跌空间"。
    与 PBS 底部回归互补：PBS 看资产便宜不便宜，本面板看估值下行风险。
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


def _carry_score_panel(cs: dict, state: str) -> str:
    """IM Carry Score panel（滚贴水持有评分，满分 100）。

    三因子：下季贴水年化(40) + PB 10y 分位(25) + PBS_score(25)。
    滚贴水≠价值投资：最怕高估值+低贴水+波动上升。Carry Score 量化"收益(贴水)+
    安全(PB/PBS)"，区分极佳开仓/可持有观望/观望，并按持仓状态给具体建议。
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
    def _pbs_tag(v, pts):
        if pts == 25:
            return f"{v:.2f} >1.05（低于趋势，便宜）"
        if pts == 15:
            return f"{v:.2f} 附近趋势"
        return f"{v:.2f} <0.85（明显高于趋势）"

    lines = [
        f"**总分 {total}/100 — {band_label}**   {suggestion}",
        "",
        "| 因子 | 得分 | 当前值 | 档位 |",
        "|---|---|---|---|",
        f"| 下季贴水年化 | {cs['discount_pts']}/40 | "
        f"{cs['discount_value']:.1f}% | {_roll_tag(cs['discount_value'], cs['discount_pts'])} |",
        f"| PB 10y 分位 | {cs['pb_pts']}/25 | "
        f"{cs['pb_pct']:.1f}% | {_pb_tag(cs['pb_pct'], cs['pb_pts'])} |",
        f"| PBS_score | {cs['pbs_pts']}/25 | "
        f"{cs['pbs_score']:.2f} | {_pbs_tag(cs['pbs_score'], cs['pbs_pts'])} |",
    ]
    return "\n".join(lines)


def _expected_return_panel(er: dict) -> str:
    """三因子预期收益 panel（PDF 杨康平框架：ROE + 分红 + 估值变动）。

    展期收益（roll_yield = 期限结构斜率）单独看 status_line 和期货合约表的基差，
    不作为多年复利收益的预测分量（曲线斜率会变化，难以长期预测）。
    """
    roe = er["roe_pct"]
    div = er["dividend_yield_pct"]
    val = er["valuation_change_pct"]
    base = er["annual_no_valuation_pct"]
    pe_med = er.get("pe_median_10y")

    pe_med_str = f"{pe_med:.1f}" if pe_med else "N/A"
    val_sign = "+" if val >= 0 else ""

    lines = [
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
        "> 展期收益（roll_yield）见状态行和期货合约表的基差；曲线斜率会变化，不计入多年复利预测。",
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
        "## 估值面板",
        _valuation_table(metrics),
        "",
    ]

    div = metrics.get("pe_pb_divergence", 0)
    if div > 10:
        lines.append(
            f"PE-PB 背离：+{div:.1f}pp — PE 分位显著高于 PB：市场对盈利恢复已有定价，"
            f"但资产估值仍处中低区间"
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

    bt = metrics.get("bottom_trend")
    if bt:
        lines.append("## 底部点位回归（PBS = close/pb，2014-至今对数趋势）")
        lines.append(_bottom_trend_panel(bt))
        lines.append("")

        # PB 压缩空间与底部回归同源（都基于 close/pb），紧跟其后展示
        pb_rows = bt.get("pb_compression")
        if pb_rows:
            lines.append("## PB 分位情景点位")
            lines.append(_pb_compression_panel(pb_rows))
            lines.append("")

    # IM Carry Score（滚贴水持有评分）— 需要持仓状态给建议
    cs = metrics.get("carry_score")
    if cs:
        lines.append("## IM Carry Score（滚贴水持有评分）")
        lines.append(_carry_score_panel(cs, state))
        lines.append("")

    er = metrics.get("expected_return")
    if er:
        lines.append("## 预期收益（三因子：ROE + 分红 + 估值变动）")
        lines.append(_expected_return_panel(er))
        lines.append("")

    lines.append("## 期货合约（IM 当日）")
    lines.append(_contracts_table(metrics))
    lines.append("")

    main_pct = metrics.get("main_continuous_discount_pct")
    if main_pct is not None:
        lines.append(f"主力连续贴水分位：{main_pct:.1f}%（近2年）")
        lines.append("")

    opt = metrics.get("otm_call")
    if opt:
        lines.append("## 卖 Call 增厚分析（10% OTM）")
        lines.append(_option_table(opt))
        lines.append("")

    # 操作建议（最小 priority 的所有信号都展示，避免同 priority 多信号丢一个）
    if signals:
        top_priority = min(s.priority for s in signals)
        top_sigs = [s for s in signals if s.priority == top_priority]
        lines.append("## 操作建议")
        for s in top_sigs:
            lines.append(f"- {s.suggestion}")
        # Carry Score 档位建议（区分已持仓/空仓，补信号系统没有的持仓视角）
        cs = metrics.get("carry_score")
        if cs:
            lines.append(f"- [Carry {cs['total']}/100] {carry_suggestion(cs['band'], state)}")
        lines.append("")

    # 持仓盈亏（holding 状态）
    if state == "holding" and position.entry_price:
        entry = position.entry_price
        pnl_pct = (close - entry) / entry * 100
        lines.append(f"## 持仓盈亏")
        lines.append(f"入场 {position.entry_date} @ {entry:.0f}，"
                     f"当前 {close:.0f}，浮盈 {pnl_pct:+.1f}%")
        lines.append("")

    return "\n".join(lines)


def render_status_line(
    report_date: str, position: Position, metrics: dict,
    signal_type: str, roll_yield: float,
) -> str:
    """status 子命令一行输出。roll_yield 由调用方通过 _extract_signal_metrics 算好
    （= 下月年化贴水 - 当月年化贴水 = 期限结构斜率）。emoji 直接从 signal_type 映射，
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
