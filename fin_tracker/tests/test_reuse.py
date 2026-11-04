"""재사용·버전 관리 확인. 실행: python3 -m unittest discover -s tests"""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fintrack import config
from fintrack.db import Store
from fintrack.pipeline import Collector
from fintrack.validate import Validator
from fintrack.metrics import build


def run(store, rid):
    p = config.periods(2026)
    c = Collector(store, "213500", 2026, rid, offline=True)
    c.actuals(); c.partial(); c.prices(); c.consensus()
    store.commit()
    Validator(store, "213500", p, rid).run()
    return c.stats, build(store, "213500", p)


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = Store(os.path.join(self.tmp, "t.sqlite"))

    def test_second_run_fetches_nothing(self):
        st1, _ = run(self.s, "r1")
        self.assertGreater(st1["fetched"], 0)
        st2, _ = run(self.s, "r2")
        self.assertEqual(st2["fetched"], 0)
        self.assertEqual(st2["inserted"], 0)

    def test_values_match_dart(self):
        _, m = run(self.s, "r1")
        self.assertAlmostEqual(m["revenue"]["actual"][2025]["v"], 22900.4, places=1)
        self.assertEqual(m["eps"]["actual"][2024]["v"], -1278)
        self.assertAlmostEqual(m["op_margin"]["actual"][2025]["v"], 49932105257 / 2290040442405 * 100, places=6)
        self.assertNotIn(2027, m["revenue"]["consensus"])          # 검증된 2027 전망 없음 → 비워 둠
        self.assertEqual(m["bps"]["consensus"][2026]["status"], "CONFLICT")

    def test_correction_creates_new_version(self):
        run(self.s, "r1")
        row = self.s.current("213500", fiscal_year=2025, item="revenue", kind="ACTUAL")[0]
        r = self.s.put({**dict(row), "value": row["value"] + 1e8, "source_ref": "정정공시", "status": None}, "정정 공시")
        self.assertEqual(r, "new_version")
        rows = self.s.con.execute("SELECT version,is_current,status FROM observations WHERE stock_code='213500' "
                                  "AND fiscal_year=2025 AND item='revenue' AND kind='ACTUAL' ORDER BY version").fetchall()
        self.assertEqual([tuple(x) for x in rows], [(1, 0, "VERIFIED"), (2, 1, "CORRECTED")])
        self.assertEqual(self.s.con.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
