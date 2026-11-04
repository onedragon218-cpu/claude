"""재사용·버전 관리·FnGuide 반영 확인. 실행: python -m unittest discover -s tests"""
import os, subprocess, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from fintrack import paths, service, config
from fintrack.db import Store
from fintrack.metrics import build
from fintrack.sources import fnguide


def new_workdir(with_fnguide=False):
    w = paths.W(paths.ensure(tempfile.mkdtemp()))
    service.import_sample(w)
    if with_fnguide:   # 시험용 가상 FnGuide 화면을 '오늘 받은 원본'으로 둔다
        subprocess.check_call([sys.executable, os.path.join(HERE, "fixtures", "make_fnguide_fixture.py"), fnguide.raw_path(w, "213500")])
    return w


class T(unittest.TestCase):
    def test_second_run_fetches_nothing(self):
        w = new_workdir()
        r1 = service.analyze(w, "213500", base_year=2026, offline=True)
        self.assertGreater(r1["summary"]["외부·원본 조회"], 0)
        r2 = service.analyze(w, "213500", base_year=2026, offline=True)
        self.assertEqual(r2["summary"]["신규 저장"], 0)
        s = Store(w.db)
        dart_fetch = s.con.execute("SELECT COUNT(*) FROM fetch_log WHERE run_id=? AND source='DART 사업보고서' AND channel!='skipped-reuse'",
                                   (r2["run_id"],)).fetchone()[0]
        self.assertEqual(dart_fetch, 0)

    def test_values_match_dart(self):
        w = new_workdir()
        service.analyze(w, "213500", base_year=2026, offline=True)
        s = Store(w.db)
        m = build(s, "213500", config.periods(2026))
        self.assertAlmostEqual(m["revenue"]["actual"][2025]["v"], 22900.4, places=1)
        self.assertEqual(m["eps"]["actual"][2024]["v"], -1278)
        self.assertNotIn(2027, m["revenue"]["consensus"])          # 전망 출처 없으면 비워 둠
        self.assertTrue(os.path.exists(os.path.join(w.output, "213500.html")))

    def test_fnguide_fills_consensus(self):
        w = new_workdir(with_fnguide=True)
        service.analyze(w, "213500", base_year=2026, offline=True)
        s = Store(w.db)
        m = build(s, "213500", config.periods(2026))
        self.assertAlmostEqual(m["revenue"]["consensus"][2028]["v"], 25700)
        self.assertEqual(m["revenue"]["consensus"][2027]["status"], "VERIFIED")   # 실적 열이 DART와 맞아 읽기 확인됨
        self.assertTrue(m["eps"]["consensus"][2026]["source"].startswith("FnGuide"))
        self.assertIn(2028, m["debt_ratio"]["consensus"])
        self.assertAlmostEqual(m["op_margin"]["consensus"][2027]["v"], 1100 / 24600 * 100, places=6)

    def test_correction_creates_new_version(self):
        w = new_workdir()
        service.analyze(w, "213500", base_year=2026, offline=True)
        s = Store(w.db)
        row = s.current("213500", fiscal_year=2025, item="revenue", kind="ACTUAL")[0]
        self.assertEqual(s.put({**dict(row), "value": row["value"] + 1e8, "source_ref": "정정공시", "status": None}, "정정 공시"), "new_version")
        rows = s.con.execute("SELECT version,is_current,status FROM observations WHERE stock_code='213500' "
                             "AND fiscal_year=2025 AND item='revenue' AND kind='ACTUAL' ORDER BY version").fetchall()
        self.assertEqual([tuple(x) for x in rows], [(1, 0, "VERIFIED"), (2, 1, "CORRECTED")])
        self.assertEqual(s.con.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
