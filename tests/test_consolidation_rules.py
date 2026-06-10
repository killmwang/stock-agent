import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web-app" / "backend"))

from app.services.analysis_service import AnalysisService
from tradingagents.agents.consolidation.consolidation_analyst import (
    _enforce_consolidation_consistency,
    _extract_decision_info,
)


class ConsolidationRuleTests(unittest.TestCase):
    def test_quarter_eps_pe_target_is_annualized_and_percentage_is_fixed(self):
        report = "\n".join([
            "1. 执行摘要 (Executive Summary)",
            "投资评级：【持有/观望】",
            "目标价位推导：",
            "基本面报告指出，当前估值水平需结合PE/PB判断。",
            "若利润零增长，PE中枢将下移至10-12倍，取目标倍数区间中值11倍。",
            "根据基本面报告，最新每股收益（EPS）为10.42元。",
            "计算公式：目标价 = 10.42元 × 11 = 114.62元。",
            "结论：目标价114.62元（较现价1266.74元下跌9.5%）。",
        ])

        fixed, signal = _enforce_consolidation_consistency(report, current_price=1266.74)

        self.assertEqual(signal, "SELL")
        self.assertIn("投资评级：【卖出】", fixed)
        self.assertIn("EPS口径校验", fixed)
        self.assertIn("458.48元", fixed)
        self.assertIn("下跌63.8%", fixed)

    def test_pb_target_formula_is_not_misclassified_as_quarter_eps(self):
        report = "\n".join([
            "1. 执行摘要 (Executive Summary)",
            "投资评级：【持有】",
            "目标价位推导：",
            "采用市净率（PB）估值法。",
            "计算公式：目标价 = 每股净资产 × 目标倍数 = 216.32元 × 0.8 = 173.06元。",
            "结论：目标价173.06元（较现价1266.74元下跌86.3%）。",
        ])

        fixed, signal = _enforce_consolidation_consistency(report, current_price=1266.74)

        self.assertEqual(signal, "SELL")
        self.assertIn("投资评级：【卖出】", fixed)
        self.assertIn("173.06元", fixed)
        self.assertNotIn("692.22元", fixed)
        self.assertNotIn("EPS口径校验", fixed)

    def test_zero_position_forces_sell_rating(self):
        report = "\n".join([
            "1. 执行摘要 (Executive Summary)",
            "投资评级：【持有】",
            "建议仓位：0%",
            "当前无任何买入信号。",
        ])

        fixed, signal = _enforce_consolidation_consistency(report, current_price=None)

        self.assertEqual(signal, "SELL")
        self.assertIn("投资评级：【卖出】", fixed)
        self.assertIn("一致性校验", fixed)

    def test_mixed_ratings_are_normalized_conservatively_for_backend_summary(self):
        service = AnalysisService()

        self.assertEqual(service._normalize_decision_text("买入/持有"), "持有")
        self.assertEqual(service._normalize_decision_text("持有/观望"), "观望")
        self.assertEqual(service._normalize_decision_text("减持/卖出"), "卖出")

    def test_mixed_rating_is_used_when_recording_decision_memory(self):
        reduce_sell = _extract_decision_info("", "投资评级：【减持/卖出】")
        hold_watch = _extract_decision_info("", "投资评级：【持有/观望】")

        self.assertEqual(reduce_sell["decision_type"], "SELL")
        self.assertEqual(hold_watch["decision_type"], "HOLD")


if __name__ == "__main__":
    unittest.main()
