"""FnGuide Snapshot 화면 구조를 흉내 낸 '시험용 가상 화면'을 만든다.
실적 열(2023~2025)은 한솔제지 DART 값(억원 반올림)과 같게, 전망 열(E)은 시험용 가상값이다. 실제 컨센서스가 아니다."""
import gzip, sys
rows = [("매출액", [21941, 22158, 22900, 23500, 24600, 25700]), ("영업이익", [472, 220, 499, 1000, 1100, 1200]),
        ("영업이익(발표기준)", [472, 220, 499, None, None, None]), ("지배주주순이익", [18, -304, 39, 260, 300, 340]),
        ("자본총계", [7241, 6873, 6976, 7100, 7300, 7500]), ("지배주주지분", [7239, 6870, 6972, 7095, 7295, 7495]),
        ("부채총계", [13503, 13296, 12961, 13000, 12900, 12800]), ("부채비율", [186.48, 193.45, 185.78, 183.1, 176.7, 170.7]),
        ("영업이익률", [2.15, 0.99, 2.18, 4.26, 4.47, 4.67]), ("ROE", [0.24, -4.31, 0.56, 3.70, 4.17, 4.61]),
        ("EPS(원)", [74, -1278, 163, 1090, 1260, 1430]), ("BPS(원)", [30422, 28872, 29322, 29800, 30600, 31500]),
        ("발행주식수", [23801, 23801, 23801, None, None, None])]
cols = ["2023/12", "2024/12", "2025/12", "2026/12(E)", "2027/12(E)", "2028/12(E)"]
def td(v): return f"<td class='r'>{'' if v is None else f'{v:,}'}</td>"
body = "".join(f"<tr><th scope='row'><div><dl class='txt_acd'><dt>{n}</dt><dd>설명</dd></dl></div></th>{''.join(td(v) for v in vs)}</tr>" for n, vs in rows)
html = f"""<html><body>
<div class="corp_group2"><table><tr><th>종가/ 전일대비</th><td>6,610/ -80</td></tr><tr><th>시가총액(보통주,억원)</th><td>1,573</td></tr>
<tr><th>발행주식수(보통주/ 우선주)</th><td>23,800,576/ 0</td></tr></table><span>[기준:2026.11.03]</span></div>
<div class="um_table"><table><tr><th>투자의견</th><th>목표주가</th><th>EPS</th><th>PER</th><th>추정기관수</th></tr><tr><td>4.00</td><td>10,250</td><td>1,090</td><td>6.06</td><td>3</td></tr></table></div>
<div id="highlight_D_Y" class="um_table"><p>단위 : 억원, %, 배, 천주</p><table><thead><tr><th>IFRS(연결)</th><th colspan="3">Annual</th></tr>
<tr><th></th>{''.join(f'<th>{c}</th>' for c in cols)}</tr></thead><tbody>{body}</tbody></table></div></body></html>"""
gzip.open(sys.argv[1], "wt", encoding="utf-8").write(html)
