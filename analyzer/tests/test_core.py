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
            self.assertGreaterEqual(r["tax"], valuation.load_assumptions()["대체세율"])
            self.assertTrue(any("예상(보수적 추정)" in n for n in r["notes"]))

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
