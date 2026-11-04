"""핵심 동작 시험(네트워크를 쓰지 않음).  python -m unittest discover -s analyzer/tests -t ."""
import json
import os
import tempfile
import unittest

from analyzer import config, db, pipeline, report
from analyzer.collectors import dart, fnguide

SEED = os.path.join(config.SEED_DIR, "213500")


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.conn = db.connect(self.tmp)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp)

    def test_periods_follow_year(self):
        p = config.periods(2027)
        self.assertEqual(p["past"], list(range(2017, 2027)))
        self.assertEqual(p["future"], [2028, 2029])

    def test_parse_dart_statement(self):
        d = json.load(open(os.path.join(SEED, "dart_2025_11011_CFS.json"), encoding="utf-8"))
        vals = {(y, k): v for (y, per, k, v, col) in dart.parse_statement(d)["values"]}
        self.assertEqual(vals[(2025, "acc_revenue")], 2290040442405.0)
        self.assertEqual(vals[(2024, "acc_eps_basic")], -1278.0)

    def test_half_year_uses_cumulative(self):
        d = json.load(open(os.path.join(SEED, "dart_2026_11012_CFS.json"), encoding="utf-8"))
        vals = {(y, per, k): v for (y, per, k, v, col) in dart.parse_statement(d)["values"]}
        self.assertEqual(vals[(2026, "H1", "acc_revenue")], 1152546492570.0)   # 3개월 값(592,742,934,637)이 아닌 누적
        self.assertEqual(vals[(2025, "H1", "acc_revenue")], 1140635669030.0)   # 전년 같은 기간 누적(최근 12개월 계산용)

    def test_reuse_without_refetch(self):
        r1 = pipeline.analyze("213500", 2026, offline=True, conn=self.conn)
        self.assertGreater(r1["stats"]["new_facts"], 100)
        r2 = pipeline.analyze("213500", 2026, offline=True, conn=self.conn)
        self.assertEqual(r2["stats"]["total_requests"], 0)
        self.assertEqual(r2["stats"]["new_facts"], 0)
        self.assertEqual(r2["stats"]["changed_facts"], 0)
        self.assertTrue(r2["had_data"])

    def test_roe_matches_dart_index(self):
        pipeline.analyze("213500", 2026, offline=True, conn=self.conn)
        roe = db.current_fact(self.conn, "213500", 2025, "roe")["value"]
        self.assertAlmostEqual(roe, 0.562, delta=0.01)   # DART 주요재무지표 ROE 0.562%

    def test_versioning_and_history(self):
        db.put_fact(self.conn, "000001", 2024, "acc_revenue", 100.0, source="DART", source_ref="A")
        self.assertEqual(db.put_fact(self.conn, "000001", 2024, "acc_revenue", 100.0, source="DART", source_ref="A"), "same")
        self.assertEqual(db.put_fact(self.conn, "000001", 2024, "acc_revenue", 110.0, source="DART", source_ref="B",
                                     change_reason="정정 공시"), "changed")
        h = db.history(self.conn, "000001")
        self.assertEqual((h[0]["old_value"], h[0]["new_value"], h[0]["change_reason"]), (100.0, 110.0, "정정 공시"))
        self.assertEqual(len(db.facts(self.conn, "000001", current_only=False)), 2)

    def test_consensus_snapshots_never_overwritten(self):
        self.assertEqual(db.add_consensus(self.conn, "000001", 2027, "eps", 500, "X", "2026-11-01"), 1)
        self.assertEqual(db.add_consensus(self.conn, "000001", 2027, "eps", 999, "X", "2026-11-01"), 0)
        self.assertEqual(db.add_consensus(self.conn, "000001", 2027, "eps", 520, "X", "2027-03-01"), 1)
        rows = self.conn.execute("SELECT value FROM consensus ORDER BY as_of").fetchall()
        self.assertEqual([r[0] for r in rows], [500, 520])
        self.assertEqual(db.latest_consensus(self.conn, "000001")[0]["value"], 520)

    def test_reverify_marks_stale_and_plans_refetch(self):
        pipeline.analyze("213500", 2026, offline=True, conn=self.conn)
        r = db.current_fact(self.conn, "213500", 2024, "acc_revenue")
        db.set_status(self.conn, r["id"], "STALE", "test")
        need = pipeline.plan(self.conn, "213500", config.periods(2026))
        self.assertIn(2024, need["statement_years"])

    def test_no_estimates_in_past_and_report_renders(self):
        res = pipeline.analyze("213500", 2026, offline=True, conn=self.conn)
        self.assertNotIn("ERROR", res["validation"]["summary"])
        html = report.render(report.payload(self.conn, "213500", 2026))
        self.assertIn("한솔제지", html)

    def test_valuation_runs_and_rules(self):
        from analyzer import valuation
        pipeline.analyze("213500", 2026, offline=True, conn=self.conn)
        res = valuation.run(self.conn, "213500", config.periods(2026))
        rows = {r["year"]: r for r in res["rows"]}
        self.assertIn(2025, rows)
        r = rows[2025]
        self.assertGreater(r["values"]["srim"], 0)
        self.assertAlmostEqual(r["ke"], 0.02894 + 0.055, places=6)            # 국고채 2025 + ERP, 베타 1
        self.assertIsNotNone(r["roic"]); self.assertIsNotNone(r["wacc"])
        for y, row in rows.items():                                            # 종합은 3개 이상일 때만
            if row["median"] is not None:
                self.assertGreaterEqual(len(row["values"]), 3)
            for v in row["values"].values():
                self.assertGreater(v, 0)                                       # 0 이하 가치는 저장하지 않음
        # 2016~2017은 상대가치 적정 배수가 아직 없어야 함(사후 정보 금지)
        self.assertIsNone(rows[2016]["fair"]["per"])

    def test_interest_classification(self):
        d = json.load(open(os.path.join(config.SEED_DIR, "086900", "dart_2025_11011_CFS.json"), encoding="utf-8"))
        keys = {(y, k) for (y, per, k, v, col) in dart.parse_statement(d)["values"]}
        self.assertIn((2025, "acc_int_paid_fin"), keys)
        self.assertNotIn((2025, "acc_int_paid"), keys)    # 재무활동 이자를 영업활동으로 세지 않음

    def test_search_by_name(self):
        from analyzer import search
        pipeline.analyze("213500", 2026, offline=True, conn=self.conn)
        db.ensure_company(self.conn, "213500", "한솔제지")
        self.assertEqual(search.resolve(self.conn, "한솔제지"), "213500")
        self.assertEqual(search.resolve(self.conn, " 한솔 제지 "), "213500")
        self.assertEqual(search.resolve(self.conn, "213500"), "213500")

    def test_naver_parser(self):
        from analyzer.collectors import naver
        page = """<div class="section cop_analysis"><table class="tb_type1 tb_num tb_info"><thead>
        <tr><th rowspan="3">주요재무정보</th><th colspan="4">최근 연간 실적</th><th colspan="6">최근 분기 실적</th></tr>
        <tr><th>2023.12</th><th>2024.12</th><th>2025.12</th><th>2026.12<em>(E)</em></th>
            <th>2025.06</th><th>2025.09</th><th>2025.12</th><th>2026.03</th><th>2026.06</th><th>2026.09<em>(E)</em></th></tr>
        <tr><th>IFRS연결</th></tr></thead><tbody>
        <tr><th><strong>매출액</strong></th><td>21,941</td><td>22,158</td><td>22,900</td><td>23,600</td><td>5,7</td><td>1</td><td>1</td><td>1</td><td>1</td><td>9,999</td></tr>
        <tr><th>ROE(지배주주)</th><td>0.2</td><td>-4.3</td><td>0.6</td><td>5.1</td><td></td><td></td><td></td><td></td><td></td><td>1</td></tr>
        <tr><th>EPS(원)</th><td>74</td><td>-1,278</td><td>163</td><td>1,520</td><td></td><td></td><td></td><td></td><td></td><td>1</td></tr>
        </tbody></table></div>"""
        got = {(o["target_year"], o["metric"], o["value"]) for o in naver.parse(page)}
        self.assertEqual(got, {(2026, "revenue", 23600.0), (2026, "roe", 5.1), (2026, "eps", 1520.0)})

    def test_failed_consensus_is_retried(self):
        db.log_fetch(self.conn, "consensus", "213500", "213500", False, 0, "blocked")
        self.assertFalse(db.fetched_recently(self.conn, "consensus", "213500", 7, ok_only=True))
        db.log_fetch(self.conn, "consensus", "213500", "213500", True, 5)
        self.assertTrue(db.fetched_recently(self.conn, "consensus", "213500", 7, ok_only=True))

    def test_wisereport_parser(self):
        from analyzer.collectors import wisereport
        sample = ('{"JsonData":[{"YYMM":"2025.12(A)","SALES":"22,900.4","OP":"499.3","NP":"38.9","EPS":"163","BPS":"29,322",'
                  '"PER":"50.89","PBR":"0.28","ROE":"0.56","EV":"8.00","MAIN":"IFRS연결"},'
                  '{"YYMM":"2027.12(E)","SALES":"24,465.5","OP":"742.5","NP":"202.5","EPS":"851","BPS":"25,624",'
                  '"PER":"7.77","PBR":"0.26","ROE":"3.36","EV":"7.68","MAIN":"IFRS연결"}]}')
        est, act, basis = wisereport.parse_data(sample)
        e = {(x["target_year"], x["metric"]): x["value"] for x in est}
        self.assertEqual(e[(2027, "eps")], 851.0)
        self.assertEqual(e[(2027, "revenue")], 24465.5)
        self.assertAlmostEqual(e[(2027, "op_margin")], 742.5 / 24465.5 * 100)
        self.assertEqual({x["target_year"] for x in act}, {2025})
        self.assertEqual(basis, "IFRS연결")
        page = "<table><tr><th>투자의견</th><th>목표주가</th><th>EPS</th><th>PER</th><th>추정기관수</th></tr>" \
               "<tr><td>4.00</td><td>10,250</td><td>1,073</td><td>6.16</td><td>2</td></tr></table>"
        self.assertEqual(wisereport.parse_opinion(page)["n"], 2)

    def test_fnguide_parser(self):
        page = """<div id="highlight_D_Y"><table><thead><tr><th>IFRS(연결)</th><th>2024/12</th><th>2025/12</th>
        <th>2026/12(E)</th><th>2027/12(E)</th></tr></thead><tbody>
        <tr><th>매출액</th><td>22,158</td><td>22,900</td><td>23,500</td><td>24,100</td></tr>
        <tr><th>EPS(원)</th><td>-1,278</td><td>163</td><td>900</td><td>1,100</td></tr></tbody></table></div>"""
        out = fnguide.parse(page)
        self.assertEqual({(o["target_year"], o["metric"], o["value"]) for o in out},
                         {(2026, "revenue", 23500.0), (2027, "revenue", 24100.0), (2026, "eps", 900.0), (2027, "eps", 1100.0)})


if __name__ == "__main__":
    unittest.main()
