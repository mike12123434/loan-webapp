"""
永豐銀行 小微東風貸 – 利率試算後端
Flask + Gunicorn, 部署於 Railway
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)          # templates/ 與 static/ 走 Flask 預設路徑
CORS(app)                      # 允許跨來源（開發用；正式環境可縮小 origins）


# ===========================================================================
# 1) 離散欄位權重
# ===========================================================================
INDUSTRY_WEIGHT: Dict[str, int] = {
    "education":           13,
    "other_services":      13,
    "water_waste":         12,
    "arts_entertainment":  12,
    "public_admin":        11,
    "mining":              10,
    "construction":        10,
    "professional_science":10,
    "support_services":    10,
    "healthcare_social":   10,
    "hospitality":          9,
    "agriculture":          8,
    "transport_storage":    7,
    "electricity_gas":      6,
    "media_it":             5,
    "wholesale_retail":     4,
    "real_estate":          3,
    "finance_insurance":    2,
    "manufacturing":        1,
}

COMPANY_YEARS_WEIGHT: Dict[str, int] = {
    "lt1":   4,
    "1to3":  3,
    "3to5":  2,
    "5p":    1,
}

YES_NO_2_1:     Dict[bool, int] = {True: 2, False: 1}
PROPERTY_WEIGHT: Dict[bool, int] = {True: 1, False: 2}

JCIC_WEIGHT: Dict[str, int] = {
    "unknown":  6,
    "lt500":    5,
    "500_599":  4,
    "600_699":  3,
    "700_799":  2,
    "800p":     1,
}


# ===========================================================================
# 2) 連續數值指標
# ===========================================================================
def w_loan_amount(loan_amt_10k: float) -> Tuple[int, str]:
    if loan_amt_10k < 1:
        return 6, "申貸額度異常偏低（<1 萬元）"
    if loan_amt_10k > 99_999:
        return 6, "申貸額度超出上限（>99,999 萬元）"
    if loan_amt_10k <= 300:
        return 0, "申貸額度較小（≤300 萬）"
    if loan_amt_10k <= 1_000:
        return 1, "申貸額度中等（301~1,000 萬）"
    if loan_amt_10k <= 3_000:
        return 2, "申貸額度偏高（1,001~3,000 萬）"
    if loan_amt_10k <= 10_000:
        return 3, "申貸額度高（3,001~10,000 萬）"
    if loan_amt_10k <= 30_000:
        return 4, "申貸額度很高（10,001~30,000 萬）"
    return 5, "申貸額度極高（30,001~99,999 萬）"


def w_deposit(total_dep_10k: float, loan_amt_10k: float) -> Tuple[int, str]:
    if loan_amt_10k <= 0:
        return 0, "申貸額度為0，忽略存款覆蓋率"
    dcr = total_dep_10k / loan_amt_10k
    if dcr >= 0.50:
        return -3, "存款覆蓋率高（>50%）"
    if dcr >= 0.20:
        return -2, "存款覆蓋率良好（20%~50%）"
    if dcr >= 0.10:
        return -1, "存款覆蓋率尚可（10%~20%）"
    return 0, "存款覆蓋率偏低（<10%）"


def w_revenue(revenue_10k: float, loan_amt_10k: float) -> Tuple[int, str]:
    if revenue_10k <= 0:
        return 4, "營業額為0/未提供，資訊不足"
    ltr = loan_amt_10k / revenue_10k
    if ltr <= 0.2:
        return 0, "借款/營收比低（≤0.2）"
    if ltr <= 0.5:
        return 1, "借款/營收比中低（0.2~0.5）"
    if ltr <= 1.0:
        return 3, "借款/營收比偏高（0.5~1.0）"
    return 5, "借款/營收比過高（>1.0）"


# ===========================================================================
# 3) 利率 / 還款試算
# ===========================================================================
def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def apr_from_risk(risk_points: int) -> Tuple[float, str]:
    MIN_P, MAX_P = 6, 50
    rp  = clamp(risk_points, MIN_P, MAX_P)
    apr = 3.0 + (rp - MIN_P) / (MAX_P - MIN_P) * (12.0 - 3.0)
    apr = float(clamp(apr, 3.0, 12.0))
    band = 1.2 if risk_points >= 40 else 0.75
    lo   = clamp(apr - band, 2.5, 15.0)
    hi   = clamp(apr + band, 2.5, 15.0)
    return apr, f"{lo:.2f}% ~ {hi:.2f}%"


def amortized_payment(principal: float, apr_percent: float, months: int) -> float:
    r = (apr_percent / 100.0) / 12.0
    if months <= 0:
        return principal
    if r == 0:
        return principal / months
    return principal * (r * (1 + r) ** months) / ((1 + r) ** months - 1)


def estimate_fee(principal: float) -> float:
    return float(clamp(principal * 0.005, 3_000, 20_000))


# ===========================================================================
# 4) 核心計分
# ===========================================================================
@dataclass
class QuoteResult:
    risk_points:        int
    score_0_100:        int
    apr_percent:        float
    apr_range:          str
    monthly_payment_ntd: float
    total_interest_ntd:  float
    total_payment_ntd:   float
    fee_ntd:             float
    reasons:             List[str]


def calc_quote(p: Dict[str, Any]) -> QuoteResult:
    reasons: List[str] = []

    loan_amt_10k  = float(p["loan_amt_10k"])
    industry      = str(p["industry"])
    company_years = str(p["company_years"])
    changed_owner = bool(p["changed_owner"])
    cc_revolve    = bool(p["cc_revolve"])
    has_bank_loan = bool(p["has_bank_loan"])
    has_lease_loan= bool(p["has_lease_loan"])
    has_property  = bool(p["has_property"])
    jcic          = str(p["jcic_score_band"])
    co_dep        = float(p["co_avg_dep_10k"])
    ow_dep        = float(p["owner_avg_dep_10k"])
    revenue       = float(p["revenue_10k"])
    tenor_months  = int(p.get("tenor_months", 36))

    # ── 離散權重 ───────────────────────────────────────────────────────────
    ind_w   = INDUSTRY_WEIGHT.get(industry, 9)
    years_w = COMPANY_YEARS_WEIGHT.get(company_years, 3)
    chg_w   = YES_NO_2_1[changed_owner]
    cc_w    = YES_NO_2_1[cc_revolve]
    bank_w  = YES_NO_2_1[has_bank_loan]
    lease_w = YES_NO_2_1[has_lease_loan]
    prop_w  = PROPERTY_WEIGHT[has_property]
    jcic_w  = JCIC_WEIGHT.get(jcic, 6)

    reasons.append(f"產業風險權重：{ind_w}")
    reasons.append(f"公司成立年限權重：{years_w}")
    reasons.append(("曾變更負責人" if changed_owner else "未變更負責人") + f"（權重{chg_w}）")
    reasons.append(("近三個月有卡循/現金卡" if cc_revolve else "近三個月無卡循/現金卡") + f"（權重{cc_w}）")
    reasons.append(("目前有金融機構貸款" if has_bank_loan else "目前無金融機構貸款") + f"（權重{bank_w}）")
    reasons.append(("近半年有租賃借款" if has_lease_loan else "近半年無租賃借款") + f"（權重{lease_w}）")
    reasons.append(("名下有不動產" if has_property else "名下無不動產") + f"（權重{prop_w}）")
    reasons.append(f"聯徵分數區間（權重{jcic_w}）")

    # ── 連續指標 ───────────────────────────────────────────────────────────
    amt_adj, amt_msg = w_loan_amount(loan_amt_10k)
    dep_adj, dep_msg = w_deposit(co_dep + ow_dep, loan_amt_10k)
    rev_adj, rev_msg = w_revenue(revenue, loan_amt_10k)

    reasons.append(f"{amt_msg}（+{amt_adj}）")
    reasons.append(f"{dep_msg}（{'+' if dep_adj >= 0 else ''}{dep_adj}）")
    reasons.append(f"{rev_msg}（+{rev_adj}）")

    # ── 風險點數合計 ───────────────────────────────────────────────────────
    risk_points = (
        ind_w + years_w + chg_w + cc_w + bank_w + lease_w + prop_w + jcic_w
        + amt_adj + dep_adj + rev_adj
    )

    MIN_P, MAX_P = 6, 50
    score_0_100 = int(round(
        100 - clamp((risk_points - MIN_P) / (MAX_P - MIN_P) * 100, 0, 100)
    ))

    apr_percent, apr_range = apr_from_risk(int(risk_points))
    principal   = loan_amt_10k * 10_000
    fee         = estimate_fee(principal)
    monthly     = amortized_payment(principal, apr_percent, tenor_months)
    total_pmt   = monthly * tenor_months
    total_int   = total_pmt - principal

    return QuoteResult(
        risk_points         = int(risk_points),
        score_0_100         = score_0_100,
        apr_percent         = apr_percent,
        apr_range           = apr_range,
        monthly_payment_ntd = float(monthly),
        total_interest_ntd  = float(total_int),
        total_payment_ntd   = float(total_pmt),
        fee_ntd             = float(fee),
        reasons             = reasons[:10],
    )


# ===========================================================================
# 5) 輸入驗證
# ===========================================================================
def validate_payload(p: Dict[str, Any]) -> Tuple[bool, str]:
    tax_id = str(p.get("tax_id", "")).strip()
    if not (tax_id.isdigit() and len(tax_id) == 8):
        return False, "tax_id 必須為 8 碼數字"

    def num_in_range(key: str, lo: float, hi: float) -> bool:
        try:
            return lo <= float(p.get(key)) <= hi  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    if not num_in_range("loan_amt_10k", 1, 99_999):
        return False, "loan_amt_10k 必須介於 1~99,999（單位：萬元）"

    for k in ("co_avg_dep_10k", "owner_avg_dep_10k", "revenue_10k"):
        if not num_in_range(k, 0, 99_999):
            return False, f"{k} 必須介於 0~99,999（單位：萬元）"

    for required in ("purpose", "industry", "company_years", "jcic_score_band"):
        if not str(p.get(required, "")).strip():
            return False, f"{required} 必填"

    return True, ""


# ===========================================================================
# 6) Routes
# ===========================================================================
@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/marketing-quote")
def marketing_quote():
    p = request.get_json(silent=True) or {}
    ok, err = validate_payload(p)
    if not ok:
        logger.warning("Validation failed: %s | payload keys: %s", err, list(p.keys()))
        return jsonify({"ok": False, "error": err}), 400

    try:
        q = calc_quote(p)
        return jsonify({
            "ok": True,
            "result": {
                "apr_percent":         q.apr_percent,
                "apr_range":           q.apr_range,
                "monthly_payment_ntd": q.monthly_payment_ntd,
                "total_interest_ntd":  q.total_interest_ntd,
                "total_payment_ntd":   q.total_payment_ntd,
                "fee_ntd":             q.fee_ntd,
                "score":               q.score_0_100,
                "risk_points":         q.risk_points,
                "reasons":             q.reasons,
            },
        })
    except Exception as exc:
        logger.exception("calc_quote failed: %s", exc)
        return jsonify({"ok": False, "error": "server error"}), 500


@app.get("/health")
def health():
    return jsonify({"ok": True})


# ===========================================================================
# 7) Entry-point（本機開發用，Railway 透過 Gunicorn 啟動）
# ===========================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    logger.info("Starting dev server on port %d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug)
