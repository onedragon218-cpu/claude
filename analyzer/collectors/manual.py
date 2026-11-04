"""CSV로 직접 넣는 자료(컨센서스 등).

컨센서스 CSV 열: target_year,metric,value,unit,broker,n_estimates,provider,as_of,source_ref
  - metric: revenue(억원) op_income(억원) op_margin(%) eps(원) bps(원) roe(%) debt_ratio(%)
  - broker를 비우면 평균값으로 저장한다.
"""
import csv


def read_consensus_csv(path):
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not r.get("target_year") or r.get("value") in (None, ""):
                continue
            out.append({
                "target_year": int(r["target_year"]), "metric": r["metric"].strip(),
                "value": float(str(r["value"]).replace(",", "")), "unit": (r.get("unit") or "").strip() or None,
                "broker": (r.get("broker") or "").strip() or "(평균)",
                "n_estimates": int(r["n_estimates"]) if (r.get("n_estimates") or "").strip() else None,
                "provider": (r.get("provider") or "manual").strip(), "as_of": r["as_of"].strip(),
                "source_ref": (r.get("source_ref") or "").strip() or path,
            })
    return out
