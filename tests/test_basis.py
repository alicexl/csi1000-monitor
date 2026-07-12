# tests/test_basis.py
from __future__ import annotations
import unittest
from datetime import date

from basis import (
    compute_basis, compute_annualized_discount,
    third_friday, days_to_expire, classify_contract,
)


class TestBasis(unittest.TestCase):
    def test_negative_means_discount(self):
        """期货 < 现货 → 负基差（贴水）"""
        self.assertAlmostEqual(compute_basis(8150, 8198), -48)

    def test_positive_means_premium(self):
        self.assertAlmostEqual(compute_basis(8200, 8198), 2)


class TestAnnualizedDiscount(unittest.TestCase):
    def test_discount_positive(self):
        """现货 8198, 期货 8150, 剩余 7 天 → 年化贴水率"""
        rate = compute_annualized_discount(8150, 8198, 7)
        # discount_rate = (8198-8150)/8198*100 = 0.5855%
        # annualized = 0.5855 * 365/7 = 30.54%
        self.assertAlmostEqual(rate, 30.54, places=1)

    def test_premium_negative(self):
        """升水 → 负年化贴水率"""
        rate = compute_annualized_discount(8200, 8198, 30)
        self.assertLess(rate, 0)

    def test_zero_days_returns_zero(self):
        """交割日当天（0 天）防除零"""
        self.assertEqual(compute_annualized_discount(8150, 8198, 0), 0.0)


class TestThirdFriday(unittest.TestCase):
    def test_july_2026(self):
        """2026-07 第三个周五是 7-17"""
        self.assertEqual(third_friday(2026, 7), date(2026, 7, 17))

    def test_august_2026(self):
        self.assertEqual(third_friday(2026, 8), date(2026, 8, 21))

    def test_december_2026(self):
        self.assertEqual(third_friday(2026, 12), date(2026, 12, 18))

    def test_january_2027(self):
        self.assertEqual(third_friday(2027, 1), date(2027, 1, 15))


class TestDaysToExpire(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(days_to_expire(date(2026, 7, 10), date(2026, 7, 17)), 7)

    def test_same_day(self):
        self.assertEqual(days_to_expire(date(2026, 7, 17), date(2026, 7, 17)), 0)

    def test_past(self):
        self.assertEqual(days_to_expire(date(2026, 7, 20), date(2026, 7, 17)), -3)


class TestClassifyContract(unittest.TestCase):
    def test_current_month(self):
        """2026-07-10: IM2607 是当月"""
        ctype, expire = classify_contract("IM2607", date(2026, 7, 10))
        self.assertEqual(ctype, "当月")
        self.assertEqual(expire, date(2026, 7, 17))

    def test_next_month(self):
        """2026-07-10: IM2608 是下月"""
        ctype, expire = classify_contract("IM2608", date(2026, 7, 10))
        self.assertEqual(ctype, "下月")
        self.assertEqual(expire, date(2026, 8, 21))

    def test_current_quarter(self):
        """2026-07-10: Q3 = 9月, IM2609 是当季"""
        ctype, expire = classify_contract("IM2609", date(2026, 7, 10))
        self.assertEqual(ctype, "当季")
        self.assertEqual(expire, date(2026, 9, 18))

    def test_next_quarter(self):
        """2026-07-10: Q4 = 12月, IM2612 是下季"""
        ctype, expire = classify_contract("IM2612", date(2026, 7, 10))
        self.assertEqual(ctype, "下季")
        self.assertEqual(expire, date(2026, 12, 18))

    def test_invalid_symbol(self):
        ctype, expire = classify_contract("XX2607", date(2026, 7, 10))
        self.assertIsNone(ctype)
        self.assertIsNone(expire)

    def test_non_im_symbol(self):
        """非 IM 开头不识别"""
        ctype, expire = classify_contract("IF2607", date(2026, 7, 10))
        self.assertIsNone(ctype)
        self.assertIsNone(expire)

    def test_invalid_month_crash_guard(self):
        """IM2613 (无效月份) 不应 crash，返回 (None, None)"""
        ctype, expire = classify_contract("IM2613", date(2026, 7, 10))
        self.assertIsNone(ctype)
        self.assertIsNone(expire)

    def test_quarter_month_boundary(self):
        """today 在季月（9月）时，当季应是下一个季月（12月）"""
        # 2026-09-01: 当月=IM2609, 下月=IM2610, 当季=IM2612, 下季=IM2703
        ctype, _ = classify_contract("IM2612", date(2026, 9, 1))
        self.assertEqual(ctype, "当季")
        ctype, _ = classify_contract("IM2703", date(2026, 9, 1))
        self.assertEqual(ctype, "下季")

    def test_march_quarter_month_boundary(self):
        """today 在 3 月（季月）时，当季=6月，下季=9月"""
        ctype, _ = classify_contract("IM2606", date(2026, 3, 15))
        self.assertEqual(ctype, "当季")
        ctype, _ = classify_contract("IM2609", date(2026, 3, 15))
        self.assertEqual(ctype, "下季")

    def test_far_future_contract(self):
        """远月合约（不在当月/下月/当季/下季范围）→ (None, expire)"""
        # 2026-07-10: 可交易 = IM2607/2608/2609/2612. IM2706 不在范围
        ctype, expire = classify_contract("IM2706", date(2026, 7, 10))
        self.assertIsNone(ctype)
        self.assertIsNotNone(expire)  # 仍返回交割日


if __name__ == "__main__":
    unittest.main()
