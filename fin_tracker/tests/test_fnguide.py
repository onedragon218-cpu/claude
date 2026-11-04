import os, sys, tempfile, unittest, subprocess
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from fintrack.sources import fnguide


class T(unittest.TestCase):
    def test_parse_fixture(self):
        p = os.path.join(tempfile.mkdtemp(), "213500_SVD_Main_20261104.html.gz")
        subprocess.check_call([sys.executable, os.path.join(HERE, "fixtures", "make_fnguide_fixture.py"), p])
        f = fnguide.parse(p)
        self.assertNotIn("error", f)
        self.assertEqual(f["basis"], "CFS")
        self.assertEqual(f["as_of"], "2026-11-03")
        self.assertEqual(f["years"][2027]["kind"], "E")
        self.assertEqual(f["years"][2025]["kind"], "A")
        self.assertEqual(f["years"][2028]["revenue"], 25700)
        self.assertEqual(f["years"][2025]["op"], 499)          # '영업이익(발표기준)' 행이 덮어쓰지 않음
        self.assertEqual(f["years"][2024]["eps"], -1278)
        self.assertEqual(f["years"][2025]["shares"], 23801000)  # 천주 → 주
        self.assertEqual(f["market"]["market_cap_eok"], 1573)
        self.assertEqual(f["market"]["price"], 6610)
        self.assertEqual(f["n_estimates"], 3)


if __name__ == "__main__":
    unittest.main()
