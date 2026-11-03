# 상장기업 컨센서스 (2028E) 추출

- 출처: FnGuide Company Guide(wcomp.fnguide.com) 공개 화면, 수집일 2026-11-03
- 조건: 코스피·코스닥(리츠·스팩·우선주 제외), 2028/12(E) 매출액·영업이익·EPS 모두 존재, 추정기관수 3곳 이상
- 주의: 추정기관수는 스냅샷 화면 값(FY1 기준)이며 연도별 기관 수가 아님

다시 만들기:
```
python3 collect.py ./out
python3 build_xlsx.py ./out 컨센서스_2028E_상장기업.xlsx
```
