# reporter.py
from __future__ import annotations
from typing import Any

from signals import Signal, Position

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
    pe_s_pct = metrics["pe_static_pct"]
    pb_pct = metrics["pb_pct"]
    return (
        "| 指标 | 当前 | 近10年分位 | 近5年 | 全历史 |\n"
        "|---|---|---|---|---|\n"
        f"| PE_TTM | {metrics['pe_ttm']:.1f} | **{_fmt_pct_window(pe_pct.get('10y'))}** | "
        f"{_fmt_pct_window(pe_pct.get('5y'))} | {_fmt_pct_window(pe_pct.get('all'))} |\n"
        f"| PE 静态 | {metrics['pe_static']:.1f} | {_fmt_pct_window(pe_s_pct.get('10y'))} | "
        f"{_fmt_pct_window(pe_s_pct.get('5y'))} | {_fmt_pct_window(pe_s_pct.get('all'))} |\n"
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
        lines.append(f"PE-PB 背离：+{div:.1f}pp（盈利阶段性低位，净资产相对坚挺）")
    elif div < -10:
        lines.append(f"PE-PB 背离：{div:.1f}pp（盈利强劲或净资产收缩）")
    else:
        lines.append(f"PE-PB 背离：{div:+.1f}pp（基本一致）")
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
