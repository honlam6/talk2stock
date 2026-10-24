#!/usr/bin/env python3
"""ask 规则直答层的路由单测（纯逻辑，不依赖 ERP / LLM）。"""
import os
import sys
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import ask as askmod   # noqa: E402


class TestRuleRoute(unittest.TestCase):
    def test_hits(self):
        cases = {
            "今天卖了多少？": "today_stats",
            "今天买了多少钱": "today_stats",
            "本月采购了多少": "month_stats",
            "这个月卖了多少": "month_stats",
            "谁欠我钱？": "debts",
            "我欠谁钱": "debts",
            "应收应付": "debts",
            "最近的单": "recent_bills",
            "最近录了哪些单": "recent_bills",
            "什么货快没了": "low_stock",
            "哪些商品缺货了": "low_stock",
            "库存值多少钱": "stock_value",
            "库存货值": "stock_value",
        }
        for text, tool in cases.items():
            self.assertEqual(askmod._rule_route(text), (tool, {}), msg=text)

    def test_misses_go_to_agent(self):
        """带参数 / 复合 / 含糊问句不得命中规则，必须交给 agent loop。"""
        for text in [
            "可乐还有多少库存？",            # 带商品参数
            "原料仓的可乐还有多少",          # 带商品+仓库参数
            "上个月哪个客户买得最多？他现在欠我多少钱？",  # 复合问题
            "今天买的可乐到了吗",            # 非统计意图（无"多少"）
            "帮我看看经营情况怎么样",        # 开放式
            "",                              # 空
            "今天买了多少？" + "账" * (25 - 7),  # 恰好 25 字 > 24 上限，交给 agent
            # 复合意图连接词（实测教训：曾被截胡只答一半）
            "今天卖了多少？另外库存值多少钱？",
            "谁欠我钱？顺便看看最近的单",
        ]:
            self.assertIsNone(askmod._rule_route(text), msg=text)

    def test_boundary_length(self):
        # 恰好 24 字的统计句仍可命中
        t = "本月采购了多少" + "账" * (24 - 7)
        self.assertEqual(askmod._rule_route(t), ("month_stats", {}))


if __name__ == "__main__":
    unittest.main()
