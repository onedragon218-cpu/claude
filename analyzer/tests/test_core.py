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
        self.assertAlmostEqual(r["ke"], 0.02894 + 0.05, places=6)             # 국고채 2025 + ERP 5%, 베타 1
        self.assertIsNotNone(r["roic"]); self.assertIsNotNone(r["wacc"])
        for y, row in rows.items():                                            # 종합은 3개 이상일 때만
            if row["median"] is not None:
                self.assertGreaterEqual(len(row["values"]), 3)
            for k, v in row["values"].items():
                self.assertGreaterEqual(v, 0)                                  # 음수는 0(주주 몫 없음)으로
                if v == 0:
                    self.assertIn(k, row.get("zero") or [])
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

    def test_zero_debt_vs_hidden_debt(self):
        """차입금 줄이 없을 때: 이자가 작으면 차입금 0(스튜디오드래곤), 크면 빈칸(메디톡스). 값을 만들지 않는다."""
        from analyzer import valuation
        for code in ("253450", "086900"):
            pipeline.analyze(code, 2026, offline=True, conn=self.conn)
        sd = {r["year"]: r for r in valuation.run(self.conn, "253450", config.periods(2026))["rows"]}
        for y in range(2017, 2026):
            self.assertIsNotNone(sd[y]["roic"], y); self.assertIsNotNone(sd[y]["wacc"], y)
        self.assertEqual(sd[2020]["debt"], 0)
        mt = {r["year"]: r for r in valuation.run(self.conn, "086900", config.periods(2026))["rows"]}
        self.assertIsNone(mt[2019]["roic"]); self.assertIn("roic", mt[2019]["missing"])
        self.assertIsNone(mt[2019]["debt"])

    def test_industry_benchmark_and_stability(self):
        from analyzer import benchmark
        self.assertEqual(benchmark.ecos_item("171"), "C17")
        self.assertEqual(benchmark.ecos_item("59114"), "J59")
        self.assertEqual(benchmark.ecos_item("211"), "C21")
        b = benchmark.for_company("171")
        self.assertEqual(b["debt_ratio"][2025], 96)                 # 한국은행 기업경영분석 C17 2025
        self.assertAlmostEqual(b["icr"][2025], 1.9683)
        self.assertIsNone(benchmark.for_company("999")["item"])    # 자료 없는 업종은 기준선 없음
        pipeline.analyze("213500", 2026, offline=True, conn=self.conn)
        st = report.payload(self.conn, "213500", 2026)["stability"]
        r24 = next(r for r in st["rows"] if r["year"] == 2024)
        self.assertAlmostEqual(r24["icr"], 21991294920 / r24["interest"], places=6)
        self.assertLess(r24["icr"], 1)
        self.assertTrue(any("1배 미만" in w for w in st["warnings"]))

    def test_universe_list_only(self):
        from analyzer import search, universe
        u = universe.load()
        self.assertEqual(len(u), 281)
        self.assertIn(("0126Z0", "삼성에피스홀딩스"), u)                    # 영문이 섞인 종목코드
        self.assertEqual(search.resolve(self.conn, "0126z0"), "0126Z0")
        self.assertEqual(search.resolve(self.conn, "스튜디오드래곤"), "253450")   # 키 없이도 목록에서 이름 검색
        # 목록은 DB에 넣지 않는다(자동 수집 대상 아님)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 0)

    def test_screener_exclusion_rules(self):
        from analyzer import screener
        cases = {("005930", "삼성전자"): None, ("005935", "삼성전자우"): "우선주·기타 종류주",
                 ("00104K", "CJ4우(전환)"): "우선주·기타 종류주", ("0099X0", "IBKS제25호스팩"): "스팩",
                 ("330590", "롯데리츠"): "리츠", ("088260", "이리츠코크렙"): "리츠",
                 ("088980", "맥쿼리인프라"): "인프라펀드", ("415640", "KB발해인프라"): "인프라펀드",
                 ("138040", "메리츠금융지주"): None, ("0126Z0", "삼성에피스홀딩스"): None,
                 ("064550", "바이오인프라"): None}
        for (code, name), want in cases.items():
            self.assertEqual(screener.classify(code, name), want, name)

    def test_krx_listing_parse(self):
        from analyzer.collectors import krx
        import unittest.mock as um, io
        body = '{"block1":[{"full_code":"KR7213500002","short_code":"213500","codeName":"한솔제지","marketEngName":"KOSPI"}]}'
        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass
        with um.patch("urllib.request.urlopen", return_value=R(body.encode())):
            self.assertEqual(krx.fetch(), [{"code": "213500", "name": "한솔제지", "market": "KOSPI", "isin": "KR7213500002"}])
        with um.patch("urllib.request.urlopen", return_value=R(b"LOGOUT")):
            with self.assertRaises(krx.KrxUnavailable):
                krx.fetch()

    def test_risk_rules(self):
        from analyzer import risk
        # 실제 공시 제목(2026-03-24, 코스닥시장본부)
        real = [{"report_nm": "주권매매거래정지기간변경              (상장폐지 사유 발생)", "rcept_dt": "20260324", "rcept_no": "1"},
                {"report_nm": "기타시장안내              (상장폐지 관련)", "rcept_dt": "20260324", "rcept_no": "2"},
                {"report_nm": "기타시장안내(관리종목지정우려종목)              (거래량 미달)", "rcept_dt": "20260324", "rcept_no": "3"}]
        self.assertEqual({k for k, *_ in risk.from_disclosures(real)}, {"상장폐지·실질심사", "거래정지", "관리종목 지정 우려"})
        cleared = [{"report_nm": "주권매매거래정지", "rcept_dt": "20260101", "rcept_no": "1"},
                   {"report_nm": "주권매매거래정지해제", "rcept_dt": "20260102", "rcept_no": "2"},
                   {"report_nm": "관리종목지정(감사의견 거절)", "rcept_dt": "20260301", "rcept_no": "3"},
                   {"report_nm": "관리종목지정해제", "rcept_dt": "20260601", "rcept_no": "4"}]
        self.assertEqual(risk.from_disclosures(cleared), [])
        acc = [{"fs_div": "CFS", "sj_div": "BS", "account_nm": "자본금", "thstrm_amount": "119,013,080,000"},
               {"fs_div": "CFS", "sj_div": "BS", "account_nm": "자본총계", "thstrm_amount": "697,642,267,848"}]
        ratio, cap, eq, basis = risk.from_accounts(acc)          # 한솔제지 2025 연결: 잠식 없음
        self.assertLess(ratio, 0); self.assertEqual(basis, "연결")
        acc2 = [{"fs_div": "OFS", "sj_div": "BS", "account_nm": "자본금", "thstrm_amount": "100"},
                {"fs_div": "OFS", "sj_div": "BS", "account_nm": "자본총계", "thstrm_amount": "40"}]
        self.assertAlmostEqual(risk.from_accounts(acc2)[0], 0.6)

    def test_cross_check_obs_do_not_break_selection(self):
        """WiseReport 교차검증 관측값(보고연도 없음)이 있어도 DART 원본 재해석이 깨지지 않아야 한다(실제 발생한 오류)."""
        pipeline.analyze("213500", 2026, offline=True, conn=self.conn)
        pipeline.record_cross_check(self.conn, "213500", [{"target_year": 2024, "metric": "revenue", "value": 22157.9}],
                                    "https://example/x&sDT=1")
        with open(os.path.join(SEED, "dart_2025_11011_CFS.json"), encoding="utf-8") as f:
            pipeline.ingest_statement(self.conn, "213500", json.load(f))
        self.assertEqual(db.current_fact(self.conn, "213500", 2024, "acc_revenue")["source"], "DART")

    def test_cash_flow_reconciles(self):
        pipeline.analyze("213500", 2026, offline=True, conn=self.conn)
        st = report.payload(self.conn, "213500", 2026)["stability"]
        r = next(x for x in st["rows"] if x["year"] == 2025)
        self.assertAlmostEqual(r["cash_only"], 256.40748607, places=4)
        self.assertAlmostEqual(r["cfo"] + r["cfi"] + r["cff"] + r["fx"], r["cash_chg"], delta=0.5)   # 억원 단위

    def test_xbrl_parser_consolidated_only(self):
        from analyzer.collectors import dart_xbrl as x
        ctx = lambda cid, mem, period, extra="": (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:segment>'
            f'<xbrldi:explicitMember dimension="ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis">ifrs-full:{mem}</xbrldi:explicitMember>{extra}'
            f'</xbrli:segment></xbrli:entity><xbrli:period>{period}</xbrli:period></xbrli:context>')
        y25 = "<xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate>"
        y24 = "<xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate>"
        q2 = "<xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2025-06-30</xbrli:endDate>"
        xml = "<xbrli:xbrl>" + ctx("C25", "ConsolidatedMember", y25) + ctx("C24", "ConsolidatedMember", y24) + \
            ctx("S25", "SeparateMember", y25) + ctx("Q2", "ConsolidatedMember", q2) + \
            ctx("CX", "ConsolidatedMember", y25, '<xbrldi:explicitMember dimension="ifrs-full:ClassesOfPropertyPlantAndEquipmentAxis">ifrs-full:MachineryMember</xbrldi:explicitMember>') + \
            ctx("I25", "ConsolidatedMember", "<xbrli:instant>2025-12-31</xbrli:instant>") + \
            '<dart:AdjustmentsForDepreciationExpense contextRef="C25">700</dart:AdjustmentsForDepreciationExpense>' \
            '<dart:AdjustmentsForDepreciationExpense contextRef="C24">680</dart:AdjustmentsForDepreciationExpense>' \
            '<dart:AdjustmentsForDepreciationExpense contextRef="S25">1</dart:AdjustmentsForDepreciationExpense>' \
            '<dart:AdjustmentsForDepreciationExpense contextRef="Q2">99</dart:AdjustmentsForDepreciationExpense>' \
            '<ifrs-full:DepreciationPropertyPlantAndEquipment contextRef="CX">5</ifrs-full:DepreciationPropertyPlantAndEquipment>' \
            '<dart:AdjustmentsForAmortisationExpense contextRef="C25">20</dart:AdjustmentsForAmortisationExpense>' \
            '<ifrs-full:DepreciationRightofuseAssets contextRef="C25">50</ifrs-full:DepreciationRightofuseAssets>' \
            '<ifrs-full:CurrentLeaseLiabilities contextRef="I25">40</ifrs-full:CurrentLeaseLiabilities>' \
            '<ifrs-full:NoncurrentLeaseLiabilities contextRef="I25">90</ifrs-full:NoncurrentLeaseLiabilities></xbrli:xbrl>'
        got = x.extract(x.parse_instance(xml))
        self.assertEqual(got[(2025, "FY")], {"acc_dep": 700, "acc_amort": 20, "acc_rou_dep": 50, "acc_lease_xbrl": 130})
        self.assertEqual(got[(2024, "FY")], {"acc_dep": 680})          # 분기 3개월 값·별도·다른 차원 값은 제외

    def test_leverage_grading(self):
        pipeline.analyze("253450", 2026, offline=True, conn=self.conn)
        st = report.payload(self.conn, "253450", 2026)["stability"]
        r22 = next(r for r in st["rows"] if r["year"] == 2022)
        # 2022: EBITDA = 영업이익 + 감가상각비 11.5억 + 무형자산상각비 1,499.7억 + 사용권자산상각비 21.8억
        self.assertAlmostEqual(r22["nd_ebitda"], r22["net_debt"] / r22["ebitda"])
        self.assertEqual(r22["lev_grade"], "안전")
        self.assertTrue(any("무형자산상각비가 영업이익보다" in n for n in r22["lev_notes"]))
        r24 = next(r for r in st["rows"] if r["year"] == 2024)
        self.assertEqual(r24["lev_grade"], "미확인")                    # 감가상각비 없음 → 단정하지 않음
        # 판정 규칙 단위 시험
        from analyzer import report as rp
        class BK:
            def __init__(s, d): s.d = d
            def v(s, y, k, per="FY"): return s.d.get((y, k, per))
            def ttm(s, *a): return None
            def latest_interim(s, y): return "H1"
        def grade(nd, op, dep, icr=None, kind="실적"):
            rows = [{"year": 2025, "kind": kind, "net_debt": nd, "icr": icr}]
            bk = BK({(2025, "acc_op", "FY"): op * 1e8, (2025, "acc_dep", "FY"): dep * 1e8, (2025, "acc_eq", "FY"): 1e11,
                     (2025, "acc_op", "H1"): op * 1e8, (2025, "acc_dep", "H1"): dep * 1e8})
            rp.grade_leverage(self.conn, "000000", rows, bk, {})
            return rows[0]
        self.assertEqual(grade(-10, 50, 10)["lev_grade"], "안전")      # 순현금
        self.assertEqual(grade(150, 50, 10)["lev_grade"], "보통")      # 2.5배
        self.assertEqual(grade(210, 50, 10)["lev_grade"], "주의")      # 3.5배
        self.assertEqual(grade(210, 50, 10, icr=0.8)["lev_grade"], "위험")   # 이자보상배율 1 미만과 겹침
        self.assertEqual(grade(300, 50, 10)["lev_grade"], "위험")      # 5배
        g = grade(100, -30, 10)
        self.assertEqual(g["lev_grade"], "위험"); self.assertIsNone(g["nd_ebitda"])   # EBITDA 적자, 계산 불가
        self.assertTrue(grade(100, 50, 10, kind="현재(H1말)")["lev_halfyear"])         # TTM 불가 → 반기 기준 표시

    def test_calibration_factor_and_raw_kept(self):
        import datetime as dt, unittest.mock as um
        from analyzer import calibration
        cal = {"fiscal_year": 2025, "companies": 3, "factors": {
            "revenue": {"0": {"median": 0.98, "n": 3}, "11": {"median": 0.95, "n": 3}},
            "op_income": {"0": {"median": 0.90, "n": 3}, "11": {"median": 0.70, "n": 3}},
            "eps": {"0": {"median": 0.85, "n": 3}, "11": {"median": 0.60, "n": 3}}}}
        today = dt.date(2026, 11, 5)
        from analyzer import valuation
        A = valuation.load_assumptions()
        self.assertEqual(calibration.factor("eps", 2026, today=today, cal=cal)[0], 0.83)      # 기본: 고정 0.83
        self.assertEqual(calibration.factor("revenue", 2026, today=today, cal=cal)[0], 1.0)   # 매출은 할인 안 함
        A2 = dict(A, 컨센서스_보정=dict(A["컨센서스_보정"], 방식="측정"))
        um.patch.object(valuation, "load_assumptions", return_value=A2).start()
        self.addCleanup(um.patch.stopall)
        f, why = calibration.factor("eps", 2026, today=today, cal=cal)      # 올해: 약 2개월 전 → 가장 가까운 0개월
        self.assertEqual(f, 0.85)
        f, why = calibration.factor("eps", 2027, today=today, cal=cal)      # 내년: 14개월 → 측정 범위 밖, 11개월 근사
        self.assertEqual(f, 0.60); self.assertIn("근사", why)
        # 그래프용 값은 보정, DB 컨센서스 원래 값은 그대로
        pipeline.analyze("213500", 2026, offline=True, conn=self.conn)
        db.add_consensus(self.conn, "213500", 2027, "eps", 1000.0, "WiseReport", "2026-11-04")
        with um.patch.object(calibration, "load", return_value=cal):
            pl = report.payload(self.conn, "213500", 2026)
        est = pl["series"]["eps"][2027]["est"]
        self.assertEqual(est["raw"], 1000.0); self.assertAlmostEqual(est["value"], 1000.0 * est["factor"])
        self.assertEqual(self.conn.execute("SELECT value FROM consensus WHERE stock_code='213500' AND target_year=2027 "
                                           "AND metric='eps'").fetchone()[0], 1000.0)

    def test_growth_table(self):
        pipeline.analyze("213500", 2026, offline=True, conn=self.conn)
        for y, rev, op, eps in ((2026, 23488, 686, 1073), (2027, 24465.5, 742.5, 851)):
            for m, v in (("revenue", rev), ("op_income", op), ("eps", eps)):
                db.add_consensus(self.conn, "213500", y, m, v, "WiseReport", "2026-11-04")
        g = report.growth_table(self.conn, config.periods(2026))
        r = next(x for x in g["rows"] if x["code"] == "213500")
        self.assertEqual(r["years"][2025]["kind"], "실적")
        self.assertAlmostEqual(r["years"][2025]["raw"]["revenue"], 22900.40442405)
        self.assertAlmostEqual(r["years"][2026]["raw"]["op_margin"], 686 / 23488 * 100)

    def test_roic_wacc_estimate_is_conservative(self):
        from analyzer import valuation
        pipeline.analyze("253450", 2026, offline=True, conn=self.conn)
        for y, op, bps in ((2027, 588.2, 30000.0), (2028, 690.1, 31000.0)):
            db.add_consensus(self.conn, "253450", y, "op_income", op, "WiseReport", "2026-11-04")
            db.add_consensus(self.conn, "253450", y, "bps", bps, "WiseReport", "2026-11-04")
        rows = valuation.run(self.conn, "253450", config.periods(2026))["rows"]
        base = next(r for r in reversed(rows) if r.get("roic") is not None and not r.get("estimated"))
        est = [r for r in rows if r.get("estimated")]
        self.assertTrue(est)
        for r in est:
            self.assertGreater(r["year"], base["year"])
            self.assertGreaterEqual(r["wacc"], base["wacc"])                  # WACC는 높게
            self.assertGreaterEqual(r["ic"], base["ic"])                      # 투하자본은 크게
            self.assertGreaterEqual(r["tax"], 0.099)            # 법정 한계세율(지방세 포함) — 최저 구간 9%×1.1 이상
            self.assertTrue(any("예상(보수적 추정)" in n for n in r["notes"]))

    def test_fnguide_parser(self):
        page = """<div id="highlight_D_Y"><table><thead><tr><th>IFRS(연결)</th><th>2024/12</th><th>2025/12</th>
        <th>2026/12(E)</th><th>2027/12(E)</th></tr></thead><tbody>
        <tr><th>매출액</th><td>22,158</td><td>22,900</td><td>23,500</td><td>24,100</td></tr>
        <tr><th>EPS(원)</th><td>-1,278</td><td>163</td><td>900</td><td>1,100</td></tr></tbody></table></div>"""
        out = fnguide.parse(page)
        self.assertEqual({(o["target_year"], o["metric"], o["value"]) for o in out},
                         {(2026, "revenue", 23500.0), (2027, "revenue", 24100.0), (2026, "eps", 900.0), (2027, "eps", 1100.0)})

    def test_consensus_trend_revisions(self):
        P = config.periods(2026)
        rows = [(2026, "op_income", "2026-05-29", 100.0, "억원"), (2026, "op_income", "2026-08-31", 120.0, "억원"),
                (2026, "op_income", "2026-09-30", 118.0, "억원"), (2026, "op_income", "2026-10-30", 110.0, "억원"),
                (2026, "op_income", "2026-11-04", 90.0, "억원"),          # 같은 달 안에서는 마지막 값만 쓴다
                (2026, "op_income", "2026-11-03", 95.0, "억원"),
                (2027, "eps", "2026-08-31", -50.0, "원"), (2027, "eps", "2026-11-04", 30.0, "원"),
                (0, "target_price", "2026-11-04", 5000.0, "원")]
        db.add_consensus_history(self.conn, "000001", rows)
        self.assertEqual(db.add_consensus_history(self.conn, "000001", rows), 0)   # 다시 넣어도 쌓이지 않음
        t = report.consensus_trend(self.conn, "000001", P)
        op = next(x for x in t["summary"] if x["year"] == 2026 and x["metric"] == "op_income")
        self.assertEqual(op["value"], 90.0)
        self.assertAlmostEqual(op["ch"][1], (90 / 110 - 1) * 100)
        self.assertAlmostEqual(op["ch"][3], (90 / 120 - 1) * 100)
        self.assertAlmostEqual(op["ch"][6], (90 / 100 - 1) * 100)
        self.assertIsNone(op["ch"][12])
        self.assertEqual(op["dir"], "하향")
        eps = next(x for x in t["summary"] if x["year"] == 2027 and x["metric"] == "eps")
        self.assertIsNone(eps["ch"][3])
        self.assertEqual((eps["tag"][3], eps["dir"]), ("흑자전환", "상향"))
        self.assertEqual(report._revision(-50, -80), (None, "적자확대"))
        self.assertFalse(report.consensus_trend(self.conn, "999999", P)["available"])

    def test_segments_keywords_and_products(self):
        from analyzer import segments
        from analyzer.collectors import wisereport
        self.assertEqual(segments.auto_keywords("인쇄용지, 특수지"), ["인쇄용지", "특수지"])
        self.assertEqual(segments.auto_keywords("건설부문"), ["건설"])
        self.assertEqual(segments.auto_keywords("제품(수출)"), [])
        self.assertEqual(segments.auto_keywords("드라마 판매"), ["드라마"])
        page = '<h6>주요제품 매출구성(2026 / 06)</h6><script>var chartData1 = [["산업용지",27.34],["건설부문",3.76]];</script>'
        self.assertEqual(wisereport.parse_products(page), ("2026-06", [("산업용지", 27.34), ("건설부문", 3.76)]))

    def test_news_flow_groups_and_clusters(self):
        from analyzer import segments
        db.ensure_company(self.conn, "000001", "가나제지", None)
        db.add_segments(self.conn, "000001", "2026-06", [("산업용지", 30.0, None, "%", None), ("기타", 5.0, None, "%", None)], "테스트")
        db.add_news(self.conn, "000001", [
            {"published": "2026-09-22", "title": "가나제지, 백판지 판매 증가로 산업용지 호조"},
            {"published": "2026-09-23", "title": "산업용지 호조…가나제지 백판지 판매 증가"},
            {"published": "2026-09-25", "title": "가나제지 대표 교체"}], "테스트")
        f = segments.news_flow(self.conn, "000001", "가나제지", "2026-01-01")
        self.assertEqual(f["flow"]["산업용지"]["count"], 2)
        top = f["flow"]["산업용지"]["months"][0]["top"]
        self.assertEqual((len(top), top[0]["n"]), (1, 2))          # 비슷한 제목 2건은 한 묶음
        self.assertTrue(top[0]["link"].startswith("https://search.naver.com/"))   # 링크 없으면 제목 검색
        self.assertEqual(f["flow"]["회사 전반"]["count"], 1)
        self.assertEqual(f["flow"]["기타"]["count"], 0)           # '기타'는 키워드가 없어 아무것도 안 걸림

    def test_peers_by_segment_keywords(self):
        from analyzer import segments
        P = config.periods(2026)
        for code, name, segs in (("000001", "가나제지", [("산업용지", 30.0)]), ("000002", "다라판지", [("백판지, 산업용지", 80.0)]),
                                 ("000003", "마바수출", [("제품(수출)", 90.0)])):
            db.ensure_company(self.conn, code, name, None)
            db.add_segments(self.conn, code, "2026-06", [(n, p, None, "%", None) for n, p in segs], "테스트")
            db.put_fact(self.conn, code, 2025, "revenue", 1000.0, unit="억원", source="derived")
        self.assertEqual(segments.company_metrics(self.conn, "000002", 2025)["revenue"], 1000.0)
        _, gs, _ = segments.groups(self.conn, "000001")
        pr = segments.peers(self.conn, "000001", P, gs)
        cands = pr["by_segment"]["산업용지"]
        self.assertEqual([c["code"] for c in cands], ["000002"])
        self.assertEqual(cands[0]["match"], "사업부 일치")

    def test_news_google_rss_parse(self):
        from analyzer.collectors import news
        rss = """<rss><channel><item><title>가나제지 증설 - 한국경제</title><link>https://news.google.com/x</link>
        <pubDate>Tue, 22 Sep 2026 01:00:00 GMT</pubDate><source url="https://hankyung.com">한국경제</source></item></channel></rss>"""
        orig = news._get
        news._get = lambda url, headers=None: rss
        try:
            n, items = news.google("가나제지", __import__("datetime").date(2026, 9, 1), __import__("datetime").date(2026, 9, 30))
        finally:
            news._get = orig
        self.assertEqual(n, 1)
        self.assertEqual(items[0], {"published": "2026-09-22", "title": "가나제지 증설", "link": "https://news.google.com/x", "press": "한국경제"})

    def test_good6_keys_and_marks(self):
        from analyzer import goodco
        pipeline.import_seed(self.conn, "213500", 2026)
        g = goodco.good6(self.conn, "213500", config.periods(2026))
        self.assertEqual(set(g["rows"]), {"one", "two", "three", "four", "five", "six"})
        for r in g["rows"].values():
            m = r["mark"]
            self.assertTrue(m in ("✓", "△", "✗", "조사") or m.startswith("판정 못 함") or m == "✓(컨센 기준)", m)
        self.assertEqual(g["rows"]["five"]["mark"], "조사")        # ⑤는 코드가 채우지 않음

    def test_good6_rule_three(self):
        from analyzer import goodco
        self.assertEqual(goodco.rule_three([5, 8, 7], [], 20), "△")
        self.assertEqual(goodco.rule_three([5, 7, 8], [], 20), "✓")
        self.assertEqual(goodco.rule_three([8, 7, 5], [], 20), "✗")
        self.assertEqual(goodco.rule_three([8, 7, 5], [25], 20), "✓")   # 컨센 추정이 정점을 넘으면 ✓

    def test_good6_rule_six_single_source(self):
        from analyzer import goodco
        self.assertEqual(goodco.rule_six(1000, 0, 500, 600, 1)[0], "판정 못 함(원천 대기)")
        self.assertEqual(goodco.rule_six(1000, 0, 500, 600, 5)[0], "✓(컨센 기준)")    # 1000/7.8=128 < 415
        self.assertEqual(goodco.rule_six(10000, 0, 500, 600, 5)[0], "✗")
        self.assertTrue(goodco.rule_six(1000, 0, 500, 600, None)[1].endswith("원천 수 미확인"))

    def test_value5_face_values_from_seed(self):
        from analyzer import value5
        fb = value5.face_by_year("213500")
        r = fb[2025]
        self.assertAlmostEqual(r["recv"], 335510484604)
        self.assertAlmostEqual(r["inv"], 372909723854)
        self.assertAlmostEqual(r["pay"], 152423586979)
        self.assertAlmostEqual(r["nci"], 421310695)
        self.assertAlmostEqual(r["debt_cur"], 517645292384)     # 단기차입금및사채
        self.assertAlmostEqual(r["div"], 11889153400)

    def test_value5_xbrl_notes(self):
        from analyzer import value5
        ctx = lambda cid, dims, inst: (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:segment>' +
            "".join(f'<xbrldi:explicitMember dimension="{a}">{m}</xbrldi:explicitMember>' for a, m in dims) +
            f'</xbrli:segment></xbrli:entity><xbrli:period>{inst}</xbrli:period></xbrli:context>')
        I = "<xbrli:instant>2025-12-31</xbrli:instant>"
        D = "<xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate>"
        C = ("ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis", "ifrs-full:ConsolidatedMember")
        xml = "".join([
            ctx("c1", [C, ("ifrs-full:ClassesOfPropertyPlantAndEquipmentAxis", "ifrs-full:LandMember")], I),
            ctx("c2", [C, ("ifrs-full:ClassesOfPropertyPlantAndEquipmentAxis", "ifrs-full:BuildingsMember")], I),
            ctx("c3", [C, ("ifrs-full:ClassesOfPropertyPlantAndEquipmentAxis", "ifrs-full:MachineryMember")], I),
            ctx("c4", [C, ("ifrs-full:MaturityAxis", "ifrs-full:LaterThanOneYearAndNotLaterThanTwoYearsMember")], I),
            ctx("c5", [C, ("ifrs-full:MaturityAxis", "ifrs-full:LaterThanTwoYearsAndNotLaterThanThreeYearsMember")], I),
            ctx("c6", [C], D),
            ctx("s1", [("ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis", "ifrs-full:SeparateMember"),
                       ("ifrs-full:ClassesOfPropertyPlantAndEquipmentAxis", "ifrs-full:LandMember")], I),
            '<ifrs-full:PropertyPlantAndEquipment contextRef="c1">100</ifrs-full:PropertyPlantAndEquipment>',
            '<ifrs-full:PropertyPlantAndEquipment contextRef="c2">200</ifrs-full:PropertyPlantAndEquipment>',
            '<ifrs-full:PropertyPlantAndEquipment contextRef="c3">300</ifrs-full:PropertyPlantAndEquipment>',
            '<ifrs-full:PropertyPlantAndEquipment contextRef="s1">999</ifrs-full:PropertyPlantAndEquipment>',
            '<ifrs-full:Borrowings contextRef="c4">40</ifrs-full:Borrowings>',
            '<ifrs-full:Borrowings contextRef="c5">50</ifrs-full:Borrowings>',
            '<ifrs-full:InterestExpense contextRef="c6">7</ifrs-full:InterestExpense>'])
        r = value5.notes_from_xml(xml)[2025]
        self.assertEqual((r["land"], r["bldg"], r["mat2"], r["mat3"], r["interest"]), (100, 500, 40, 50, 7))

    def test_value5_compute_rules(self):
        from analyzer import value5
        base = dict(kind="실적", op=100.0, ni=10.0, eq=1000.0, liab=500.0, nci=0.0, shares=10.0, da=20.0, capex=20.0,
                    wc=50.0, cash_st=100.0, recv=100.0, inv=100.0, land=100.0, bldg=100.0, nd=200.0, interest=5.0,
                    mat1=10.0, mat2=10.0, mat3=10.0, rf=0.03, wacc=0.08, div=0.0)
        Y = {y: dict(base, year=y, op=float(o), tax=0.22, fcff=float(o) * 0.78) for y, o in zip(range(2010, 2026), range(160, 0, -10))}   # 2010 160 … 2025 10
        data = {"years": Y, "order": sorted(Y), "price": 50.0, "stress_from": 2006, "stress_to": 2025, "g": 0.0}
        bad, by, n = value5.stress_op(data)
        self.assertEqual((n, bad, by), (16, 40.0, 2022))   # 16년 중 하위 20% = ceil(16×0.2)=4번째로 낮은 값(10·20·30·40)
        r = value5.compute(data)["rows"][2025]
        fcff = 10 * 0.78                                   # 감가상각 = 설비투자, 운전자본 변화 0
        fn = sum(o * 0.78 for o in (50, 40, 30, 20, 10)) / 5
        self.assertAlmostEqual(r["fcff"], fcff)
        self.assertAlmostEqual(r["fcff_n"], fn)
        self.assertAlmostEqual(r["dcf"], (fn / 0.08 - 200) / 10)       # 23.4 ÷ 0.08 − 200 = 92.5 → 9.25원
        self.assertEqual(r["asset"], 0.0)                # (100+85+60+100+30 − 500)/10 < 0 → 0
        self.assertIn("asset", r["no_equity"])
        st = (40 * 0.78 / 0.08 - 200) / 10               # ④ = (불황 40 × 0.78 ÷ 0.08 − 200)/10 = 19
        self.assertAlmostEqual(r["stress"], st)
        short = max(0, 3 * 5 + 30 - 3 * fn)              # 15 + 30 − 70.2 < 0 → 0
        self.assertAlmostEqual(r["risk"], st * 10 / (10 + short / 50))
        ke = 0.08
        self.assertAlmostEqual(r["rim"], (1000 + (0.01 - ke) * 1000 * 0.6 / (1 + ke - 0.6)) / 10)

    def test_stability_warning_only_recent_5_years(self):
        pipeline.import_seed(self.conn, "086900", 2026)
        st = report.payload(self.conn, "086900", 2026)["stability"]
        self.assertFalse(any("이자보상배율" in w for w in st["warnings"]))       # 2020년은 5년보다 오래됨 → 상단 경고 없음
        self.assertTrue(any("2020년" in w for w in st["old_warnings"]))
        self.assertEqual(st["warn_from"], 2021)

    def test_batch_job_stops_before_next_company(self):
        from analyzer import server
        with server.JOB_LOCK:
            server.JOB.update(running=True, total=2, done=0, results=[], stop=True, stopped=False, finished_at=None)
        server._update_all([("000001", "가"), ("000002", "나")], 2026, False)
        self.assertTrue(server.JOB["stopped"])
        self.assertEqual((server.JOB["done"], server.JOB["running"]), (0, False))   # 중지 요청이 있으면 다음 기업을 시작하지 않음

    def test_upvalue_rising_and_tiers(self):
        from analyzer import upvalue
        self.assertEqual(upvalue.rising([1, 2, 3, 4], "%")[0], True)
        self.assertEqual(upvalue.rising([1, 2, 2, 4], "%")[0], False)          # 같으면 우상향 아님
        self.assertEqual(upvalue.rising([-100, 50, 80, 100], "%", 20)[0], True)  # 적자 → 흑자(턴어라운드), 이 구간은 % 문턱 제외
        self.assertEqual(upvalue.rising([100, 110, 115, 130], "%", 6)[0], False)   # 110→115 = +4.5% < 6%
        self.assertEqual(upvalue.rising([5, 5.5, 6.5, 7], "%p", 0.5)[0], True)
        self.assertIsNone(upvalue.rising([1, 2, None, 4], "%")[0])
        row = {"price": 10000.0, "series": {m: [1, 2, 3, 4] for m in upvalue.METRICS},
               "roe": [8.0, 10.0, 15.0], "bps": [10000.0, 11000.0, 12000.0]}
        c = upvalue.classify(row)
        # 적정가 = ROE × 0.1 × 0.83 × BPS → 6,640 / 9,130 / 14,940
        self.assertEqual([round(v) for v in c["fair"]], [6640, 9130, 14940])
        self.assertEqual(c["tier"], 2)                   # 내후년 기준에서야 쌈 → 1차
        self.assertEqual(upvalue.classify(row, factor=False)["tier"], 1)   # 보정 끄면 내년 기준 11,000 > 10,000 → 2차
        row["series"]["eps"] = [1, 2, None, 4]
        self.assertEqual(upvalue.classify(row)["status"], "자료 부족")
        self.assertIn("내년 컨센서스", upvalue.classify(row)["why"])
        row["series"]["roe"] = [9, 8, 7, 6]                 # 있는 값으로 이미 우상향이 깨지면 자료 부족보다 탈락이 먼저
        self.assertEqual(upvalue.classify(row)["status"], "우상향 아님")
        row["series"]["roe"] = [10, 11, 12, 13]
        self.assertEqual(upvalue.classify(row, use=("roe",))["status"], "통과")
        # min PBR: 우상향 통과 + (10년 최저 연말 PBR × 최근 결산 BPS) > 현재 주가
        row["minpbr"] = {"price": 12000.0}
        c = upvalue.classify(row, use=("roe",))
        self.assertTrue(c["minpbr_cheap"])
        self.assertAlmostEqual(c["minpbr_margin"], 0.2)
        row["minpbr"] = {"price": 9000.0}
        self.assertFalse(upvalue.classify(row, use=("roe",))["minpbr_cheap"])

    def test_min_pbr_from_year_end(self):
        from analyzer import goodco
        class FakeD:
            cy = 2026
            years = list(range(2014, 2026))                    # 2014~2025 — 최근 10개 연도는 2016~2025
            pb = {2014: 0.2, 2015: 0.3, 2018: 0.5, 2020: 0.6, 2025: 0.9}
            def v(self, y, k, per="FY"):
                if k == "market_cap":
                    return self.pb.get(y, 1.0) * 100.0        # 억
                if k == "bps":
                    return 20000.0 if y == 2025 else None
                return None
            def eok(self, y, k, per="FY"):
                return 100.0 if k == "acc_eq_owner" else None
            def st(self, y, k, per="FY"):
                return "VERIFIED"
        m = goodco.min_pbr(FakeD())
        self.assertEqual((m["pbr"], m["year"], m["n"]), (0.5, 2018, 10))   # 2014·2015는 10년 밖
        self.assertEqual((m["bps"], m["bps_year"]), (20000.0, 2025))
        self.assertAlmostEqual(m["price"], 10000.0)

    def test_year_rollover_and_rf_override(self):
        import datetime as _d, json as _j, tempfile
        from unittest import mock
        from analyzer import config, rates
        class D(_d.date):
            pass
        for today, by in ((_d.date(2027, 3, 31), 2026), (_d.date(2027, 4, 1), 2027), (_d.date(2026, 11, 13), 2026)):
            D.today = classmethod(lambda cls, t=today: t)
            with mock.patch.object(config._dt, "date", D):
                self.assertEqual(config.periods()["base_year"], by)
        self.assertEqual(config.periods(2030)["base_year"], 2030)          # 화면에서 고른 기준 연도는 그대로
        A = {"무위험수익률_국고채10년": {"_설명": "x", "2025": 2.8, "2026": 3.1}}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(config, "DATA_DIR", d):
            self.assertFalse(rates.info(A, 2027)["ok"])
            self.assertEqual(rates.info(A, 2027)["used"], 2026)
            with open(os.path.join(d, "rates.json"), "w", encoding="utf-8") as f:
                _j.dump({"kr10y": {"2026": 3.2, "2027": 3.4}}, f)
            t, src = rates.table(A)
            self.assertEqual((t[2025], t[2026], t[2027]), (2.8, 3.2, 3.4))   # ECOS 값이 설정 파일 값을 덮어씀
            self.assertEqual(src[2027], "ECOS 자동")
            from analyzer import valuation
            self.assertEqual(valuation._rf(A, 2027), (0.034, None))

    def test_update_selected_companies_only(self):
        import http.client, threading, time
        from http.server import ThreadingHTTPServer
        from unittest import mock
        from analyzer import server
        got = []
        def fake(codes, by, fresh):
            got.append((codes, fresh))
            with server.JOB_LOCK:
                server.JOB["running"] = False
        conn = db.connect(); names = {r["stock_code"]: r["name"] for r in conn.execute("SELECT stock_code, name FROM companies")}; conn.close()
        pick = sorted(names)[:1]
        with mock.patch.object(server, "_update_all", fake):
            srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def post(body):
                    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
                    b = json.dumps(body).encode()
                    c.request("POST", "/api/update-all?fresh=1", body=b, headers={"Content-Type": "application/json", "Content-Length": str(len(b)), "Origin": f"http://127.0.0.1:{srv.server_port}"})
                    r = c.getresponse(); d = json.loads(r.read() or b"{}"); c.close(); return r.status, d
                st, d = post({"codes": pick + ["999999"]})
                self.assertEqual((st, d.get("kind")), (200, "선택 갱신"))
                for _ in range(50):
                    if got: break
                    time.sleep(0.05)
                self.assertEqual([c for c, _ in got[0][0]], pick)       # DB에 없는 코드는 빠지고 고른 기업만
                self.assertTrue(got[0][1])
                st, d = post({"codes": ["999999"]})
                self.assertEqual(st, 400)
            finally:
                srv.shutdown(); srv.server_close()

    def test_fairper_formula_guards_and_payload(self):
        from analyzer import fairper
        # 1) 공식 값
        per, why = fairper.fair_per(0.12, 0.10, 0.03)
        self.assertAlmostEqual(per, 10.714, places=3)
        self.assertIsNone(why)
        self.assertAlmostEqual(per * 1000 * 0.83, 8892.9, places=1)
        # 2) 막는 조건
        self.assertIn("ROE ≤ g", fairper.fair_per(0.03, 0.10, 0.03)[1])
        self.assertIn("Ke − g", fairper.fair_per(0.10, 0.04, 0.03)[1])
        # 3) ROE = Ke면 성장과 무관하게 1/Ke
        for g in (0.0, 0.02, 0.04):
            self.assertAlmostEqual(fairper.fair_per(0.10, 0.10, g)[0], 10.0, places=9)
        # 4) 항등식: ROE = EPS/BPS 이면 PER × EPS = (ROE − g)/(Ke − g) × BPS
        for roe, ke, g, bps in ((0.15, 0.09, 0.03, 20000.0), (0.08, 0.11, 0.02, 5000.0)):
            eps = roe * bps
            self.assertAlmostEqual(fairper.fair_per(roe, ke, g)[0] * eps, (roe - g) / (ke - g) * bps, delta=1e-9)
        # 5) 시드 213500: 세 해 행, 예외 없음, 원천 1곳인 해에 '원천 대기'
        conn = db.connect()
        P = config.periods(2026)
        pl = fairper.payload(conn, "213500", P)
        cur = next(r for r in pl["rows"] if r["code"] == "213500")
        self.assertIn("data", cur)
        res = fairper.compute(cur["data"])
        self.assertEqual([r["y"] for r in res["rows"]], [2026, 2027, 2028])
        d = dict(cur["data"], years=[dict(y, n_est=1 if i == 1 else y["n_est"]) for i, y in enumerate(cur["data"]["years"])])
        rows = fairper.compute(d)["rows"]
        self.assertIn("판정 못 함(원천 대기)", rows[1]["tags"])
        self.assertTrue(all(r["n_est"] is None or r["n_est"] > 1 or "판정 못 함(원천 대기)" in r["tags"] for r in res["rows"]))
        # ROE 이월: 기말 자본 = 기초 + 보정 순이익 × (1 − d)
        r0 = res["rows"][0]
        self.assertAlmostEqual(r0["e_end"], r0["e_start"] + r0["ni"] * (1 - cur["data"]["d"]), places=6)
        self.assertEqual(len(fairper.sensitivity(cur["data"])), 3)
        conn.close()

    def test_delete_companies_backup_and_doca_cleanup(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            raw = sqlite3.connect(path)                     # 되돌린 기능이 만들었던 표가 남아 있는 DB
            raw.execute("CREATE TABLE node_judgments (id INTEGER, stock_code TEXT)")
            raw.execute("CREATE TABLE peer_groups (id INTEGER, target TEXT)")
            raw.commit(); raw.close()
            conn = db.connect(path)
            have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertFalse(have & set(db.DOCA_TABLES))        # 4) 표 삭제
            self.assertIn("facts", have)
            db.ensure_company(conn, "213500", "한솔제지")
            conn.execute("UPDATE companies SET created_at='2026-11-01 09:00:00' WHERE stock_code='213500'")
            for c, t in (("000001", "2026-11-15 10:00:05"), ("000002", "2026-11-15 10:00:09"), ("000003", "2026-11-15 12:00:00")):
                db.ensure_company(conn, c, "가짜" + c)
                conn.execute("UPDATE companies SET created_at=? WHERE stock_code=?", (t, c))
            conn.execute("INSERT INTO fetch_log(source, key, fetched_at, ok, n_items) VALUES ('doca_import', 'x', '2026-11-15 10:00:10', 1, 3)")
            conn.execute("INSERT INTO facts(stock_code, fiscal_year, period, metric, value, is_current, status, version) VALUES ('000001', 2025, 'FY', 'revenue', 1, 1, 'VERIFIED', 1)")
            conn.execute("INSERT INTO news(stock_code, published, title) VALUES ('000001', '2026-11-15', 't')")
            conn.commit()
            self.assertEqual(db.doca_added(conn), ["000001", "000002"])   # 가져오기 시각 직전 10분 안에 등록된 기업만
            bk = db.backup(path)
            self.assertTrue(os.path.exists(bk))
            res = db.delete_companies(conn, ["000001", "000002"])
            self.assertEqual(res.get("companies"), 2)
            self.assertEqual((res.get("facts"), res.get("news")), (1, 1))
            left = [r[0] for r in conn.execute("SELECT stock_code FROM companies ORDER BY 1")]
            self.assertEqual(left, ["000003", "213500"])
            self.assertEqual(sqlite3.connect(bk).execute("SELECT COUNT(*) FROM companies").fetchone()[0], 4)   # 백업에는 지우기 전 그대로
            conn.close()

    def test_nodeview_knowledge_engine(self):
        from unittest import mock
        from analyzer import nodeview
        K = nodeview.knowledge()
        self.assertGreaterEqual(len(K["nodes"]), 50)
        for n in K["nodes"].values():                          # 사전 무결성: 없는 흐름·범위 밖 영향 없음
            for t, v in n["themes"].items():
                self.assertIn(t, K["themes"], n["id"])
                self.assertTrue(-2 <= v <= 2)
        # 연결: 사용자 > Claude 검토 > 이름 낱말(긴 것 우선) > 업종코드
        self.assertEqual(nodeview.link_segment("인쇄용지, 특수지", "171", K, {}, {})[0], "paper_print")
        self.assertEqual(nodeview.link_segment("제품(수출)", "211", K, {}, {})[0], "pharma")
        self.assertEqual(nodeview.link_segment("제품(수출)", "211", K, {}, {"제품(수출)": "botulinum"}), ("botulinum", "Claude 검토"))
        self.assertEqual(nodeview.link_segment("제품(수출)", "211", K, {"제품(수출)": "cdmo"}, {"제품(수출)": "botulinum"})[1], "사용자 지정")
        self.assertTrue(nodeview.is_generic("용역부문 등") and nodeview.is_generic("제품(내수)") and not nodeview.is_generic("산업용지"))
        # 판정 규칙: 같은 입력이면 같은 답, 문턱대로
        node = {"themes": {"T_DIGITAL": -2}}
        cs = {"op_rev3": (-10.0, 2027), "fwd_g": 0.0, "rev_g2": -0.06}
        a = nodeview.judge(node, K, {"pct": 70}, cs, (-1, "x"))
        self.assertEqual(a, nodeview.judge(node, K, {"pct": 70}, cs, (-1, "x")))
        self.assertEqual(a["verdict"], "비우호")
        self.assertAlmostEqual(a["score"], -2 * K["themes"]["T_DIGITAL"]["strength"] - 1 - 1, places=6)
        self.assertEqual(a["confidence"], "높음")
        b = nodeview.judge({"themes": {}}, K, {"pct": 20}, {"op_rev3": None, "fwd_g": None}, (None, "없음"))
        self.assertEqual((b["verdict"], b["confidence"]), ("중립", "낮음"))
        half = nodeview.judge({"themes": {}}, K, {"pct": 20}, {"op_rev3": (5.0, 2027), "fwd_g": 0.1}, (None, ""))
        self.assertEqual((half["score"], half["verdict"]), (1.0, "약한 우호"))     # 비중 60% 미만이면 회사 숫자 절반
        s1, _ = nodeview.structural({"themes": {"T_AI": 2, "T_GRID": 2}}, K)
        self.assertEqual(s1, 2.0)                                                     # −2~+2로 자름
        # Claude 검토 넣기: 형식 검사·합치기
        clean, notes = nodeview.validate_review({"themes": [{"id": "T_AI", "strength": 5}], "nodes": [{"id": "botulinum", "themes": {"T_AGING": 3, "T_KCULT": 1}}],
                                                 "links": [{"stock_code": "086900", "segment": "제품(수출)", "node": "botulinum"}, {"stock_code": "x", "segment": "a", "node": "b"}],
                                                 "verdicts": [{"stock_code": "086900", "segment": "제품(수출)", "verdict": "우호"}, {"stock_code": "086900", "segment": "a", "verdict": "좋음"}]}, K)
        self.assertEqual(clean["themes"][0]["strength"], 1.0)
        self.assertEqual(clean["nodes"][0]["themes"], {"T_KCULT": 1})
        self.assertEqual((len(clean["links"]), len(clean["verdicts"]), len(notes)), (1, 1, 3))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(config, "DATA_DIR", d):
            nodeview.save_review(clean)
            nodeview.save_review({"as_of": "2026-12-01", "themes": [], "nodes": [], "links": [{"stock_code": "086900", "segment": "제품(수출)", "node": "cdmo"}], "verdicts": []})
            K2 = nodeview.knowledge()
            self.assertEqual(K2["review"]["links"], [{"stock_code": "086900", "segment": "제품(수출)", "node": "cdmo"}])   # 같은 사업부는 새 것으로
            self.assertEqual(len(K2["review"]["verdicts"]), 1)                                                                # 이전 판정은 유지
            self.assertEqual(K2["themes"]["T_AI"]["strength"], 1.0)
            self.assertTrue(os.listdir(os.path.join(d, "backup")))                                                            # 이전 검토 파일 보관
        # 실제 DB 회사로 끝까지(예외 없음, 기록)
        conn = db.connect()
        r = nodeview.company(conn, "213500", config.periods(2026))
        self.assertTrue(any(x.get("node_id") == "paper_print" and x["verdict"] in ("비우호", "약한 비우호") for x in r["segments"]))
        self.assertTrue(any(x.get("excluded") for x in r["segments"]))     # 용역부문 등(1.3%) 판정 제외
        conn.close()

    def test_profile_definition_and_roles(self):
        from analyzer import profile, nodeview
        page = ('<li class="dot_cmp" data-cd="1">동사는 2016년 A사의 드라마 사업본부가 물적분할되어 설립된 방송영상물 제작 전문기업으로, 2017년 상장함.</li>'
                '<li class="dot_cmp" data-cd="1">드라마 콘텐츠를 기획·제작하여 방송국, 미디어 플랫폼, 해외시장에 공급하고 있음.</li> WICS : 방송과엔터테인먼트 &nbsp;')
        w = profile.parse_wr(page)
        self.assertEqual((len(w["lines"]), w["wics"]), (2, "방송과엔터테인먼트"))
        K = nodeview.knowledge()
        prof = {"wr_lines": w["lines"]}
        self.assertEqual(nodeview.definition(prof), "방송영상물 제작 전문기업")
        found = {nid: biz for nid, sc, kw, biz in nodeview.profile_nodes(prof, K)}
        self.assertNotIn("broadcasting", found)                 # '방송영상물' 안의 '방송'은 세지 않음
        self.assertFalse(found.get("platform", False))           # '플랫폼에 공급'은 고객이지 사업이 아님
        self.assertTrue(found.get("drama"))
        self.assertEqual(nodeview.definition({"wr_lines": ["동사는 1965년 A로 설립되었고, 2015년 B에서 인적분할하여 신설된 국내 최대 지류 제조회사임."]}),
                         "국내 최대 지류 제조회사")
        xml = ('<SECTION-1><TITLE>II. 사업의 내용</TITLE><SECTION-2><TITLE>1. 사업의 개요</TITLE><P>당사는 보툴리눔 톡신을 주력으로 합니다.</P></SECTION-2>'
               '<SECTION-2><TITLE>2. 주요 제품 및 서비스</TITLE><TABLE><TR><TH>사업부문</TH><TH>품목</TH><TH>구체적용도</TH><TH>매출액</TH><TH>비율</TH></TR>'
               '<TR><TD ROWSPAN="2">바이오</TD><TD>메디톡신</TD><TD>보툴리눔 톡신 제제</TD><TE>120</TE><TE>60.0</TE></TR>'
               '<TR><TD>뉴라미스</TD><TD>필러</TD><TE>40</TE><TE>20.0</TE></TR><TR><TD>기타</TD><TD>기타</TD><TD>용역</TD><TE>40</TE><TE>20.0</TE></TR>'
               '<TR><TD>합계</TD><TD></TD><TD></TD><TE>200</TE><TE>100.0</TE></TR></TABLE></SECTION-2></SECTION-1><SECTION-1><TITLE>III. 재무</TITLE></SECTION-1>')
        b = profile.parse_business(xml)
        self.assertIn("보툴리눔", b["overview"])
        self.assertEqual([(x["name"], x["desc"], x["pct"]) for x in b["products"]],
                         [("메디톡신", "보툴리눔 톡신 제제", 60.0), ("뉴라미스", "필러", 20.0), ("기타", "용역", 20.0)])
        self.assertEqual(profile.parse_business("<SECTION-1><TITLE>I. 회사</TITLE></SECTION-1>")["products"], [])
        # 막연한 사업부 이름은 기업개요의 주력 낱말로 연결(업종코드보다 먼저)
        self.assertEqual(nodeview.link_segment("제품(수출)", "211", K, {}, {}, ("botulinum", 3, "보툴리눔"))[0], "botulinum")

    def test_nodeview_freshness_levels(self):
        import datetime as _d
        from analyzer import nodeview
        t = _d.date(2026, 11, 18)
        self.assertEqual(nodeview._age("2026-11-04", t), 14)
        self.assertEqual(nodeview._age("2026-06", t), (t - _d.date(2026, 6, 30)).days)     # 'YYYY-MM' = 그달 말일
        conn = db.connect()
        P = config.periods(2026)
        K = nodeview.knowledge()
        res = nodeview.company(conn, "213500", P, K)
        lv = lambda today: {f["key"]: f["level"] for f in nodeview.freshness(conn, "213500", P, res, K, today)}
        now = lv(_d.date(2026, 11, 18))
        self.assertEqual(now["consensus"], "ok")
        self.assertEqual(lv(_d.date(2026, 11, 25))["consensus"], "warn")           # 21일: 2주 넘음
        self.assertEqual(lv(_d.date(2026, 12, 20))["consensus"], "old")            # 46일: 한 달 넘음
        self.assertEqual(now["annual"], "ok")                                      # 2025년 실적 있음
        P2 = config.periods(2027)
        self.assertEqual({f["key"]: f["level"] for f in nodeview.freshness(conn, "213500", P2, res, K, _d.date(2027, 4, 2))}["annual"], "warn")
        conn.close()

    def test_nodeview_catalog(self):
        from analyzer import nodeview
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "t.db"))
            db.ensure_company(conn, "213500", "한솔제지")
            nodeview.ensure(conn)
            for day, v in (("2026-11-17", "중립"), ("2026-11-18", "비우호")):      # 회사별 마지막 날 기록만
                conn.execute("INSERT INTO node_verdict_log(day, stock_code, segment, node_id, verdict, score) VALUES (?,?,?,?,?,?)",
                             (day, "213500", "인쇄용지, 특수지", "paper_print", v, -3.0))
            conn.commit()
            cat = nodeview.catalog(conn)
            n = next(x for x in cat["nodes"] if x["id"] == "paper_print")
            self.assertEqual([(x["name"], x["verdict"]) for x in n["links"]], [("한솔제지", "비우호")])
            self.assertEqual(n["score"], -2.0)
            self.assertEqual(n["themes"][0]["id"], "T_DIGITAL")
            t = next(x for x in cat["themes"] if x["id"] == "T_DIGITAL")
            self.assertGreaterEqual(t["n_neg"], 1)
            self.assertEqual((cat["log_day"], cat["n_companies"]), ("2026-11-18", 1))
            conn.close()

    def test_kiwoom_cli_readonly(self):
        import sys
        from unittest import mock
        from analyzer import kiwoom_store
        from analyzer.collectors import kiwoom
        self.assertEqual((kiwoom.num("+9810"), kiwoom.num("-6220", True), kiwoom.num("1,480"), kiwoom.num("")), (9810.0, 6220.0, 1480.0, None))
        for bad in (["domestic", "orders", "buy"], ["domestic", "accounts", "balance"], ["domestic", "stocks", "info", "--confirm"]):
            with self.assertRaises(kiwoom.KiwoomUnavailable):              # 조회 외 명령은 막음
                kiwoom._call(bad)
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "kiwoomcli")
            with open(fake, "w", encoding="utf-8") as f:
                f.write("#!" + sys.executable + "\nimport sys, json\na = sys.argv[1:]\n"
                        "if a[:3] == ['domestic','stocks','info']: print(json.dumps({'상장주식':'23801','현재가':'-6220','외인소진률':'+5.73','시가총액':'1480','return_code':0}))\n"
                        "elif a[:3] == ['domestic','candles','daily']: print(json.dumps({'stk_dt_pole_chart_qry':[{'dt':'20261119','cur_prc':'6220'},{'dt':'20251230','cur_prc':'8310'}],'return_code':0}))\n"
                        "elif a[:3] == ['domestic','candles','by-stock']: print(json.dumps({'종목별투자자기관별차트':[{'일자':'20261119','외국인투자자':'-120','기관계':'+50','개인투자자':'+70'}],'return_code':0}))\n"
                        "else:\n    print('단말기 인증에 실패했습니다 (8050): IP가 등록되지 않았습니다', file=sys.stderr); sys.exit(1)\n")
            os.chmod(fake, 0o755)
            with mock.patch.dict(os.environ, {"KIWOOM_CLI": fake}):
                info = kiwoom.stock_info("213500")
                self.assertEqual((info["shares_listed"], info["price"], info["foreign_ratio"]), (23801000.0, 6220.0, 5.73))
                self.assertEqual(kiwoom.daily("213500", "20261119")["rows"][1], ("20251230", 8310.0, None))
                self.assertEqual(kiwoom.flows("213500", "20261119")[0]["foreign"], -120.0)
                with self.assertRaises(kiwoom.KiwoomUnavailable) as cm:
                    kiwoom._call(["doctor"])
                self.assertIn("API 사용신청", str(cm.exception))                # 8050이면 IP 등록 안내
                conn = db.connect(os.path.join(d, "t.db"))
                kiwoom_store.collect(conn, "213500")
                sm = kiwoom_store.summary(conn, "213500")
                self.assertEqual(sm["flows"]["d20"]["foreign_net"], -1.2)       # 백만 원 → 억 원
                self.assertEqual(sm["flows"]["label"], "외국인 순매도 · 기관 순매수")
                conn.close()

    def test_hanjang_node4y(self):
        import io, zipfile
        from unittest import mock
        from analyzer import hanjang, hj_learn
        self.assertEqual(hanjang.map_judgment("<span class='tag t-good'>순풍(약)</span>")[0], "우호")
        self.assertEqual(hanjang.map_judgment("<span class='tag t-mid'>씨앗</span>")[0], "씨앗")
        self.assertEqual(hanjang.map_judgment("<span class='tag t-warn'>걸림(확인형)</span>")[0], "걸림")
        self.assertEqual(hanjang.map_judgment("<span class='tag t-bad'>무엇</span>")[0], "역풍")
        self.assertEqual(hanjang.rep_judgment({"2026": {"judgment": "우호"}, "2027": {"judgment": "중립"}, "2028": {"judgment": "중립"}, "2029": {"judgment": "우호"}}), "우호")  # 같으면 가까운 해
        self.assertEqual(hanjang.guess_kind("④ 사람·지배구조"), "사람")
        self.assertEqual(hanjang.guess_kind("④ 원가·환율"), "원료")
        seed = {"name": "가짜", "asof": "2026-10-30", "nodes_verdict": "<b>방향은 맞음</b>",
                "nodes": [["① 메모리 특수가스", "<span class='tag t-good'>우호</span>", "수요 증가", "몫", "2026 1Q 매출 882억", "감산"],
                          ["② 신사업", "<span class='tag t-mid'>씨앗</span>", "a", "b", "c", "d"],
                          ["③ 사람·지배구조", "<span class='tag t-warn'>걸림</span>", "—", "횡령", "미확정", "확정 공시"]]}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(config, "DATA_DIR", d):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("한장조사/123456.json", json.dumps(seed, ensure_ascii=False))
                z.writestr("한장조사/_조사지침.md", "지침")
                z.writestr("../evil.json", "{}")
            conn = db.connect(os.path.join(d, "t.db"))
            rep = hanjang.import_zip(conn, buf.getvalue())
            self.assertEqual(rep["companies"], 1)
            c = hanjang.load(conn, "123456", "claude")
            self.assertEqual([n["rep"] for n in c["nodes"]], ["우호", "씨앗", "걸림"])
            self.assertEqual(c["verdict"], "방향은 맞음")
            # Claude 결과 넣기: 판정 말 정리·원천 1·2029 단계 상한·가격 낱말 경고
            docs, notes = hanjang.validate_claude({"code": "123456", "asof": "2026-11-19", "nodes": [
                {"name": "가스", "kind": "사업부", "demand": "x", "share": "y", "numbers": "z", "triggers": "w",
                 "years": {"2026": {"judgment": "우호", "stage": "숫자", "basis": "실적"}, "2027": {"judgment": "순풍", "stage": "확정", "basis": "목표가 상향"},
                           "2028": {"judgment": "중립", "stage": "확정", "basis": "b"}, "2029": {"judgment": "중립", "stage": "숫자", "basis": "c"}},
                 "evidence": [{"year": 2026, "date": "2026-08-07", "type": "공시", "stage": "숫자", "dir": "+", "title": "잠정실적"}]}], "verdict": "v"})
            self.assertEqual(docs[0]["nodes"][0]["years"]["2027"]["judgment"], "우호")
            warns = hanjang.save(conn, docs[0], "claude", "시험")
            self.assertTrue(any(w.startswith("1.") for w in warns))           # 노드 1개(3~6 권장)
            self.assertTrue(any(w.startswith("6.") for w in warns))           # 우호인데 원천 1
            self.assertTrue(any(w.startswith("7.") for w in warns))           # 2029 단계 상한
            self.assertTrue(any(w.startswith("9.") for w in warns))           # 근거에 목표가
            self.assertEqual(hanjang.load(conn, "123456", "claude")["company"]["version"], 2)
            # 학습 모델: 낱말로 판정을 익힘
            rows = [("우호", "증설 수요 증가 확대", "A"), ("우호", "수요 증가 램프업", "B"), ("우호", "확대 증가 수주", "C"),
                    ("역풍", "수요 감소 감산 부진", "A"), ("역풍", "가격 하락 감소", "B"), ("역풍", "부진 적자 감소", "C"),
                    ("걸림", "횡령 배임 소송", "A"), ("걸림", "전환사채 희석 증자", "B"), ("걸림", "소송 분쟁 최대주주", "C")]
            m = hj_learn.train_rows(rows)
            for t, want in (("증설로 수요 증가", "우호"), ("감산과 부진", "역풍"), ("횡령 혐의 소송", "걸림")):
                p, _ = hj_learn.predict(m, t)
                self.assertEqual(max(p, key=p.get), want, t)
            conn.close()

    def test_hanjang_folder_and_fresh(self):
        import datetime as dtm
        from unittest import mock
        from analyzer import hanjang, hj_learn
        old = {"name": "옛", "asof": "2026-10-30", "nodes": [["① 가스", "<span class='tag t-good'>우호</span>", "a", "b", "c", "d"]]}
        new = {"asof": "2026-11-18", "nodes": [{"name": "칩", "kind": "사업부", "years": {"2026": {"judgment": "우호", "stage": "숫자", "basis": "x"},
                                                                                       "2027": {"judgment": "역풍", "stage": "확정", "basis": "y"}}}]}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(config, "DATA_DIR", d), mock.patch.object(hj_learn, "train", return_value={}):
            f = os.path.join(d, "작업", "한장조사")
            os.makedirs(f)
            for name, obj in (("111111.json", old), ("222222_노드4년.json", new), ("설정.json", {"a": 1})):
                with open(os.path.join(f, name), "w", encoding="utf-8") as fh:
                    json.dump(obj, fh, ensure_ascii=False)
            with open(os.path.join(f, "333333.json"), "w", encoding="utf-8") as fh:
                fh.write("{깨진")
            conn = db.connect(os.path.join(d, "t.db"))
            self.assertIn("error", hanjang.sync_folder(conn))                    # 폴더 미지정
            with self.assertRaises(ValueError):
                hanjang.folder_set(os.path.join(d, "없는폴더"))
            hanjang.folder_set(os.path.join(d, "작업"))                         # 상위 폴더를 골라도 아래까지 찾음
            rep = hanjang.sync_folder(conn)
            self.assertEqual(rep["files"], 4)
            self.assertEqual(sorted(rep["codes"]), ["111111", "222222"])
            self.assertEqual(len(rep["errors"]), 1)
            y = hanjang.load(conn, "222222", "claude")["nodes"][0]["years"]
            self.assertEqual((y["2026"]["judgment"], y["2027"]["judgment"]), ("우호", "역풍"))
            self.assertEqual(hanjang.sync_folder(conn)["changed"], 0)            # 안 바뀐 파일은 다시 안 읽음
            st = hanjang.folder_status(conn)
            self.assertEqual((st["counts"]["ok"], st["counts"]["err"], st["counts"]["skip"]), (2, 1, 1))
            # 신선도: 평소 30일·실적 시즌 14일 노랑, 90일·보고서 기한 지나면 빨강
            F = lambda asof, today: hanjang.claude_fresh(conn, "999999", asof, "", dtm.date.fromisoformat(today))
            self.assertEqual(F("2026-12-01", "2026-12-20")["state"], "ok")
            self.assertEqual(F("2026-11-20", "2026-12-25")["state"], "warn")
            self.assertEqual(F("2027-01-20", "2027-02-10")["state"], "warn")      # 시즌: 21일 > 14일
            self.assertEqual(F("2026-10-30", "2026-11-19")["state"], "old")       # 3분기 보고서 기한(11/14) 넘김
            self.assertEqual(F("2026-06-01", "2026-06-20")["state"], "ok")
            conn.execute("INSERT INTO disclosures(rcept_no, stock_code, rcept_dt, report_nm, reprt_code) VALUES ('1','999999','20260601','반기보고서','11012')")
            self.assertEqual(F("2026-10-30", "2026-11-19")["state"], "ok")       # 공시 자료가 있으면 달력 대신 실제 공시로
            conn.execute("INSERT INTO events(rcept_no, stock_code, rcept_dt, title) VALUES ('2','999999','20261110','영업(잠정)실적(공정공시)')")
            self.assertEqual(F("2026-10-30", "2026-11-19")["state"], "old")
            ov = hanjang.fresh_overview(conn, dtm.date(2026, 11, 19))
            self.assertEqual(sum(ov["counts"].values()), 2)
            # 요청 파일: 70곳씩 · 중복·잘못된 코드 제외 · 형식 지시 없음 · 한장조사 폴더/_앱요청에 새 파일만(기존 파일 그대로)
            before = {n: open(os.path.join(f, n), "rb").read() for n in os.listdir(f)}
            P = config.periods(2026)
            txt, used = hanjang.request_md(conn, ["111111", "111111", "BAD"] + [f"{i:06d}" for i in range(200000, 200100)], P, "refresh")
            self.assertEqual(len(used), 101)
            self.assertEqual(used[0], "111111")
            self.assertIn("기존 조사 지침과 기존 한장조사 파일 모양 그대로", txt)
            self.assertIn("_앱요청` 폴더 안에는 저장하지 마세요", txt)
            self.assertNotIn("years", txt)
            codes = [f"{i:06d}" for i in range(300000, 300150)]
            rep = hanjang.save_requests(conn, codes, P, "new")
            self.assertEqual(rep["dir"], os.path.join(f, "_앱요청"))               # 한장조사 파일이 있는 폴더 안
            self.assertEqual([n for _, n in rep["files"]], [150])                 # 한 파일 — 나누기는 Claude가 스스로
            body = open(os.path.join(rep["dir"], rep["files"][0][0]), encoding="utf-8").read()
            self.assertIn("스스로 확실히 할 수 있는 양만큼 묶음으로 나눠", body)
            self.assertIn("이미 `{종목코드}.json`이 있는 회사는 건너뛰고", body)
            self.assertIn("150. 300149 (300149)", body)
            rep2 = hanjang.save_requests(conn, codes[:5], P, "new")                # 같은 날 다시 → 덮어쓰지 않고 (2)
            self.assertNotEqual(rep2["files"][0][0], rep["files"][0][0])
            self.assertEqual(len(os.listdir(rep["dir"])), 2)
            self.assertEqual({n: open(os.path.join(f, n), "rb").read() for n in os.listdir(f) if n != "_앱요청"}, before)
            self.assertEqual(hanjang.sync_folder(conn)["changed"], 0)              # 요청 파일은 읽어 들이지 않음
            pl = hanjang.payload(conn, "222222", P)
            self.assertIn("1", {str(k) for k in (pl["facts"] or {}).get("nodes", {})})
            self.assertEqual(pl["db_not_in_claude"], [])
            conn.close()

    def test_naver_401_falls_back_to_google(self):
        import datetime as dt, io, urllib.error
        from unittest import mock
        from analyzer.collectors import news
        news._NAVER_BAD.clear()
        calls = []
        def fake_open(req, timeout=30):
            calls.append(req.full_url)
            if "openapi.naver.com" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                             io.BytesIO('{"errorMessage":"Authentication failed","errorCode":"024"}'.encode()))
            body = "<rss><channel><item><title>가짜 뉴스 - 언론</title><link>http://x</link><pubDate>Tue, 10 Nov 2026 00:00:00 GMT</pubDate><source>언론</source></item></channel></rss>"
            return mock.MagicMock(__enter__=lambda s: mock.MagicMock(read=lambda: body.encode()), __exit__=lambda *a: False)
        with mock.patch.dict(os.environ, {"NAVER_CLIENT_ID": "id", "NAVER_CLIENT_SECRET": "sec"}), \
                mock.patch("urllib.request.urlopen", fake_open):
            with self.assertRaises(news.NaverAuthError) as cm:
                news.naver("삼성전자", dt.date(2026, 11, 1))
            self.assertIn("401", str(cm.exception))
            self.assertIn("024", str(cm.exception))
            self.assertNotIn("sec", str(cm.exception))                    # 키 값은 문구에 없음
            self.assertIsNone(news.naver_key())                           # 거절된 키는 이번 실행에서 다시 안 씀
            src, n, items = news.fetch("삼성전자", dt.date(2026, 11, 1))
            self.assertTrue(src.startswith("구글 뉴스 RSS"))
            self.assertIn("네이버 키 거절됨", src)
            self.assertEqual(sum("openapi.naver.com" in c for c in calls), 1)  # 네이버는 한 번만 시도
        news._NAVER_BAD.clear()

    def test_refetch_forces_only_chosen_sources(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "t.db"))
            db.log_fetch(conn, "news", "123456", "123456", True, 5)
            db.log_fetch(conn, "consensus", "123456", "123456", True, 3)
            self.assertTrue(db.fetched_recently(conn, "news", "123456", 3, ok_only=True))
            seen = {}
            def fake_analyze(code, base_year=None, conn=None, **kw):
                seen["news"] = db.fetched_recently(conn, "news", code, 3, ok_only=True)
                seen["consensus"] = db.fetched_recently(conn, "consensus", code, 7, ok_only=True)
                db.log_fetch(conn, "news", code, code, True, 9)
            with mock.patch.object(pipeline, "analyze", fake_analyze):
                res = pipeline.refetch("123456", ["news"], conn=conn)
            self.assertEqual(seen, {"news": False, "consensus": True})          # 고른 출처만 기간 무시
            self.assertEqual(res, {"news": {"ok": True, "n": 9, "note": ""}})
            self.assertEqual(db.FORCE, set())                                   # 끝나면 원래대로
            self.assertTrue(db.fetched_recently(conn, "news", "123456", 3, ok_only=True))
            with self.assertRaises(ValueError):
                pipeline.refetch("123456", ["parse"], conn=conn)
            conn.close()

    def test_lan_password_and_network_rules(self):
        from analyzer import lan
        old = os.environ.get("APPDATA")
        os.environ["APPDATA"] = tempfile.mkdtemp()
        try:
            self.assertFalse(lan.enabled())
            for weak in ("12345", "12345678", "abcdefgh", "aaaaaaa1", "password1"):
                with self.assertRaises(ValueError):
                    lan.enable(weak)                       # 8자 미만·한 종류·너무 쉬움
            lan.enable("tablet123")
            self.assertTrue(lan.enabled())
            txt = open(lan.path(), encoding="utf-8").read()
            self.assertNotIn("tablet123", txt)             # 원문은 저장하지 않음
            self.assertTrue(lan.check_password("tablet123"))
            self.assertFalse(lan.check_password("tablet124"))
            tok = lan.login("192.168.0.20", "tablet123")
            self.assertTrue(lan.valid(tok))
            lan.logout(tok)
            self.assertFalse(lan.valid(tok))
            lan.disable()
            self.assertFalse(lan.enabled())
        finally:
            if old is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old
        for ip in ("192.168.0.20", "10.0.0.5", "172.20.1.1", "fe80::1"):
            self.assertTrue(lan.is_home_network(ip), ip)
        for ip in ("8.8.8.8", "211.234.1.1", "2001:4860::8888"):
            self.assertFalse(lan.is_home_network(ip), ip)   # 인터넷 쪽 주소는 비밀번호가 맞아도 받지 않음
        self.assertTrue(lan.is_local("127.0.0.1"))
        self.assertEqual(lan.cookie_token("a=1; __Host-ca_session=xyz; b=2"), "xyz")
        self.assertFalse(lan.allow_write())

    def test_lan_global_lockout(self):
        from analyzer import lan
        lan._global_fails[:] = []
        import time as _t
        lan._global_fails.extend([_t.time()] * lan.GLOBAL_MAX_FAIL)
        self.assertEqual(lan.locked_out("192.168.0.99"), "모든 기기")     # 기기를 바꿔 가며 시도해도 막힘
        lan._global_fails[:] = []
        self.assertIsNone(lan.locked_out("192.168.0.99"))

    def test_tls_cert_is_valid(self):
        import ssl
        from analyzer import tlscert
        old = os.environ.get("APPDATA")
        os.environ["APPDATA"] = tempfile.mkdtemp()
        try:
            cp, kp, new = tlscert.ensure(["192.168.0.10"])
            self.assertTrue(new)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cp, kp)                    # 키와 인증서가 짝이 맞고 형식이 올바름
            der = ssl.PEM_cert_to_DER_cert(open(cp).read())
            self.assertIn(bytes([0x03, 0x02, 0x02, 0xA4]), der)   # 키 용도 BIT STRING: 남는 비트 2(엄격한 브라우저 규격)
            self.assertIn(bytes([0x87, 0x04, 192, 168, 0, 10]), der)  # 주소(SAN)에 PC IP
            self.assertEqual(len(tlscert.fingerprint().split(":")), 32)
            self.assertFalse(tlscert.ensure(["192.168.0.10"])[2])     # 있으면 다시 만들지 않음
        finally:
            if old is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old

    def test_standards_tax_factor_netdebt(self):
        from analyzer import standards
        self.assertAlmostEqual(standards.statutory_marginal(2025, 5000e8), 0.264)    # 24% × 1.1
        self.assertAlmostEqual(standards.statutory_marginal(2026, 5000e8), 0.275)    # 25% × 1.1 (2026년 1%p 환원)
        self.assertAlmostEqual(standards.statutory_marginal(2025, 100e8), 0.209)     # 19% × 1.1
        self.assertAlmostEqual(standards.statutory_marginal(2020, 1e8), 0.11)        # 10% × 1.1
        t, _ = standards.marginal_tax(2025, 5e8, [500e8, 600e8, -10e8, 400e8])        # 일시적으로 작은 해 → 정상 이익(중앙값 450억) 구간
        self.assertAlmostEqual(t, 0.231)
        self.assertEqual(standards.cons_factor({"ch": {3: -6.0}, "tag": {3: None}}, "op_income")[0], 0.73)
        self.assertEqual(standards.cons_factor({"ch": {3: -4.9}, "tag": {3: None}}, "eps")[0], 0.83)
        self.assertEqual(standards.cons_factor(None, "revenue")[0], 1.0)
        vals = {(2025, "acc_debt"): 100.0, (2025, "acc_cash"): 30.0, (2025, "acc_cash_only"): 30.0, (2025, "acc_lease_face"): 0.0}
        get = lambda y, k, per="FY": vals.get((y, k))
        nd = standards.net_debt(get, 2025, "FY", {"lease": 20.0, "stfin": 5.0})
        self.assertEqual((nd["debt"], nd["cash"], nd["nd"]), (120.0, 35.0, 85.0))   # 리스(주석) 더하고 단기금융(주석) 빼기
        sh = standards.shares(lambda y, k, per="FY": {"shares_common": 1000, "shares_pref": 10, "treasury_common": 50}.get(k), 2025)
        self.assertEqual(sh[0], 960)

    def test_same_dcf_in_valuation_and_value5(self):
        """같은 이름이면 같은 식: 화면 G의 DCF와 화면 O의 ① DCF가 실적 연도에 같은 값"""
        from analyzer import valuation, value5
        pipeline.analyze("253450", 2026, offline=True, conn=self.conn)
        P = config.periods(2026)
        vr = valuation.run(self.conn, "253450", P)["rows"]
        G = {r["year"]: r for r in vr}
        res = value5.compute(value5.inputs(self.conn, "253450", P, vr))["rows"]
        n = 0
        for y, o in res.items():
            g = G.get(y)
            if g and g["kind"] == "실적" and g["values"].get("dcf") is not None:
                self.assertAlmostEqual(g["values"]["dcf"], o["dcf"], places=3, msg=str(y))
                n += 1
        self.assertGreater(n, 3)


if __name__ == "__main__":
    unittest.main()
