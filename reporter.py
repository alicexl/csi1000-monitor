# reporter.py
from __future__ import annotations
from typing import Any

from config import Config
from signals import Signal

STATE_LABEL = {
    "empty": "🟡 空仓等待",
    "holding": "🟢 持仓",
}


def format_signals_section(signals: list[Signal], state: str) -> str:
    if not signals:
        return "> 无信号"
    lines = []
    for s in signals:
        star = "★" if s.priority <= 3 else "·"
        lines.append(f"> **{star} {s.type}** — {s.condition}\n> {s.suggestion}")
    return "\n".join(lines)


def _valuation_table(metrics: dict) -> str:
    pe_pct = metrics["pe_ttm_pct"]
    pe_s_pct = metrics["pe_static_pct"]
    pb_pct = metrics["pb_pct"]
    return (
        "| 指标 | 当前 | 近10年分位 | 近5年 | 全历史 |\n"
        "|---|---|---|---|---|\n"
        f"| PE_TTM | {metrics['pe_ttm']:.1f} | **{pe_pct.get('10y', 0):.1f}%** | "
        f"{pe_pct.get('5y', 0):.1f}% | {pe_pct.get('all', 0):.1f}% |\n"
        f"| PE 静态 | {metrics['pe_static']:.1f} | {pe_s_pct.get('10y', 0):.1f}% | "
        f"{pe_s_pct.get('5y', 0):.1f}% | {pe_s_pct.get('all', 0):.1f}% |\n"
        f"| PB | {metrics['pb']:.2f} | {pb_pct.get('10y', 0):.1f}% | "
        f"{pb_pct.get('5y', 0):.1f}% | {pb_pct.get('all', 0):.1f}% |"
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
    if opt.get("implied_discount") is not None:
        lines.append("")
        lines.append(f"期权隐含远期: {opt['implied_forward']:.0f}  "
                     f"隐含贴水: {opt['implied_discount']:.2f}%")
    return "\n".join(lines)


def generate_report(
    report_date: str,
    config: Config,
    metrics: dict[str, Any],
    signals: list[Signal],
) -> str:
    """生成完整 Markdown 报告。"""
    state = config.position.status
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
        lines.append(f"PE-PB 背离：+{div:.1f}pp（盈利阶段性低位）")
    elif div < -10:
        lines.append(f"PE-PB 背离：{div:.1f}pp（净资产膨胀）")
    else:
        lines.append(f"PE-PB 背离：{div:+.1f}pp（基本一致）")
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

    # 操作建议
    if signals:
        top = min(signals, key=lambda s: s.priority)
        lines.append("## 操作建议")
        lines.append(top.suggestion)
        lines.append("")

    # 持仓盈亏（holding 状态）
    if state == "holding" and config.position.entry_price:
        entry = config.position.entry_price
        pnl_pct = (close - entry) / entry * 100
        lines.append(f"## 持仓盈亏")
        lines.append(f"入场 {config.position.entry_date} @ {entry:.0f}，"
                     f"当前 {close:.0f}，浮盈 {pnl_pct:+.1f}%")
        lines.append("")

    return "\n".join(lines)


def render_status_line(
    report_date: str, config: Config, metrics: dict, signal_type: str
) -> str:
    """status 子命令一行输出。"""
    state = config.position.status
    state_cn = "空仓" if state == "empty" else "持仓"
    close = metrics.get("close", 0)
    pe = metrics.get("pe_ttm", 0)
    pe_pct = metrics.get("pe_ttm_pct", {}).get("10y", 0)
    warn = "⚠" if pe_pct > 85 else ("🟢" if pe_pct < 50 else "")

    contracts = metrics.get("contracts", [])
    cur_month = next(
        (c for c in contracts if c["contract_type"] == "当月"), None)
    disc = cur_month["annualized_discount"] if cur_month else 0

    return (f"{report_date} | {state_cn} | {close:.0f}点 | "
            f"PE_TTM {pe:.1f} ({pe_pct:.1f}%{warn}) | "
            f"当月贴水 {disc:+.1f}% | 信号: {signal_type}")
