"""수집 결과(out/*.json) → 엑셀. 사용: python build_xlsx.py OUT_DIR XLSX_PATH"""
import json, os, re, sys
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

OUT, XLSX = sys.argv[1], sys.argv[2]
L = lambda n: json.load(open(os.path.join(OUT, n), encoding="utf-8"))
corps, cons, snap = L("corps.json"), L("cons.json"), L("snap.json")
MIN_INST = 3


def num(x):
    if x in (None, "", "-"):
        return None
    try:
        return float(str(x).replace(",", ""))
    except ValueError:
        return None


def exclude_reason(c):
    nm, code = c["name"], c["code"]
    if c["mkt"] not in ("코스피", "코스닥"):
        return "코스피·코스닥 아님"
    if "스팩" in nm or "기업인수목적" in nm or re.search(r"SPAC", nm, re.I):
        return "스팩"
    if "리츠" in nm or re.search(r"REIT", nm, re.I):
        return "리츠"
    if code[-1] != "0" or re.search(r"(\d*우[A-C]?|\(전환\))$", nm):
        return "우선주"
    return None


res, exc = [], []
for code, c in sorted(corps.items(), key=lambda kv: kv[1]["name"]):
    r = exclude_reason(c)
    if r:
        exc.append([code, c["name"], c["mkt"], r, None])
        continue
    cr = cons.get(code)
    if cr is None:
        exc.append([code, c["name"], c["mkt"], "수집 안 됨", None]); continue
    if "error" in cr:
        why = "FnGuide 기업정보 없음 (신규상장 등)" if "Expecting value" in cr["error"] else "수집 오류"
        exc.append([code, c["name"], c["mkt"], why, None]); continue
    if not cr.get("consol"):
        miss = [k for k, lab in (("sales", "매출액"), ("op", "영업이익"), ("eps", "EPS"))
                if num(cr.get(f"{k}_2028E")) is None]
        has_any = any(num(v) is not None for k, v in cr.items() if k.endswith("E"))
        why = "2028E 없음 (" + ",".join({"sales": "매출액", "op": "영업이익", "eps": "EPS"}[m] for m in miss) + ")" \
            if has_any else "컨센서스 없음"
        exc.append([code, c["name"], c["mkt"], why, None]); continue
    s = snap.get(code, {})
    n = num(s.get("n_inst"))
    if n is None:
        exc.append([code, c["name"], c["mkt"], "추정기관수 표시 없음 (투자의견 미제시)", None]); continue
    if n < MIN_INST:
        exc.append([code, c["name"], c["mkt"], f"추정기관 {MIN_INST}곳 미만", n]); continue
    row = [code, c["name"], c["mkt"], "연결" if cr["consol"] == "C" else "별도", int(n),
           num(s.get("opinion")), num(s.get("target"))]
    for k in ("sales", "op", "eps"):
        row += [num(cr.get(f"{k}_{y}E")) for y in (2026, 2027, 2028)]
    res.append(row)

wb = Workbook()
hdr_fill = PatternFill("solid", fgColor="1F4E78")
hdr_font = Font(bold=True, color="FFFFFF")


def sheet(ws, headers, rows, fmts):
    ws.append(headers)
    for r in rows:
        ws.append(r)
    for i, h in enumerate(headers, 1):
        cell = ws.cell(1, i)
        cell.fill, cell.font = hdr_fill, hdr_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = max(10, min(28, len(h) * 1.8 + 2))
        if fmts.get(i):
            for (c,) in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                c.number_format = fmts[i]
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    ws.row_dimensions[1].height = 32


ws = wb.active
ws.title = "결과"
H = ["종목코드", "종목명", "시장", "재무기준", "추정기관수", "투자의견(1~5)", "목표주가(원)"]
for lab in ("매출액(억원)", "영업이익(억원)", "EPS(원)"):
    H += [f"{lab} {y}E" for y in (2026, 2027, 2028)]
F = {6: "0.00", 7: "#,##0"}
F.update({i: "#,##0" for i in range(8, 17)})
sheet(ws, H, res, F)
ws.column_dimensions["B"].width = 20

ws2 = wb.create_sheet("제외")
sheet(ws2, ["종목코드", "종목명", "시장", "제외 사유", "추정기관수"], exc, {})
ws2.column_dimensions["D"].width = 30

ws3 = wb.create_sheet("기준")
from collections import Counter
cnt = Counter(e[3].split(" (")[0] for e in exc)
info = [["항목", "내용"],
        ["자료 출처", "FnGuide Company Guide (wcomp.fnguide.com) 공개 화면"],
        ["수집일", "2026-11-03"],
        ["범위", "코스피·코스닥 (리츠·스팩·우선주 제외)"],
        ["조건 1", "2028/12(E) 매출액·영업이익·EPS 컨센서스가 모두 있음 (연결 우선, 없으면 별도)"],
        ["조건 2", f"추정기관수 {MIN_INST}곳 이상"],
        ["주의", "추정기관수는 FnGuide 스냅샷의 값(FY1 기준 투자의견·EPS 추정 기관 수)이며, 2028E 연도별 기관 수가 아님"],
        ["주의", "컨센서스 = 최근 3개월 증권사 추정치 평균 (FnGuide 기준)"],
        ["결과 종목 수", len(res)]] + [[f"제외: {k}", v] for k, v in cnt.most_common()]
for r in info:
    ws3.append(r)
ws3.column_dimensions["A"].width = 18
ws3.column_dimensions["B"].width = 90
for c in ws3[1]:
    c.fill, c.font = hdr_fill, hdr_font
wb.save(XLSX)
print("결과", len(res), "제외", len(exc), dict(cnt))
