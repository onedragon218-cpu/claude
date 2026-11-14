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
