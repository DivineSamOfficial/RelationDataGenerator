"""Day steps in their fixed order (1G §2.2). Each is ``step(day: DayContext) -> None``.

Step 3.6 implements 1 Calendar, 2 Periods and 3 Master-data changes; the business steps (procurement,
demand, fulfilment …) are added by steps 3.7-3.14 at their place in ``DAY_STEPS``.
"""

from __future__ import annotations

from datetime import date, timedelta

from scmgen.core.calendar import fiscal_year, fiscal_year_label
from scmgen.generation.engine.day import DayContext, poisson
from scmgen.generation.master.parties import approve_suppliers, create_campaign
from scmgen.generation.engine.opening import opening_step
from scmgen.generation.engine.procurement import procurement_step
from scmgen.generation.engine.collections import collections_step
from scmgen.generation.engine.fulfilment import fulfilment_step
from scmgen.generation.engine.sales import sales_step
from scmgen.generation.engine.sales.customers import acquisition_step
from scmgen.generation.master.catalogue import create_style


# ------------------------------------------------------------------------------------------ 1 calendar


def calendar_step(day: DayContext) -> None:
    """Working day or not: warehouses close on the weekly off day and national holidays."""
    day.is_working_day = day.world.config.daily.is_working_day(day.day)


# ------------------------------------------------------------------------------------------ 2 periods


def _close_date(day: DayContext, period_end: date) -> date:
    """The configured close day of the month after the period, moved to the next working day."""
    after = period_end + timedelta(days=1)
    close = date(after.year, after.month, day.world.config.daily.period_close_day)
    while not day.world.config.daily.is_working_day(close):
        close += timedelta(days=1)
    return close


def _ensure_fiscal_year(day: DayContext) -> None:
    """Continue mode can run past the last generated FY: add its twelve FUTURE periods (1B §4.9)."""
    if day.rows("SELECT 1 FROM accounting_periods WHERE :d BETWEEN start_date AND end_date", d=day.day):
        return
    start_month = day.world.config.fiscal_year_start_month
    fy = fiscal_year(day.day, start_month)
    stamp = day.at(0)
    for number in range(1, 13):
        index = start_month - 1 + number - 1
        year, month = fy + index // 12, index % 12 + 1
        start = date(year, month, 1)
        end = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
        day.writer.insert("accounting_periods", {
            "accounting_period_id": day.ids.next("accounting_periods"), "fiscal_year": fy,
            "fiscal_year_label": fiscal_year_label(fy, start_month), "period_number": number,
            "period_code": f"{year}-{month:02d}", "period_name": start.strftime("%b %Y"), "start_date": start,
            "end_date": end, "period_status": "FUTURE", "opened_at": None, "closed_at": None,
            "closed_by_employee_id": None, "created_at": stamp, "updated_at": stamp,
        })


def periods_step(day: DayContext) -> None:
    """Open the month on its first simulated day; close a month on the close day of the next (1B §4.9)."""
    _ensure_fiscal_year(day)
    opened = day.at(0)
    day.execute("UPDATE accounting_periods SET period_status = 'OPEN', opened_at = :t, updated_at = :t "
                "WHERE period_status = 'FUTURE' AND :d BETWEEN start_date AND end_date", {"t": opened, "d": day.day})
    for period in day.rows("SELECT accounting_period_id, end_date FROM accounting_periods "
                           "WHERE period_status = 'OPEN' AND end_date < :d ORDER BY start_date", d=day.day):
        if day.day >= _close_date(day, period["end_date"]):
            closed = day.at(18)
            day.execute("UPDATE accounting_periods SET period_status = 'CLOSED', closed_at = :t, "
                        "closed_by_employee_id = :e, updated_at = :t WHERE accounting_period_id = :p",
                        {"t": closed, "e": day.world.finance_closer, "p": period["accounting_period_id"]})


# ------------------------------------------------------------------------------------------ 3 master data


def gst_revision_step(day: DayContext) -> None:
    """D17: on the revision date, close each affected product's current rate and open the new one."""
    revision = day.world.seed.gst_rate_revision
    if not day.world.config.gst_rate_revision or day.day != revision["effective_date"]:
        return
    stamp = day.at(0, 0, 1)
    closing, opening = [], []
    for row in day.rows("""
            SELECT t.product_tax_rate_id, t.product_id, t.gst_rate, c.category_code
            FROM product_tax_rates t JOIN products p ON p.product_id = t.product_id
            JOIN product_categories c ON c.product_category_id = p.product_category_id
            WHERE t.effective_to IS NULL AND t.effective_from < :d ORDER BY t.product_id""", d=day.day):
        _, leaf = day.world.leaf_of_category[row["category_code"]]
        if leaf.gst_before != leaf.gst_after and row["gst_rate"] == leaf.gst_before:
            closing.append({"id": row["product_tax_rate_id"], "to": day.day - timedelta(days=1), "t": stamp})
            opening.append((row["product_id"], leaf.gst_after))
    if closing:
        day.execute("UPDATE product_tax_rates SET effective_to = :to, updated_at = :t WHERE product_tax_rate_id = :id",
                    closing)
    for product_id, new_rate in opening:
        day.writer.insert("product_tax_rates", {
            "product_tax_rate_id": day.ids.next("product_tax_rates"), "product_id": product_id, "gst_rate": new_rate,
            "effective_from": day.day, "effective_to": None, "change_reason": "GST_RATE_REVISION",
            "notification_reference": revision["notification_reference"], "created_at": stamp, "updated_at": stamp,
        })


def catalogue_step(day: DayContext) -> None:
    """New styles launch and old products are discontinued at the configured yearly rates (1B §3.3)."""
    settings = day.world.config.daily
    master = day.master()
    for pack in day.world.packs:
        rng = day.rng(f"catalogue.{pack.code}")
        active = day.world.active_products[pack.code]
        for _ in range(poisson(rng, len(active) * settings.new_products_per_year / 365)):
            leaf = rng.choices(pack.leaves, weights=[float(leaf.share) for leaf in pack.leaves])[0]
            counters = day.world.refs["catalogue_counters"][pack.code]
            created = create_style(master, rng, pack, leaf, day.day, 20, counters)
            approve_suppliers(master, rng, created)
            active.extend((p["product_id"], day.day) for p in created)
        retirees = poisson(rng, len(active) * settings.discontinued_per_year / 365)
        candidates = [p for p in active if (day.day - p[1]).days >= 365]       # nothing younger than a year
        for product in rng.sample(candidates, k=min(retirees, len(candidates))):
            day.writer.update("products", "product_id", product[0], {
                "product_status": "DISCONTINUED", "discontinued_date": day.day, "updated_at": day.at(9)})
            active.remove(product)


def campaigns_step(day: DayContext) -> None:
    """Campaigns are created ``campaign_lead_days`` ahead (PLANNED), may be cancelled in the last week,
    start (ACTIVE) and finish (COMPLETED) with the clock (1B §4.7)."""
    world = day.world
    rng = day.rng("campaigns")
    master = day.master()
    start = day.day + timedelta(days=world.config.daily.campaign_lead_days)
    owners = world.refs["employees_by_department"]["MKT"]
    for pack in world.packs:
        for event in pack.events:
            if (event.month, event.day) == (start.month, start.day):
                world.campaign_numbers[start.year] = world.campaign_numbers.get(start.year, 0) + 1
                create_campaign(master, rng, pack, event, start, "PLANNED", world.campaign_numbers[start.year], owners)
    stamp = day.at(8)
    cancel_share = world.config.master_data.campaign_cancelled_share
    for campaign in day.rows("SELECT marketing_campaign_id FROM marketing_campaigns WHERE campaign_status = 'PLANNED' "
                             "AND start_date BETWEEN :a AND :b ORDER BY marketing_campaign_id",
                             a=day.day + timedelta(days=1), b=day.day + timedelta(days=7)):
        if rng.random() < cancel_share / 7:
            day.execute("UPDATE marketing_campaigns SET campaign_status = 'CANCELLED', updated_at = :t "
                        "WHERE marketing_campaign_id = :c", {"t": stamp, "c": campaign["marketing_campaign_id"]})
    day.execute("UPDATE marketing_campaigns SET campaign_status = 'ACTIVE', updated_at = :t "
                "WHERE campaign_status = 'PLANNED' AND start_date <= :d", {"t": stamp, "d": day.day})
    day.execute("UPDATE marketing_campaigns SET campaign_status = 'COMPLETED', updated_at = :t "
                "WHERE campaign_status = 'ACTIVE' AND end_date < :d", {"t": stamp, "d": day.day})


# The fixed order of a simulated day (1G §2.2). Later steps insert themselves at their position.
DAY_STEPS = (
    ("calendar", calendar_step),
    ("periods", periods_step),
    ("opening", opening_step),                       # first simulated day only (P01, P02)
    ("gst_revision", gst_revision_step),
    ("catalogue", catalogue_step),
    ("campaigns", campaigns_step),
    ("customers", acquisition_step),                 # 3.8b new customers over time
    ("procurement", procurement_step),               # 3.7 procure-to-pay
    ("sales", sales_step),                           # 3.8 demand: orders, prepayments, credit holds
    ("fulfilment", fulfilment_step),                 # 3.9 handovers, dispatch, invoices, carriers
    ("collections", collections_step),               # 3.10 B2B payments, cheques, chargebacks
)
