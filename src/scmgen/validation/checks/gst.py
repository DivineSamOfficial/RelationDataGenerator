"""GST return rules (1E §7 V-TX-03; D58; step 3.14b). Each check returns the violating rows."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from scmgen.generation.engine.gst_returns import HEADS, set_off
from scmgen.validation.framework import ValidationContext, check

Rows = list[dict[str, Any]]


@check("V-TX-03")
def returns_match_their_tax_rows(ctx: ValidationContext) -> Rows:
    """★ GSTR-1 section lines = Σ the included OUTPUT rows per section (and the HSN summary = all of them); GSTR-3B:
    output = its period's GSTR-1, 4A5 = Σ included eligible INPUT rows = the credit claimed; 6.1 lines = credit used
    per type and cash per head, and output − credit used − cash = 0 per head; the set-off follows the legal order on
    the credit available (carried forward + claimed); P22 and the GST payment line carry the 3B amounts."""
    found = [{**r, "problem": "GSTR-1 section ≠ Σ included rows of that section"} for r in ctx.rows("""
        SELECT r.gst_return_id, x.section, x.cgst, x.sgst, x.igst, t.cgst AS rows_cgst, t.sgst AS rows_sgst,
               t.igst AS rows_igst
        FROM gst_returns r
        JOIN (SELECT gst_return_id, section_code AS section, SUM(cgst_amount) AS cgst, SUM(sgst_amount) AS sgst,
                     SUM(igst_amount) AS igst FROM gst_return_line_items GROUP BY 1, 2) x ON x.gst_return_id = r.gst_return_id
        LEFT JOIN (SELECT gst_return_id, gstr1_section AS section, SUM(cgst_amount) AS cgst, SUM(sgst_amount) AS sgst,
                          SUM(igst_amount) AS igst FROM tax_transactions WHERE tax_direction = 'OUTPUT' GROUP BY 1, 2
                   UNION ALL
                   SELECT gst_return_id, 'HSN', SUM(cgst_amount), SUM(sgst_amount), SUM(igst_amount)
                   FROM tax_transactions WHERE tax_direction = 'OUTPUT' GROUP BY 1) t
               ON t.gst_return_id = r.gst_return_id AND t.section = x.section
        WHERE r.return_type = 'GSTR1'
          AND (NOT (x.cgst <=> t.cgst) OR NOT (x.sgst <=> t.sgst) OR NOT (x.igst <=> t.igst))""")]
    found += [{**r, "problem": "OUTPUT row in no return although its period's GSTR-1 is filed"} for r in ctx.rows("""
        SELECT t.tax_transaction_id, t.return_period FROM tax_transactions t
        JOIN gst_returns r ON r.company_gst_registration_id = t.company_gst_registration_id
             AND r.return_type = 'GSTR1' AND r.return_period >= t.return_period AND r.created_at > t.created_at
        WHERE t.tax_direction = 'OUTPUT' AND t.gst_return_id IS NULL""")]
    returns = ctx.rows("""
        SELECT r.*, (SELECT COALESCE(SUM(t.cgst_amount), 0) FROM tax_transactions t JOIN gst_returns g ON g.gst_return_id = t.gst_return_id
                     WHERE g.return_type = 'GSTR1' AND g.return_period = r.return_period
                       AND g.company_gst_registration_id = r.company_gst_registration_id) AS out_cgst,
               (SELECT COALESCE(SUM(t.sgst_amount), 0) FROM tax_transactions t JOIN gst_returns g ON g.gst_return_id = t.gst_return_id
                     WHERE g.return_type = 'GSTR1' AND g.return_period = r.return_period
                       AND g.company_gst_registration_id = r.company_gst_registration_id) AS out_sgst,
               (SELECT COALESCE(SUM(t.igst_amount), 0) FROM tax_transactions t JOIN gst_returns g ON g.gst_return_id = t.gst_return_id
                     WHERE g.return_type = 'GSTR1' AND g.return_period = r.return_period
                       AND g.company_gst_registration_id = r.company_gst_registration_id) AS out_igst,
               (SELECT COALESCE(SUM(t.cgst_amount), 0) FROM tax_transactions t WHERE t.gst_return_id = r.gst_return_id
                       AND t.itc_eligibility = 'ELIGIBLE') AS in_cgst,
               (SELECT COALESCE(SUM(t.sgst_amount), 0) FROM tax_transactions t WHERE t.gst_return_id = r.gst_return_id
                       AND t.itc_eligibility = 'ELIGIBLE') AS in_sgst,
               (SELECT COALESCE(SUM(t.igst_amount), 0) FROM tax_transactions t WHERE t.gst_return_id = r.gst_return_id
                       AND t.itc_eligibility = 'ELIGIBLE') AS in_igst,
               (SELECT j.total_debit FROM journal_entries j WHERE j.gst_return_id = r.gst_return_id) AS journal_total,
               (SELECT SUM(b.debit_amount) FROM bank_transactions b WHERE b.gst_return_id = r.gst_return_id) AS bank_paid
        FROM gst_returns r WHERE r.return_type = 'GSTR3B'
        ORDER BY r.company_gst_registration_id, r.return_period""")
    six_one: dict[int, dict[tuple[str, str], Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for line in ctx.rows("SELECT gst_return_id, paid_through, cgst_amount, sgst_amount, igst_amount "
                         "FROM gst_return_line_items WHERE section_code = '6.1'"):
        for head in HEADS:
            six_one[line["gst_return_id"]][(line["paid_through"], head)] += line[f"{head}_amount"]
    carried: dict[int, dict[str, Decimal]] = defaultdict(lambda: {h: Decimal(0) for h in HEADS})
    for r in returns:
        problem = None
        liability = {h: max(r[f"out_{h}"], Decimal(0)) for h in HEADS}
        claimed = {h: r[f"in_{h}"] for h in HEADS}
        available = {h: carried[r["company_gst_registration_id"]][h] + claimed[h] for h in HEADS}
        used, cash = set_off(liability, available)
        lines = six_one[r["gst_return_id"]]
        if any(r[f"output_{h}"] != liability[h] or r[f"itc_{h}"] != claimed[h] for h in HEADS):
            problem = "3B output or claimed credit differs from its tax rows"
        elif any(r[f"cash_paid_{h}"] != cash[h] or lines[("CASH", h)] != cash[h] for h in HEADS):
            problem = "cash paid differs from the legal set-off"
        elif any(r[f"itc_used_{k}"] != sum((a for t, _, a in used if t == k), Decimal(0)) for k in HEADS) \
                or any(lines[(f"ITC_{k.upper()}", head)] != a for k, head, a in used):
            problem = "credit used differs from the legal set-off (D58)"
        elif any(liability[h] - sum((v for (p, hh), v in lines.items() if hh == h), Decimal(0)) != 0 for h in HEADS):
            problem = "output − credit used − cash ≠ 0 for a head"
        elif sum(liability.values()) and r["journal_total"] != sum(liability.values()):
            problem = "P22 doesn't carry the 3B liability"
        elif (r["bank_paid"] or Decimal(0)) != sum(cash.values()):
            problem = "GST payment line ≠ cash paid"
        if problem:
            found.append({"gst_return_id": r["gst_return_id"], "return_period": r["return_period"], "problem": problem})
        for k in HEADS:
            carried[r["company_gst_registration_id"]][k] = available[k] - sum((a for t, _, a in used if t == k), Decimal(0))
    return found
