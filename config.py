# config.py
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None


@dataclass
class Position:
    status: str = "empty"  # "empty" | "holding"
    contract: str | None = None
    entry_date: str | None = None
    entry_price: float | None = None


@dataclass
class Thresholds:
    entry_pe_pct: float = 50
    entry_discount: float = 5
    warn_entry_pe_pct: float = 60
    reduce_pe_pct: float = 85
    warn_reduce_pe_pct: float = 75
    switch_days: int = 7


@dataclass
class Config:
    position: Position = field(default_factory=Position)
    thresholds: Thresholds = field(default_factory=Thresholds)
    pct_windows: list[str] = field(default_factory=lambda: ["10y", "5y", "all"])


def _build_position(data: dict[str, Any]) -> Position:
    return Position(
        status=data.get("status", "empty"),
        contract=data.get("contract"),
        entry_date=data.get("entry_date"),
        entry_price=data.get("entry_price"),
    )


def _build_thresholds(data: dict[str, Any]) -> Thresholds:
    return Thresholds(
        entry_pe_pct=data.get("entry_pe_pct", 50),
        entry_discount=data.get("entry_discount", 5),
        warn_entry_pe_pct=data.get("warn_entry_pe_pct", 60),
        reduce_pe_pct=data.get("reduce_pe_pct", 85),
        warn_reduce_pe_pct=data.get("warn_reduce_pe_pct", 75),
        switch_days=data.get("switch_days", 7),
    )


def load_config(path: Path) -> Config:
    """加载 YAML 配置；文件不存在或缺失字段时用默认值。"""
    if not path.exists() or yaml is None:
        return Config()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config(
        position=_build_position(data.get("position", {})),
        thresholds=_build_thresholds(data.get("thresholds", {})),
        pct_windows=data.get("pct_windows", ["10y", "5y", "all"]),
    )


def default_config_template() -> str:
    return """position:
  status: empty        # empty(空仓) | holding(持仓)
  contract: null       # 持仓合约，如 IM2607
  entry_date: null
  entry_price: null

thresholds:
  entry_pe_pct: 50     # 入场：PE_TTM 10y分位 <
  entry_discount: 5    # 入场：年化贴水率 > %
  warn_entry_pe_pct: 60
  reduce_pe_pct: 85    # 减仓：PE_TTM 10y分位 >
  warn_reduce_pe_pct: 75
  switch_days: 7       # 切换提醒：当月剩余天数 <

pct_windows:
  - 10y
  - 5y
  - all
"""
