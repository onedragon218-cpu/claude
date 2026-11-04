"""가치평가 계산 확인(한솔제지 예시 원본 + 시험용 가상 FnGuide 화면)."""
import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from fintrack import config, service, valuation
from fintrack.db import Store
from fintrack.metrics import build
from test_reuse import new_workdir


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = new_workdir(with_fnguide=True)
        service.analyze(cls.w, "213500", base_year=2026, offline=True)
        cls.s = Store(cls.w.db)
        cls.p = config.periods(2026)
        cls.m = build(cls.s, "213500", cls.p)
        cls.v, cls.t, cls.a = valuation.build(cls.s, "213500", cls.p, cls.m, cls.w)

    def test_roic_2025(self):
        # 세후영업이익 = 영업이익 × (1 − 실효세율), 실효세율 = (세전이익 − 당기순이익) ÷ 세전이익
        t = self.t[2025]
        self.assertAlmostEqual(t["tax"], (4531027893 - 3889085144) / 4531027893, places=6)
        self.assertAlmostEqual(self.v["roic"]["actual"][2025]["v"], 2.90, places=1)

    def test_wacc_between_kd_and_ke(self):
        t = self.t[2025]
        self.assertTrue(t["kd"] * (1 - t["tax"]) < t["wacc"] < t["ke"])
        self.assertAlmostEqual(t["ke"], 0.02894 + 0.055, places=6)

    def test_srim_formula(self):
        bps = self.m["bps"]["actual"][2025]["v"]; ke = self.t[2025]["ke"]
        r = self.m["roe"]["actual"]; rw = (r[2025]["v"] * 3 + r[2024]["v"] * 2 + r[2023]["v"]) / 6 / 100
        exp = bps + bps * (rw - ke) * 0.9 / (1 + ke - 0.9)
        self.assertAlmostEqual(self.v["srim"]["actual"][2025]["v"], exp, places=3)

    def test_relative_uses_median_and_consensus(self):
        med = self.a["multiples"]["per"]
        self.assertAlmostEqual(self.v["per_v"]["consensus"][2027]["v"], 1260 * med, places=3)   # 가상 화면 2027E EPS 1,260원
        self.assertIn(2028, self.v["psr_v"]["consensus"])

    def test_dcf_is_a_flow_with_flags(self):
        # 범위 밖 값도 지우지 않고 표시만 한다 → 과거 10개년 모두 이어진 선(2016은 2015 현금흐름 포함 평균)
        self.assertEqual(sorted(self.v["dcf"]["actual"]), self.p["actual"])
        flagged = {y: r["dcf_flag"] for y, r in self.t.items() if "dcf_flag" in r}
        self.assertTrue(flagged)
        for y, f in flagged.items():
            pnt = self.v["dcf"]["actual"].get(y) or self.v["dcf"]["consensus"].get(y)
            self.assertEqual(pnt["flag"], f["why"])

    def test_hamada_option_raises_wacc(self):
        import json
        with open(os.path.join(self.w.manual, "assumptions.json"), "w", encoding="utf-8") as f:
            json.dump({"DCF_하마다_베타": True}, f)
        try:
            _, t2, a2 = valuation.build(self.s, "213500", self.p, self.m, self.w)
        finally:
            os.remove(os.path.join(self.w.manual, "assumptions.json"))
        self.assertTrue(a2["hamada"])
        self.assertGreater(t2[2025]["wacc"], self.t[2025]["wacc"])

    def test_fair_is_median(self):
        import statistics
        y = 2025
        parts = [self.v[k]["actual"][y]["v"] for k in ("srim", "dcf", "per_v", "pbr_v", "psr_v")
                 if y in self.v[k]["actual"] and not self.v[k]["actual"][y].get("flag")]
        self.assertAlmostEqual(self.v["fair"]["actual"][y]["v"], statistics.median(parts), places=6)


if __name__ == "__main__":
    unittest.main()
