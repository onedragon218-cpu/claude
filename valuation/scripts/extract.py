"""DART 전체 재무제표 JSON에서 가치평가용 핵심 계정을 뽑는다.
사용: python3 extract.py <회사키> <json파일>...  -> raw/<회사키>_<연도>.json 기록
"""
import json, sys, os, re

IDS = {
  "revenue": ["ifrs-full_Revenue"],
  "op": ["dart_OperatingIncomeLoss"],
  "ni_owner": ["ifrs-full_ProfitLossAttributableToOwnersOfParent"],
  "ni": ["ifrs-full_ProfitLoss"],
  "eq_owner": ["ifrs-full_EquityAttributableToOwnersOfParent"],
  "eq": ["ifrs-full_Equity"],
  "ocf": ["ifrs-full_CashFlowsFromUsedInOperatingActivities"],
  "capex_ppe": ["ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
  "capex_int": ["ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities"],
  "liab": ["ifrs-full_Liabilities"],
  "div_paid": ["ifrs-full_DividendsPaidClassifiedAsFinancingActivities", "ifrs-full_DividendsPaidToEquityHoldersOfParentClassifiedAsFinancingActivities"],
}
SJ = {"revenue":("IS","CIS"),"op":("IS","CIS"),"ni_owner":("IS","CIS"),"ni":("IS","CIS"),
      "eq_owner":("BS",),"liab":("BS",),"eq":("BS",),"ocf":("CF",),"capex_ppe":("CF",),"capex_int":("CF",),"div_paid":("CF",)}

NAMES = {"op":("영업이익","영업이익(손실)"), "capex_ppe":("유형자산의취득",), "capex_int":("무형자산의취득",),
         "div_paid":("배당금의지급","배당금지급","배당금의지급등"), "ni":("당기순이익","당기순이익(손실)")}

def num(s):
    if s in (None, "", "-"): return None
    s = s.replace(",", "")
    try: return float(s)
    except: return None

def main():
    key = sys.argv[1]
    out_dir = os.path.join(os.path.dirname(__file__), "..", "raw")
    for path in sys.argv[2:]:
        d = json.load(open(path))
        lst = d.get("list", [])
        if not lst: print(path, "empty", d.get("message")); continue
        year = int(lst[0]["bsns_year"]); reprt = lst[0]["reprt_code"]
        res = {}
        for col, off in (("thstrm_amount",0),("frmtrm_amount",1),("bfefrmtrm_amount",2)):
            y = year - off; row = {}
            for k, ids in IDS.items():
                for it in lst:
                    if it.get("sj_div") in SJ[k] and it.get("account_id","").replace("ifrs_","ifrs-full_",1) in ids:
                        v = num(it.get(col))
                        if v is not None: row[k] = v; break
                if k not in row and k in NAMES:   # 표준코드가 없을 때 계정 이름으로 보조 탐색
                    for it in lst:
                        if it.get("sj_div") in SJ[k] and re.sub(r"\s","",it.get("account_nm","")) in NAMES[k]:
                            v = num(it.get(col))
                            if v is not None: row[k]=v; break
            res[y] = row
        tag = "" if reprt == "11011" else "_" + reprt
        fn = os.path.join(out_dir, f"{key}_{year}{tag}.json")
        json.dump({"source_rcept_no": lst[0]["rcept_no"], "reprt_code": reprt, "years": res}, open(fn,"w"), ensure_ascii=False, indent=1)
        print(fn, {y: len(r) for y, r in res.items()})
main()
