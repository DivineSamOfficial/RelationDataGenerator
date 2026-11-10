"""Opening position on the workspace's first simulated day (1E P01, P02; D22, D34; step 3.6b).

* P01 opening journal (type OPENING): bank balances, petty-cash floats and fixed assets on the debit side;
  share capital, accumulated depreciation and the drawn cash-credit facility on the credit side; Opening
  Balance Equity balances it. Opening balances never live on master tables (D22).
* P02 opening stock (D34): one OPENING_STOCK adjustment per warehouse. DCs hold every product, stores a
  share of the consumer-facing range; quantities sit between the reorder point and the maximum, valued at
  the preferred supplier's reference cost. Each line gets its ledger row and, for batch-tracked products, a
  batch (the batch points to the line, D86). The ``inventory`` rows hold the resulting balances, and each
  adjustment posts Dr Inventory / Cr Opening Balance Equity.

The step runs on the first simulated day only, and only if no opening journal exists yet (state from the
database, D66), inside that day's transaction.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from scmgen.core.money import money, quantity
from scmgen.domain.costing import StockPosition
from scmgen.domain.journal import JournalBuilder
from scmgen.generation.engine.accounting import account_book, post_journal
from scmgen.generation.engine.day import DayContext
from scmgen.generation.numbering import Registration

MEDIUM_PRODUCTS = 2000            # the opening amounts in config are for the Medium preset


def opening_step(day: DayContext) -> None:
    if day.day != day.world.first_day:
        return
    if day.rows("SELECT 1 FROM journal_entries WHERE journal_type = 'OPENING' LIMIT 1"):
        return
    book = account_book(day)
    day.world.refs["account_book"] = book
    opening_journal(day, book)
    opening_stock(day, book)


def _scaled(day: DayContext, amount: int) -> Decimal:
    scale = Decimal(day.world.config.volumes.products) / MEDIUM_PRODUCTS
    return money(round(Decimal(amount) * scale))


def opening_journal(day: DayContext, book) -> None:
    """P01: what the company owned and owed when the simulated history starts."""
    settings = day.world.config.opening_balances
    builder = JournalBuilder("Opening balances at the start of the simulated history")
    for row in day.rows("SELECT b.bank_account_id, b.account_purpose, b.account_type, w.warehouse_type "
                        "FROM bank_accounts b LEFT JOIN warehouses w ON w.warehouse_id = b.warehouse_id "
                        "ORDER BY b.bank_account_id"):
        gl = book.bank(row["bank_account_id"])
        if row["account_type"] == "PETTY_CASH":
            builder.debit(gl, _scaled(day, settings.petty_cash[row["warehouse_type"]]))
        elif row["account_type"] == "CASH_CREDIT":
            builder.credit(gl, _scaled(day, settings.cash_credit_drawn))
        elif row["account_purpose"] in settings.bank_balances:
            builder.debit(gl, _scaled(day, settings.bank_balances[row["account_purpose"]]))
    asset_accounts = {r["account_code"]: r["account_id"] for r in day.rows(
        "SELECT account_code, account_id FROM chart_of_accounts WHERE is_postable")}
    fixed_total = Decimal(0)
    for code, cost in sorted(settings.fixed_assets.items()):
        if code not in asset_accounts:
            raise RuntimeError(f"opening_balances.fixed_assets: no postable account {code}")
        amount = _scaled(day, cost)
        fixed_total += amount
        builder.debit(asset_accounts[code], amount)
    builder.credit(book.role("ACCUMULATED_DEPRECIATION"),
                   money(fixed_total * Decimal(str(settings.accumulated_depreciation_share))))
    builder.credit(book.role("SHARE_CAPITAL"), _scaled(day, settings.share_capital))
    builder.balance_with(book.role("OPENING_BALANCE_EQUITY"))      # the difference, on whichever side balances
    post_journal(day, builder.build(), source="OPENING_BALANCE", journal_type="OPENING")


def opening_stock(day: DayContext, book) -> None:
    """P02: one OPENING_STOCK adjustment per warehouse, with lines, batches, ledger rows, balances, journal."""
    world = day.world
    rng = day.rng("opening_stock")
    stamp = day.at(6)
    consumer_packs = {p.code for p in world.packs if p.b2b_sales_share < 1}
    pack_of_bu = {bu: code for code, bu in world.refs["business_unit"].items()}
    products = day.rows("""
        SELECT p.product_id, p.business_unit_id, p.reorder_level, p.reorder_quantity, p.is_batch_tracked,
               p.shelf_life_days, sp.reference_unit_cost
        FROM products p JOIN supplier_products sp ON sp.product_id = p.product_id AND sp.is_preferred
             AND sp.supplier_product_status = 'ACTIVE' AND sp.deleted_at IS NULL
        WHERE p.product_status = 'ACTIVE' AND p.launch_date < :d ORDER BY p.product_id""", d=day.day)
    warehouses = day.rows("""
        SELECT w.warehouse_id, w.warehouse_code, w.warehouse_type, w.company_gst_registration_id, s.state_code
        FROM warehouses w JOIN states s ON s.state_id = w.state_id
        WHERE w.warehouse_status = 'ACTIVE' ORDER BY w.warehouse_id""")
    managers = {r["warehouse_id"]: r["employee_id"] for r in day.rows(
        "SELECT warehouse_id, MIN(employee_id) AS employee_id FROM employees WHERE job_level = 'MANAGER' "
        "AND warehouse_id IS NOT NULL GROUP BY warehouse_id")}
    locations: dict[int, list[int]] = defaultdict(list)
    for row in day.rows("SELECT warehouse_location_id, warehouse_id FROM warehouse_locations "
                        "WHERE zone IN ('STORAGE', 'SALES_FLOOR') AND is_active ORDER BY warehouse_location_id"):
        locations[row["warehouse_id"]].append(row["warehouse_location_id"])
    share = world.config.opening_balances.store_assortment_share

    for warehouse in warehouses:
        is_dc = warehouse["warehouse_type"] == "DISTRIBUTION_CENTRE"
        assortment = [p for p in products if is_dc or (pack_of_bu[p["business_unit_id"]] in consumer_packs
                                                       and rng.random() < share)]
        if not assortment:
            continue
        adjustment_id = day.ids.next("stock_adjustments")
        registration = Registration(warehouse["company_gst_registration_id"], warehouse["state_code"])
        day.writer.insert("stock_adjustments", {
            "stock_adjustment_id": adjustment_id,
            "adjustment_number": day.numbers.next(day.conn, "ADJ", day.day, registration),
            "warehouse_id": warehouse["warehouse_id"], "adjustment_date": day.day, "adjustment_type": "OPENING_STOCK",
            "reason": "Opening stock at the start of the simulated history", "adjustment_status": "POSTED",
            "created_by_employee_id": managers[warehouse["warehouse_id"]], "approved_by_employee_id": None,
            "posted_at": stamp, "created_at": stamp, "updated_at": stamp,
        })
        total = Decimal(0)
        for number, product in enumerate(assortment, 1):
            level, reorder = int(product["reorder_level"]), int(product["reorder_quantity"])
            if not is_dc:
                level, reorder = max(1, level // 4), max(1, reorder // 4)
            qty = quantity(level + rng.randint(1, reorder))
            position, movement = StockPosition().receive(qty, product["reference_unit_cost"])
            total += movement.value_change
            line_id = day.ids.next("stock_adjustment_line_items")
            day.writer.insert("stock_adjustment_line_items", {
                "stock_adjustment_line_item_id": line_id, "stock_adjustment_id": adjustment_id,
                "line_number": number, "product_id": product["product_id"], "inventory_batch_id": None,
                "stock_status": "SELLABLE", "system_quantity": None, "counted_quantity": None,
                "quantity_change": qty, "unit_cost": movement.unit_cost, "value_change": movement.value_change,
                "reason_code": "OPENING", "created_at": stamp, "updated_at": stamp,
            })
            batch_id = None
            if product["is_batch_tracked"]:
                batch_id = day.ids.next("inventory_batches")
                shelf = product["shelf_life_days"] or 365
                made = day.day - timedelta(days=rng.randint(5, max(6, shelf // 3)))
                day.writer.insert("inventory_batches", {
                    "inventory_batch_id": batch_id,
                    "batch_number": f"LOT-OPN-{warehouse['warehouse_code']}-{product['product_id']:06d}",
                    "product_id": product["product_id"], "goods_receipt_line_item_id": None,
                    "stock_adjustment_line_item_id": line_id, "supplier_batch_number": None,
                    "manufacture_date": made, "expiry_date": made + timedelta(days=shelf), "received_date": day.day,
                    "initial_quantity": qty, "created_at": stamp, "updated_at": stamp,
                })
            day.writer.insert("inventory_transactions", {
                "inventory_transaction_id": day.ids.next("inventory_transactions"), "transaction_at": stamp,
                "warehouse_id": warehouse["warehouse_id"], "product_id": product["product_id"],
                "inventory_batch_id": batch_id,
                "warehouse_location_id": locations[warehouse["warehouse_id"]][number % len(locations[warehouse["warehouse_id"]])],
                "transaction_type": "ADJUSTMENT", "stock_status": "SELLABLE", "quantity": qty,
                "unit_cost": movement.unit_cost, "value_change": movement.value_change,
                "goods_receipt_line_item_id": None, "shipment_line_item_id": None, "invoice_line_item_id": None,
                "sales_return_line_item_id": None, "return_inspection_id": None, "stock_transfer_line_item_id": None,
                "stock_adjustment_line_item_id": line_id, "defective_stock_id": None,
                "reversal_of_inventory_transaction_id": None, "generation_run_id": day.run_id,
                "created_at": stamp, "updated_at": stamp,
            })
            day.writer.insert("inventory", {
                "inventory_id": day.ids.next("inventory"), "warehouse_id": warehouse["warehouse_id"],
                "product_id": product["product_id"], "qty_sellable": position.quantity, "qty_quarantine": Decimal(0),
                "qty_defective": Decimal(0), "qty_reserved": Decimal(0), "stock_value": position.value,
                "last_unit_cost": movement.unit_cost, "reorder_level": quantity(level), "reorder_quantity": quantity(reorder),
                "max_stock_level": quantity(level + reorder), "is_stocked": True, "last_receipt_at": None,
                "last_issue_at": None, "last_counted_at": None, "created_at": stamp, "updated_at": stamp,
            })
        journal = (JournalBuilder(f"Opening stock {warehouse['warehouse_code']}")
                   .debit(book.role("INVENTORY"), total, warehouse_id=warehouse["warehouse_id"])
                   .credit(book.role("OPENING_BALANCE_EQUITY"), total)
                   .build())
        post_journal(day, journal, source="STOCK_ADJUSTMENT", stock_adjustment_id=adjustment_id,
                     created_by=managers[warehouse["warehouse_id"]])
