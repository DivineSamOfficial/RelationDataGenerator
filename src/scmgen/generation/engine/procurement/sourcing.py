"""Sourcing (1C §3.0; D30; step 3.7a): replenishment → RFQ or direct PO → supplier responses → evaluation
and line-level award → purchase orders → approval and sending (or cancellation).

Every decision that spans days is re-derived from the documents each day (D66): e.g. an invited supplier
that hasn't answered gets today's chance to answer, drawn from the day's random stream in id order. No
schedule is kept in memory, so a run split at any day produces the same documents.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from math import ceil
from typing import Any, Iterable

from scmgen.core.money import money, quantity, unit_price
from scmgen.generation.engine.day import DayContext
from scmgen.generation.engine.documents import (approver, preload_rates, price_lines, rate_on, record_status,
                                                 staff, totals)
from scmgen.generation.numbering import Registration

OPEN_PO_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "SENT", "PARTIALLY_RECEIVED")
OPEN_RFQ_STATUSES = ("DRAFT", "ISSUED", "UNDER_EVALUATION")
DECLINE_REASONS = ("Capacity full", "Product discontinued", "Price not workable", "Lead time not possible")


# ------------------------------------------------------------------------------------------ lookups


def _dcs(day: DayContext) -> dict[int, dict[str, Any]]:
    if "dcs" not in day.notes:
        day.notes["dcs"] = {r["warehouse_id"]: r for r in day.rows("""
            SELECT w.warehouse_id, w.company_gst_registration_id, w.state_id, s.state_code
            FROM warehouses w JOIN states s ON s.state_id = w.state_id
            WHERE w.warehouse_type = 'DISTRIBUTION_CENTRE' AND w.warehouse_status = 'ACTIVE'
            ORDER BY w.warehouse_id""")}
    return day.notes["dcs"]


def _supplier(day: DayContext, supplier_id: int) -> dict[str, Any]:
    """GST type, rating, terms, registered state and dispatch address; all suppliers loaded once a day."""
    if "suppliers" not in day.notes:
        day.notes["suppliers"] = {r["supplier_id"]: r for r in day.rows("""
            SELECT s.supplier_id, s.gst_registration_type, s.supplier_rating, s.payment_term_id,
                   r.state_id AS registered_state_id,
                   COALESCE(d.supplier_address_id, r.supplier_address_id) AS dispatch_address_id
            FROM suppliers s
            JOIN supplier_addresses r ON r.supplier_id = s.supplier_id AND r.address_type = 'REGISTERED'
                 AND r.is_default AND r.deleted_at IS NULL
            LEFT JOIN supplier_addresses d ON d.supplier_id = s.supplier_id AND d.address_type = 'DISPATCH'
                 AND d.is_default AND d.deleted_at IS NULL""")}
    return day.notes["suppliers"][supplier_id]


def _approvals(day: DayContext, product_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
    """Active approved suppliers per product (supplier_products × active suppliers), cached for the day;
    only products not seen yet today are queried."""
    cache: dict[int, list[dict[str, Any]]] = day.notes.setdefault("approvals", {})
    wanted = sorted(set(product_ids))
    missing = [p for p in wanted if p not in cache]
    if missing:
        for p in missing:
            cache[p] = []
        for row in day.rows(f"""
                SELECT sp.product_id, sp.supplier_id, sp.reference_unit_cost, sp.minimum_order_quantity,
                       sp.lead_time_days, sp.is_preferred
                FROM supplier_products sp JOIN suppliers s ON s.supplier_id = sp.supplier_id
                WHERE sp.supplier_product_status = 'ACTIVE' AND sp.deleted_at IS NULL AND s.supplier_status = 'ACTIVE'
                  AND sp.product_id IN ({", ".join(str(p) for p in missing)})
                ORDER BY sp.product_id, sp.supplier_id"""):
            cache[row["product_id"]].append(row)
    return {p: cache[p] for p in wanted}


def _gst_rate(day: DayContext, product_id: int, supplier: dict[str, Any], on: date) -> Decimal:
    """Unregistered and composition suppliers charge no GST (1C §2.3)."""
    return rate_on(day, product_id, on) if supplier["gst_registration_type"] == "REGISTERED" else Decimal(0)


def _round_up(qty: Decimal, moq: Decimal) -> Decimal:
    return quantity(Decimal(ceil(qty / moq)) * moq) if moq > 0 else quantity(qty)


# ------------------------------------------------------------------------------------------ purchase orders


def create_po(day: DayContext, rng: random.Random, *, warehouse: dict[str, Any], supplier_id: int, source: str,
              lines: list[dict[str, Any]], quotation: dict[str, Any] | None = None) -> int:
    """Buffer a PO with its lines in PENDING_APPROVAL (or APPROVED and SENT, if approved the same day).

    ``lines``: product_id, ordered_quantity, unit_price, discount_percent, gst_rate, lead_time_days and, for
    RFQ awards, quotation_line_item_id. The PO's tax mode is the supplier's state vs the warehouse's.
    """
    settings = day.world.config.procurement
    supplier = _supplier(day, supplier_id)
    inter_state = (quotation["is_inter_state"] if quotation else supplier["registered_state_id"] != warehouse["state_id"])
    priced = price_lines(lines, bool(inter_state), "ordered_quantity")
    header = totals(priced, bool(inter_state))
    po_id = day.ids.next("purchase_order_headers")
    created = day.at(16)
    buyers = staff(day, "PUR", ("STAFF", "MANAGER"), day.day)
    creator = rng.choice(buyers)
    registration = Registration(warehouse["company_gst_registration_id"], warehouse["state_code"])
    same_day = rng.random() < 1 / (settings.approval_days[1] - settings.approval_days[0] + 1)
    approved_by = approver(day, header["total_amount"], ("PUR", "FIN"), day.day) if same_day else None
    status = "SENT" if same_day else "PENDING_APPROVAL"
    expected = max(day.day + timedelta(days=int(l["lead_time_days"])) for l in lines)
    day.writer.insert("purchase_order_headers", {
        "purchase_order_id": po_id, "po_number": day.numbers.next(day.conn, "PO", day.day, registration),
        "po_date": day.day, "po_source": source, "quotation_id": quotation["quotation_id"] if quotation else None,
        "supplier_id": supplier_id, "supplier_address_id": supplier["dispatch_address_id"],
        "delivery_warehouse_id": warehouse["warehouse_id"],
        "payment_term_id": quotation["payment_term_id"] if quotation else supplier["payment_term_id"],
        "is_inter_state": bool(inter_state), "expected_delivery_date": expected, **header, "po_status": status,
        "created_by_employee_id": creator, "approved_by_employee_id": approved_by,
        "approved_at": day.at(17) if same_day else None, "cancellation_reason": None,
        "created_at": created, "updated_at": day.at(17, 30) if same_day else created,
    })
    for number, line in enumerate(priced, 1):
        day.writer.insert("purchase_order_line_items", {
            "purchase_order_line_item_id": day.ids.next("purchase_order_line_items"), "purchase_order_id": po_id,
            "line_number": number, "product_id": line["product_id"],
            "quotation_line_item_id": line.get("quotation_line_item_id"), "ordered_quantity": line["ordered_quantity"],
            "unit_price": line["unit_price"], "discount_percent": line["discount_percent"],
            "taxable_amount": line["taxable_amount"], "gst_rate": line["gst_rate"], "cgst_amount": line["cgst_amount"],
            "sgst_amount": line["sgst_amount"], "igst_amount": line["igst_amount"], "line_total": line["line_total"],
            "expected_delivery_date": day.day + timedelta(days=int(line["lead_time_days"])),
            "quantity_received": 0, "quantity_accepted": 0, "quantity_rejected": 0, "quantity_invoiced": 0,
            "quantity_cancelled": 0, "line_status": "OPEN", "created_at": created, "updated_at": created,
        })
    record_status(day, "purchase_order", po_id, None, "DRAFT", created, creator)
    record_status(day, "purchase_order", po_id, "DRAFT", "PENDING_APPROVAL", created, creator)
    if same_day:
        record_status(day, "purchase_order", po_id, "PENDING_APPROVAL", "APPROVED", day.at(17), approved_by)
        record_status(day, "purchase_order", po_id, "APPROVED", "SENT", day.at(17, 30), creator)
    return po_id


def direct_po_lines(day: DayContext, warehouse: dict[str, Any], supplier_id: int,
                    needs: list[tuple[int, Decimal, dict[str, Any]]]) -> list[dict[str, Any]]:
    supplier = _supplier(day, supplier_id)
    return [{"product_id": product_id, "ordered_quantity": qty, "unit_price": unit_price(approval["reference_unit_cost"]),
             "discount_percent": Decimal(0), "gst_rate": _gst_rate(day, product_id, supplier, day.day),
             "lead_time_days": approval["lead_time_days"]}
            for product_id, qty, approval in needs]


# ------------------------------------------------------------------------------------------ day steps


def po_approvals(day: DayContext) -> None:
    """PENDING_APPROVAL POs from earlier days: cancelled (rarely), or approved and sent within the window."""
    settings = day.world.config.procurement
    rng = day.rng("procurement.approvals")
    for po in day.rows("SELECT purchase_order_id, po_date, total_amount, created_by_employee_id FROM purchase_order_headers "
                       "WHERE po_status = 'PENDING_APPROVAL' AND po_date < :d ORDER BY purchase_order_id", d=day.day):
        waited = (day.day - po["po_date"]).days
        if rng.random() < settings.po_cancel_share:
            reason = "Requirement changed before approval"
            day.writer.update("purchase_order_headers", "purchase_order_id", po["purchase_order_id"], {
                "po_status": "CANCELLED", "cancellation_reason": reason, "updated_at": day.at(11)})
            for line in day.rows("SELECT purchase_order_line_item_id, ordered_quantity FROM purchase_order_line_items "
                                 "WHERE purchase_order_id = :p", p=po["purchase_order_id"]):
                day.writer.update("purchase_order_line_items", "purchase_order_line_item_id",
                                  line["purchase_order_line_item_id"], {
                                      "quantity_cancelled": line["ordered_quantity"], "line_status": "CANCELLED",
                                      "updated_at": day.at(11)})
            record_status(day, "purchase_order", po["purchase_order_id"], "PENDING_APPROVAL", "CANCELLED", day.at(11),
                          po["created_by_employee_id"], reason)
            continue
        if waited >= settings.approval_days[1] or rng.random() < 0.6:
            by = approver(day, po["total_amount"], ("PUR", "FIN"), day.day)
            day.writer.update("purchase_order_headers", "purchase_order_id", po["purchase_order_id"], {
                "po_status": "SENT", "approved_by_employee_id": by, "approved_at": day.at(12),
                "updated_at": day.at(12, 30)})
            record_status(day, "purchase_order", po["purchase_order_id"], "PENDING_APPROVAL", "APPROVED", day.at(12), by)
            record_status(day, "purchase_order", po["purchase_order_id"], "APPROVED", "SENT", day.at(12, 30),
                          po["created_by_employee_id"])


def rfq_responses(day: DayContext) -> None:
    """Invited suppliers answer within the response window: a quotation, a decline, or silence."""
    settings = day.world.config.procurement
    rng = day.rng("procurement.responses")
    invitations = day.rows("""
        SELECT rs.rfq_supplier_id, rs.supplier_id, h.rfq_id, h.rfq_date, h.response_due_date, h.delivery_warehouse_id
        FROM rfq_suppliers rs JOIN rfq_headers h ON h.rfq_id = rs.rfq_id
        WHERE rs.response_status = 'INVITED' AND h.rfq_status = 'ISSUED' AND h.rfq_date < :d
        ORDER BY rs.rfq_supplier_id""", d=day.day)
    quotable: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in day.rows("""
            SELECT rs.rfq_supplier_id, l.rfq_line_item_id, l.product_id, l.requested_quantity, sp.reference_unit_cost,
                   sp.lead_time_days
            FROM rfq_suppliers rs JOIN rfq_headers h ON h.rfq_id = rs.rfq_id
            JOIN rfq_line_items l ON l.rfq_id = rs.rfq_id
            JOIN supplier_products sp ON sp.product_id = l.product_id AND sp.supplier_id = rs.supplier_id
                 AND sp.supplier_product_status = 'ACTIVE' AND sp.deleted_at IS NULL
            WHERE rs.response_status = 'INVITED' AND h.rfq_status = 'ISSUED' AND h.rfq_date < :d
              AND h.response_due_date >= :d
            ORDER BY rs.rfq_supplier_id, l.line_number""", d=day.day):
        quotable[line["rfq_supplier_id"]].append(line)
    preload_rates(day, (l["product_id"] for ls in quotable.values() for l in ls), day.day)
    answering = settings.response_mix["QUOTED"] + settings.response_mix["DECLINED"]
    for invite in invitations:
        if day.day > invite["response_due_date"]:
            day.writer.update("rfq_suppliers", "rfq_supplier_id", invite["rfq_supplier_id"], {
                "response_status": "NO_RESPONSE", "updated_at": day.at(9)})
            continue
        window = (invite["response_due_date"] - invite["rfq_date"]).days
        if rng.random() >= 1 - (1 - answering) ** (1 / max(window, 1)):
            continue
        if rng.random() < settings.response_mix["DECLINED"] / answering:
            day.writer.update("rfq_suppliers", "rfq_supplier_id", invite["rfq_supplier_id"], {
                "response_status": "DECLINED", "responded_date": day.day, "decline_reason": rng.choice(DECLINE_REASONS),
                "updated_at": day.at(14)})
            continue
        lines = quotable[invite["rfq_supplier_id"]]
        if not lines:                                     # its approvals ended since the invitation
            day.writer.update("rfq_suppliers", "rfq_supplier_id", invite["rfq_supplier_id"], {
                "response_status": "DECLINED", "responded_date": day.day, "decline_reason": "Product discontinued",
                "updated_at": day.at(14)})
            continue
        _quotation(day, rng, invite, lines)


def _quotation(day: DayContext, rng: random.Random, invite: dict[str, Any], lines: list[dict[str, Any]]) -> None:
    settings = day.world.config.procurement
    supplier = _supplier(day, invite["supplier_id"])
    warehouse = _dcs(day)[invite["delivery_warehouse_id"]]
    if len(lines) > 1:                                    # sometimes a supplier skips a line it can't do
        lines = [l for l in lines if rng.random() > 0.1] or lines[:1]
    inter_state = supplier["registered_state_id"] != warehouse["state_id"]
    items = []
    for line in lines:
        full = rng.random() < 0.9
        qty = line["requested_quantity"] if full else quantity(max(1, int(line["requested_quantity"] * Decimal(rng.uniform(0.5, 0.9)))))
        lead = max(1, min(120, round(line["lead_time_days"] * rng.uniform(*settings.quote_lead_factor))))
        items.append({"rfq_line_item_id": line["rfq_line_item_id"], "quoted_quantity": qty,
                      "unit_price": unit_price(money(line["reference_unit_cost"] * Decimal(str(round(rng.uniform(*settings.quote_price_factor), 4))))),
                      "discount_percent": Decimal(0), "gst_rate": _gst_rate(day, line["product_id"], supplier, day.day),
                      "lead_time_days": lead})
    priced = price_lines(items, inter_state, "quoted_quantity")
    header = totals(priced, inter_state)
    quotation_id = day.ids.next("quotation_headers")
    received = day.at(rng.randint(10, 17), rng.randint(0, 59))
    registration = Registration(warehouse["company_gst_registration_id"], warehouse["state_code"])
    day.writer.insert("quotation_headers", {
        "quotation_id": quotation_id, "quotation_number": day.numbers.next(day.conn, "QTN", day.day, registration),
        "rfq_supplier_id": invite["rfq_supplier_id"],
        "supplier_quotation_reference": f"Q{day.day:%y%m}-{rng.randint(1000, 9999)}",
        "quotation_date": day.day, "valid_until_date": day.day + timedelta(days=rng.randint(*settings.quote_validity_days)),
        "payment_term_id": supplier["payment_term_id"], "is_inter_state": inter_state, **header,
        "evaluation_score": None, "evaluated_by_employee_id": None, "evaluated_date": None,
        "quotation_status": "RECEIVED", "rejection_reason": None, "created_at": received, "updated_at": received,
    })
    for number, line in enumerate(priced, 1):
        day.writer.insert("quotation_line_items", {
            "quotation_line_item_id": day.ids.next("quotation_line_items"), "quotation_id": quotation_id,
            "line_number": number, "rfq_line_item_id": line["rfq_line_item_id"], "quoted_quantity": line["quoted_quantity"],
            "unit_price": line["unit_price"], "discount_percent": line["discount_percent"],
            "taxable_amount": line["taxable_amount"], "gst_rate": line["gst_rate"], "cgst_amount": line["cgst_amount"],
            "sgst_amount": line["sgst_amount"], "igst_amount": line["igst_amount"], "line_total": line["line_total"],
            "lead_time_days": line["lead_time_days"], "is_awarded": False, "created_at": received, "updated_at": received,
        })
    record_status(day, "quotation", quotation_id, None, "RECEIVED", received)
    day.writer.update("rfq_suppliers", "rfq_supplier_id", invite["rfq_supplier_id"], {
        "response_status": "QUOTED", "responded_date": day.day, "updated_at": received})


def rfq_evaluations(day: DayContext) -> None:
    """After the response window: evaluation starts, then within ``evaluation_days`` the award per line.
    Everything the awards read is fetched up front, before any of today's evaluation writes."""
    settings = day.world.config.procurement
    rng = day.rng("procurement.evaluations")
    rfqs = day.rows("SELECT rfq_id, rfq_status, response_due_date, delivery_warehouse_id, created_by_employee_id "
                    "FROM rfq_headers WHERE rfq_status IN ('ISSUED', 'UNDER_EVALUATION') AND response_due_date < :d "
                    "ORDER BY rfq_id", d=day.day)
    if not rfqs:
        return
    ids = ", ".join(str(r["rfq_id"]) for r in rfqs)
    waiting = {r["rfq_id"] for r in day.rows(f"SELECT DISTINCT rfq_id FROM rfq_suppliers WHERE rfq_id IN ({ids}) "
                                             "AND response_status = 'INVITED'")}
    received: dict[int, list[int]] = defaultdict(list)
    for q in day.rows(f"SELECT rs.rfq_id, q.quotation_id FROM quotation_headers q JOIN rfq_suppliers rs USING (rfq_supplier_id) "
                      f"WHERE rs.rfq_id IN ({ids}) AND q.quotation_status = 'RECEIVED' ORDER BY q.quotation_id"):
        received[q["rfq_id"]].append(q["quotation_id"])
    lines: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in day.rows(f"SELECT rfq_id, rfq_line_item_id, product_id, requested_quantity, target_unit_price "
                         f"FROM rfq_line_items WHERE rfq_id IN ({ids}) ORDER BY rfq_id, line_number"):
        lines[line["rfq_id"]].append(line)
    quotes: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for q in day.rows(f"""
            SELECT rs.rfq_id, q.quotation_id, q.valid_until_date, q.is_inter_state, q.payment_term_id, rs.supplier_id,
                   s.supplier_rating, ql.quotation_line_item_id, ql.rfq_line_item_id, ql.quoted_quantity, ql.unit_price,
                   ql.discount_percent, ql.gst_rate, ql.lead_time_days
            FROM quotation_headers q JOIN rfq_suppliers rs ON rs.rfq_supplier_id = q.rfq_supplier_id
            JOIN suppliers s ON s.supplier_id = rs.supplier_id
            JOIN quotation_line_items ql ON ql.quotation_id = q.quotation_id
            WHERE rs.rfq_id IN ({ids}) AND q.quotation_status = 'UNDER_EVALUATION'
            ORDER BY ql.quotation_line_item_id"""):
        quotes[q["rfq_id"]].append(q)
    _approvals(day, (l["product_id"] for ls in lines.values() for l in ls))
    preload_rates(day, (l["product_id"] for ls in lines.values() for l in ls), day.day)
    for rfq in rfqs:
        if rfq["rfq_status"] == "ISSUED":
            if rfq["rfq_id"] in waiting:                  # silence becomes NO_RESPONSE in rfq_responses first
                continue
            day.writer.update("rfq_headers", "rfq_id", rfq["rfq_id"], {"rfq_status": "UNDER_EVALUATION", "updated_at": day.at(9, 30)})
            record_status(day, "rfq", rfq["rfq_id"], "ISSUED", "UNDER_EVALUATION", day.at(9, 30), rfq["created_by_employee_id"])
            for quotation_id in received[rfq["rfq_id"]]:
                day.writer.update("quotation_headers", "quotation_id", quotation_id,
                                  {"quotation_status": "UNDER_EVALUATION", "updated_at": day.at(9, 30)})
                record_status(day, "quotation", quotation_id, "RECEIVED", "UNDER_EVALUATION", day.at(9, 30))
            continue
        waited = (day.day - rfq["response_due_date"]).days
        if waited < settings.evaluation_days[0] or (waited < settings.evaluation_days[1] and rng.random() < 0.5):
            continue
        _award(day, rng, rfq, lines[rfq["rfq_id"]], quotes[rfq["rfq_id"]])


def _award(day: DayContext, rng: random.Random, rfq: dict[str, Any], lines: list[dict[str, Any]],
           quotes: list[dict[str, Any]]) -> None:
    settings = day.world.config.procurement
    weights = settings.score_weights
    evaluator = rng.choice(staff(day, "PUR", ("MANAGER", "HEAD"), day.day))
    stamp = day.at(15)
    valid = [q for q in quotes if q["valid_until_date"] >= day.day]
    line_scores: dict[int, list[float]] = defaultdict(list)
    awarded: dict[int, dict[str, Any]] = {}                       # rfq line → winning quotation line
    for line in lines:
        offers = [q for q in valid if q["rfq_line_item_id"] == line["rfq_line_item_id"]]
        if not offers:
            continue
        cheapest = min(q["unit_price"] for q in offers)
        fastest = min(q["lead_time_days"] for q in offers)
        scored = []
        for q in offers:
            score = 100 * (weights["price"] * float(cheapest / q["unit_price"]) + weights["lead_time"] * fastest / q["lead_time_days"]
                           + weights["rating"] * float(q["supplier_rating"]) / 5)
            line_scores[q["quotation_id"]].append(score)
            scored.append((score, -q["quotation_line_item_id"], q))
        best = max(scored, key=lambda s: (s[0], s[1]))[2]
        target = line["target_unit_price"]
        if target is None or best["unit_price"] <= target * Decimal(str(settings.award_price_ceiling)):
            awarded[line["rfq_line_item_id"]] = best
    winners = {q["quotation_line_item_id"] for q in awarded.values()}
    by_quotation: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for q in quotes:
        by_quotation[q["quotation_id"]].append(q)
    for quotation_id, qlines in sorted(by_quotation.items()):
        won = [q for q in qlines if q["quotation_line_item_id"] in winners]
        if qlines[0]["valid_until_date"] < day.day:
            status, reason = "EXPIRED", None
        elif len(won) == len(qlines):
            status, reason = "ACCEPTED", None
        elif won:
            status, reason = "PARTIALLY_ACCEPTED", None
        else:
            status, reason = "REJECTED", rng.choice(("PRICE_TOO_HIGH", "LEAD_TIME_TOO_LONG", "LOWER_RATED"))
        scores = line_scores.get(quotation_id)
        evaluated = {"evaluation_score": Decimal(str(round(sum(scores) / len(scores), 2))), "evaluated_by_employee_id": evaluator,
                     "evaluated_date": day.day} if scores else {}
        day.writer.update("quotation_headers", "quotation_id", quotation_id,
                          {"quotation_status": status, "rejection_reason": reason, **evaluated, "updated_at": stamp})
        record_status(day, "quotation", quotation_id, "UNDER_EVALUATION", status, stamp, evaluator, reason)
        for q in won:
            day.writer.update("quotation_line_items", "quotation_line_item_id", q["quotation_line_item_id"],
                              {"is_awarded": True, "updated_at": stamp})
    for line in lines:
        day.writer.update("rfq_line_items", "rfq_line_item_id", line["rfq_line_item_id"], {
            "line_status": "AWARDED" if line["rfq_line_item_id"] in awarded else "NOT_AWARDED", "updated_at": stamp})
    status = "AWARDED" if len(awarded) == len(lines) else ("PARTIALLY_AWARDED" if awarded else "NO_AWARD")
    day.writer.update("rfq_headers", "rfq_id", rfq["rfq_id"], {
        "rfq_status": status, "awarded_at": stamp if awarded else None, "updated_at": stamp})
    record_status(day, "rfq", rfq["rfq_id"], "UNDER_EVALUATION", status, stamp, evaluator)

    warehouse = _dcs(day)[rfq["delivery_warehouse_id"]]
    by_winner: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        if line["rfq_line_item_id"] in awarded:
            by_winner[awarded[line["rfq_line_item_id"]]["quotation_id"]].append(
                {**awarded[line["rfq_line_item_id"]], "product_id": line["product_id"],
                 "requested_quantity": line["requested_quantity"]})
    for quotation_id, won in sorted(by_winner.items()):
        first = won[0]
        create_po(day, rng, warehouse=warehouse, supplier_id=first["supplier_id"], source="RFQ_AWARD", quotation=first,
                  lines=[{"product_id": q["product_id"], "quotation_line_item_id": q["quotation_line_item_id"],
                          "ordered_quantity": min(q["quoted_quantity"], q["requested_quantity"]), "unit_price": q["unit_price"],
                          "discount_percent": q["discount_percent"],
                          # GST follows the supply date: a rate change since the quotation applies (D17).
                          "gst_rate": _gst_rate(day, q["product_id"], _supplier(day, q["supplier_id"]), day.day),
                          "lead_time_days": q["lead_time_days"]} for q in won])
    # Lines nobody could be awarded fall back to a direct reorder from the preferred supplier (D30).
    unawarded = [line for line in lines if line["rfq_line_item_id"] not in awarded]
    _direct_orders(day, rng, warehouse, [(l["product_id"], l["requested_quantity"]) for l in unawarded])


def _direct_orders(day: DayContext, rng: random.Random, warehouse: dict[str, Any],
                   needs: list[tuple[int, Decimal]]) -> None:
    approvals = _approvals(day, [p for p, _ in needs])
    per_supplier: dict[int, list[tuple[int, Decimal, dict[str, Any]]]] = defaultdict(list)
    for product_id, qty in needs:
        preferred = next((a for a in approvals.get(product_id, []) if a["is_preferred"]), None)
        if preferred is not None:
            per_supplier[preferred["supplier_id"]].append((product_id, _round_up(qty, preferred["minimum_order_quantity"]), preferred))
    for supplier_id, items in sorted(per_supplier.items()):
        for start in range(0, len(items), 20):
            chunk = items[start:start + 20]
            create_po(day, rng, warehouse=warehouse, supplier_id=supplier_id, source="DIRECT_REORDER",
                      lines=direct_po_lines(day, warehouse, supplier_id, chunk))


def replenishment(day: DayContext) -> None:
    """Daily stock check per DC (1C §3.0): what's below its reorder point (counting stock already on order or
    being sourced) is ordered up to the maximum, directly or via an RFQ; new products are sourced by RFQ."""
    settings = day.world.config.procurement
    rng = day.rng("procurement.replenishment")
    dcs = _dcs(day)
    if not dcs:
        return
    in_dc = ", ".join(str(w) for w in dcs)
    statuses_po = ", ".join(f"'{s}'" for s in OPEN_PO_STATUSES)
    statuses_rfq = ", ".join(f"'{s}'" for s in OPEN_RFQ_STATUSES)
    candidates = day.rows(f"""
        SELECT i.warehouse_id, p.product_id, p.business_unit_id, i.max_stock_level, i.reorder_level,
               i.qty_sellable - i.qty_reserved AS available
        FROM inventory i JOIN products p ON p.product_id = i.product_id
        WHERE i.warehouse_id IN ({in_dc}) AND p.product_status = 'ACTIVE' AND i.is_stocked
          AND i.qty_sellable - i.qty_reserved <= i.reorder_level
        ORDER BY i.warehouse_id, p.product_id""")
    on_order: dict[tuple[int, int], Decimal] = defaultdict(Decimal)    # stock on order or being sourced
    if candidates:
        products = ", ".join(sorted({str(r["product_id"]) for r in candidates}))
        for row in day.rows(f"""
                SELECT h.delivery_warehouse_id AS warehouse_id, l.product_id, l.quantity_open AS qty
                FROM purchase_order_line_items l JOIN purchase_order_headers h ON h.purchase_order_id = l.purchase_order_id
                WHERE l.product_id IN ({products}) AND h.po_status IN ({statuses_po})
                UNION ALL
                SELECT h.delivery_warehouse_id, l.product_id, l.requested_quantity
                FROM rfq_line_items l JOIN rfq_headers h ON h.rfq_id = l.rfq_id
                WHERE l.product_id IN ({products}) AND h.rfq_status IN ({statuses_rfq})"""):
            on_order[(row["warehouse_id"], row["product_id"])] += row["qty"]
    below = []
    for row in candidates:
        position = row["available"] + on_order[(row["warehouse_id"], row["product_id"])]
        if position <= row["reorder_level"]:
            below.append({**row, "position": position})
    # New products (no stock row in the DC yet) and nothing on order or being sourced: one anti-join.
    new = day.rows(f"""
        SELECT w.warehouse_id, p.product_id, p.business_unit_id, p.reorder_level + p.reorder_quantity AS need
        FROM products p CROSS JOIN warehouses w
        LEFT JOIN inventory i ON i.warehouse_id = w.warehouse_id AND i.product_id = p.product_id
        LEFT JOIN (SELECT DISTINCT h.delivery_warehouse_id AS warehouse_id, l.product_id
                   FROM purchase_order_line_items l JOIN purchase_order_headers h ON h.purchase_order_id = l.purchase_order_id
                   WHERE h.po_status IN ({statuses_po})
                   UNION
                   SELECT h.delivery_warehouse_id, l.product_id
                   FROM rfq_line_items l JOIN rfq_headers h ON h.rfq_id = l.rfq_id WHERE h.rfq_status IN ({statuses_rfq})) o
               ON o.warehouse_id = w.warehouse_id AND o.product_id = p.product_id
        WHERE w.warehouse_id IN ({in_dc}) AND p.product_status = 'ACTIVE' AND p.launch_date <= :d
          AND i.inventory_id IS NULL AND o.product_id IS NULL
        ORDER BY w.warehouse_id, p.product_id""", d=day.day)
    approvals = _approvals(day, sorted({r["product_id"] for r in below} | {r["product_id"] for r in new}))
    preload_rates(day, approvals, day.day)
    direct: dict[int, list[tuple[int, Decimal]]] = defaultdict(list)
    sourcing: dict[tuple[int, int, str], list[tuple[int, Decimal]]] = defaultdict(list)
    for row in below:
        need = quantity(row["max_stock_level"] - row["position"])
        if need <= 0:
            continue
        preferred = next((a for a in approvals.get(row["product_id"], []) if a["is_preferred"]), None)
        if preferred and need * preferred["reference_unit_cost"] <= settings.direct_reorder_max_value:
            direct[row["warehouse_id"]].append((row["product_id"], need))
        else:
            sourcing[(row["warehouse_id"], row["business_unit_id"], "REPLENISHMENT")].append((row["product_id"], need))
    for row in new:
        sourcing[(row["warehouse_id"], row["business_unit_id"], "NEW_PRODUCT")].append((row["product_id"], quantity(row["need"])))
    for warehouse_id, needs in sorted(direct.items()):
        _direct_orders(day, rng, dcs[warehouse_id], needs)
    for (warehouse_id, bu_id, rfq_type), needs in sorted(sourcing.items()):
        for start in range(0, len(needs), settings.rfq_max_lines):
            _create_rfq(day, rng, dcs[warehouse_id], bu_id, rfq_type, needs[start:start + settings.rfq_max_lines], approvals)


def _create_rfq(day: DayContext, rng: random.Random, warehouse: dict[str, Any], bu_id: int, rfq_type: str,
                needs: list[tuple[int, Decimal]], approvals: dict[int, list[dict[str, Any]]]) -> None:
    settings = day.world.config.procurement
    needs = [(p, q) for p, q in needs if approvals.get(p)]
    if not needs:
        return
    rfq_id = day.ids.next("rfq_headers")
    created = day.at(10)
    creator = rng.choice(staff(day, "PUR", ("STAFF", "MANAGER"), day.day))
    due = day.day + timedelta(days=rng.randint(*settings.rfq_response_days))
    registration = Registration(warehouse["company_gst_registration_id"], warehouse["state_code"])
    day.writer.insert("rfq_headers", {
        "rfq_id": rfq_id, "rfq_number": day.numbers.next(day.conn, "RFQ", day.day, registration), "rfq_date": day.day,
        "rfq_type": rfq_type, "delivery_warehouse_id": warehouse["warehouse_id"], "business_unit_id": bu_id,
        "response_due_date": due, "required_by_date": due + timedelta(days=21), "rfq_status": "ISSUED",
        "created_by_employee_id": creator, "awarded_at": None, "cancellation_reason": None,
        "created_at": created, "updated_at": created,
    })
    candidates: set[int] = set()
    preferred: set[int] = set()
    for number, (product_id, qty) in enumerate(needs, 1):
        options = approvals[product_id]
        best = next((a for a in options if a["is_preferred"]), options[0])
        preferred.add(best["supplier_id"])
        candidates.update(a["supplier_id"] for a in options)
        day.writer.insert("rfq_line_items", {
            "rfq_line_item_id": day.ids.next("rfq_line_items"), "rfq_id": rfq_id, "line_number": number,
            "product_id": product_id, "requested_quantity": _round_up(qty, best["minimum_order_quantity"]),
            "target_unit_price": unit_price(best["reference_unit_cost"]), "line_status": "OPEN",
            "created_at": created, "updated_at": created,
        })
    others = sorted(candidates - preferred)
    low, high = settings.rfq_suppliers
    wanted = max(low, min(high, rng.randint(low, high)))
    invited = sorted(preferred)[:high] + rng.sample(others, k=max(0, min(len(others), wanted - len(preferred))))
    for supplier_id in sorted(set(invited)):
        day.writer.insert("rfq_suppliers", {
            "rfq_supplier_id": day.ids.next("rfq_suppliers"), "rfq_id": rfq_id, "supplier_id": supplier_id,
            "invited_date": day.day, "response_status": "INVITED", "responded_date": None, "decline_reason": None,
            "created_at": created, "updated_at": created,
        })
    record_status(day, "rfq", rfq_id, None, "DRAFT", created, creator)
    record_status(day, "rfq", rfq_id, "DRAFT", "ISSUED", created, creator)
