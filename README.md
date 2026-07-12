# 中证1000 贴水策略监控

监控中证1000指数估值分位 + IM 股指期货贴水率，为纯多头吃贴水策略提供入场/减仓/切换信号。

## 快速开始

```bash
# 首次：拉数据入库
python monitor.py scan

# 生成 Markdown 报告
python monitor.py report

# 或一键 scan + report
python monitor.py run

# 快速一行查状态
python monitor.py status
```

## 配置

编辑 `config.yaml` 维护持仓状态和阈值：

```yaml
position:
  status: empty        # empty | holding
  contract: null       # 持仓合约（holding 时填，如 IM2607）
  entry_date: null
  entry_price: null
```

## 运行频率建议

| 状态 | 频率 |
|---|---|
| 空仓等待 | 每周五盘后 |
| 持仓 | 每月一次 / 交割日前一周 |
| 触发减仓 | 回到空仓等待 |

## 文档

- [设计 spec](docs/superpowers/specs/2026-07-12-csi1000-monitor-design.md)
- [实现计划](docs/superpowers/plans/2026-07-12-csi1000-monitor.md)

## 测试

```bash
python -m unittest discover tests -v
```

## 数据源

- 估值 PE/PB：akshare `stock_index_pe_lg` / `stock_index_pb_lg`（乐咕乐股）
- 期货主力连续：`futures_main_sina("IM0")`
- 期货当日合约：`get_futures_daily(market="CFFEX")`

**免责声明**：研究学习辅助工具，非投资建议。
