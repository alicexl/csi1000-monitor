# tests/test_config.py
from __future__ import annotations
import unittest
from pathlib import Path
import tempfile
import os

from config import Position, Thresholds, Config, load_config, default_config_template


class TestPositionDefaults(unittest.TestCase):
    def test_default_position_is_empty(self):
        p = Position()
        self.assertEqual(p.status, "empty")
        self.assertIsNone(p.contract)
        self.assertIsNone(p.entry_date)
        self.assertIsNone(p.entry_price)


class TestThresholdsDefaults(unittest.TestCase):
    def test_default_thresholds(self):
        t = Thresholds()
        self.assertEqual(t.entry_pe_pct, 50)
        self.assertEqual(t.entry_discount, 5)
        self.assertEqual(t.warn_entry_pe_pct, 60)
        self.assertEqual(t.reduce_pe_pct, 85)
        self.assertEqual(t.warn_reduce_pe_pct, 75)
        self.assertEqual(t.switch_days, 7)


class TestLoadConfig(unittest.TestCase):
    def test_load_full_config(self):
        yaml_content = """
position:
  status: holding
  contract: IM2607
  entry_date: "2026-06-01"
  entry_price: 7500.0
thresholds:
  entry_pe_pct: 45
  entry_discount: 6
  warn_entry_pe_pct: 55
  reduce_pe_pct: 80
  warn_reduce_pe_pct: 70
  switch_days: 5
pct_windows:
  - 10y
  - 5y
  - all
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            f.write(yaml_content)
            path = f.name
        try:
            cfg = load_config(Path(path))
            self.assertEqual(cfg.position.status, "holding")
            self.assertEqual(cfg.position.contract, "IM2607")
            self.assertEqual(cfg.thresholds.entry_pe_pct, 45)
            self.assertEqual(cfg.thresholds.switch_days, 5)
            self.assertEqual(cfg.pct_windows, ["10y", "5y", "all"])
        finally:
            os.unlink(path)

    def test_load_partial_config_uses_defaults(self):
        """缺字段时用 dataclass 默认值"""
        yaml_content = """
position:
  status: empty
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            f.write(yaml_content)
            path = f.name
        try:
            cfg = load_config(Path(path))
            self.assertEqual(cfg.position.status, "empty")
            self.assertIsNone(cfg.position.contract)
            self.assertEqual(cfg.thresholds.entry_pe_pct, 50)  # 默认
            self.assertEqual(cfg.pct_windows, ["10y", "5y", "all"])  # 默认
        finally:
            os.unlink(path)

    def test_load_nonexistent_returns_defaults(self):
        """文件不存在时返回全默认配置"""
        cfg = load_config(Path("/nonexistent/path/config.yaml"))
        self.assertEqual(cfg.position.status, "empty")
        self.assertEqual(cfg.thresholds.entry_pe_pct, 50)


class TestDefaultTemplate(unittest.TestCase):
    def test_template_is_valid_yaml(self):
        import yaml
        content = default_config_template()
        data = yaml.safe_load(content)
        self.assertIn("position", data)
        self.assertIn("thresholds", data)
        self.assertEqual(data["position"]["status"], "empty")


if __name__ == "__main__":
    unittest.main()
