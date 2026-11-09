"""A small, internally consistent dataset that puts at least one valid row in every one of the 92 tables.

Phase 2 step 2.6. Inserting it is itself the positive test for every CHECK / FK / UNIQUE in the schema:
each constraint must ACCEPT valid business data. The constraint tests then mutate these rows to prove
each rule REJECTS bad data. Phase 3's generator tests can reuse it as a known-good fixture.

The story (April 2026, FY 2026-27, Karnataka DC in Bengaluru):
  supplier invited to an RFQ -> quotes -> awarded PO -> delivers 100 earphones -> inspected -> invoiced -> paid
  B2B customer orders 10 -> reserved -> shipped -> invoiced -> paid -> 2 returned (1 sellable, 1 defective,
  scrapped) -> credit note -> refund; a B2C order priced GST-inclusive; rent bill paid; GSTR-3B filed;
  bank reconciled; the sales invoice journal posted; AR / AP / tax subledgers filled.
Amounts follow the Part 1C §2.3 formulas exactly (D84), computed with Python Decimal ROUND_HALF_UP.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Connection

CENT = Decimal("0.01")
FOUR = Decimal("0.0001")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_HALF_UP)


def hex64(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


class BaselineBuilder:
    """Inserts the baseline dataset; `ids` maps a short name to each inserted row's primary key."""

    def __init__(self, conn: Connection) -> None:
        self.cursor = conn.connection.cursor()
        self.ids: dict[str, int] = {}

    def insert(self, key: str, table: str, **values: Any) -> int:
        columns = ", ".join(values)
        placeholders = ", ".join(["%s"] * len(values))
        self.cursor.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(values.values()))
        self.ids[key] = self.cursor.lastrowid
        return self.cursor.lastrowid

    def build(self) -> dict[str, int]:  # noqa: PLR0915 - one linear story, deliberately explicit
        i = self.insert
        ids = self.ids

        # --- reference data (no foreign keys) -----------------------------------------------
        i("state_ka", "states", gst_state_code="29", state_code="KA", state_name="Karnataka", region="SOUTH")
        i("state_mh", "states", gst_state_code="27", state_code="MH", state_name="Maharashtra", region="WEST")
        for code, balance, statement, prefix, order in [
            ("ASSET", "DEBIT", "BALANCE_SHEET", "1", 1), ("LIABILITY", "CREDIT", "BALANCE_SHEET", "2", 2),
            ("EQUITY", "CREDIT", "BALANCE_SHEET", "3", 3), ("REVENUE", "CREDIT", "PROFIT_AND_LOSS", "4", 4),
            ("COGS", "DEBIT", "PROFIT_AND_LOSS", "5", 5), ("EXPENSE", "DEBIT", "PROFIT_AND_LOSS", "6", 6),
        ]:
            i(f"type_{code.lower()}", "account_types", account_type_code=code, account_type_name=code.title(),
              normal_balance=balance, financial_statement=statement, code_prefix=prefix, display_order=order)
        i("dept_fin", "departments", department_code="FIN", department_name="Finance & Accounts")
        i("uom_pcs", "product_units_of_measure", uom_code="PCS", uom_name="Pieces", uom_category="COUNT",
          decimal_places=0, gst_uqc="NOS")
        i("brand", "product_brands", brand_code="VOLTRIX", brand_name="Voltrix", brand_tier="MID")
        i("method_neft", "payment_methods", method_code="NEFT", method_name="NEFT", is_electronic=True,
          usable_for_receipts=True, usable_for_payments=True, settlement_lag_days=0)
        i("term_net30", "payment_terms", term_code="NET30", term_name="Net 30 days", due_days=30)
        i("channel_web", "sales_channels", channel_code="WEBSITE", channel_name="Website", is_online=True,
          requires_shipment=True, allowed_customer_type="BOTH")
        i("mkt_insta", "marketing_channels", channel_code="INSTAGRAM", channel_name="Instagram",
          channel_group="PAID_SOCIAL", is_paid=True, supports_campaigns=True)

        # --- company, accounts, GST registrations -------------------------------------------
        i("company", "companies", company_code="kaveri", legal_name="Kaveri Nexus Distribution Pvt. Ltd.",
          trade_name="Kaveri Nexus", cin="U51909KA2019PTC128734", pan="ZZXCK4821M", registered_state_id=ids["state_ka"],
          registered_address_line1="12 Residency Road", registered_city="Bengaluru", registered_pincode="560025",
          incorporation_date="2019-06-01", email="accounts@kaverinexus.example", phone="9845012345")

        def account(key, code, name, type_key, level=1, parent=None, postable=True, balance=None, role=None,
                    subledger=None, cost_center=False, cash_flow="OPERATING"):
            type_id = ids[type_key]
            normal = balance or ("DEBIT" if type_key in ("type_asset", "type_cogs", "type_expense") else "CREDIT")
            i(key, "chart_of_accounts", account_code=code, account_name=name, account_type_id=type_id,
              parent_account_id=ids[parent] if parent else None, account_level=level, is_postable=postable,
              normal_balance=normal, system_role=role, is_control_account=subledger is not None,
              control_subledger=subledger, requires_cost_center=cost_center, cash_flow_category=cash_flow)

        account("acc_current_assets", "10000", "Current Assets", "type_asset", postable=False)
        account("acc_ar", "11000", "Trade Receivables", "type_asset", 2, "acc_current_assets", role="AR_CONTROL", subledger="AR")
        account("acc_bank", "10110", "Bank - Collections", "type_asset", 2, "acc_current_assets", subledger="BANK", cash_flow="CASH")
        account("acc_input_cgst", "13110", "Input CGST - Karnataka", "type_asset", 2, "acc_current_assets", subledger="GST")
        for bu, digit in (("elec", "1"), ("home", "2")):
            account(f"acc_sales_{bu}", f"410{digit}0", f"Sales - {bu}", "type_revenue", cost_center=True, cash_flow="NOT_APPLICABLE")
            account(f"acc_returns_{bu}", f"420{digit}0", f"Sales Returns - {bu}", "type_revenue", balance="DEBIT",
                    cost_center=True, cash_flow="NOT_APPLICABLE")
            account(f"acc_cogs_{bu}", f"510{digit}0", f"COGS - {bu}", "type_cogs", cost_center=True, cash_flow="NOT_APPLICABLE")
        account("acc_rent", "60100", "Rent", "type_expense", cost_center=True, cash_flow="NOT_APPLICABLE")

        i("reg_ka", "company_gst_registrations", company_id=ids["company"], state_id=ids["state_ka"],
          gstin="29ZZXCK4821M1Z7", registration_date="2019-07-01", is_principal_place=True)
        i("reg_mh", "company_gst_registrations", company_id=ids["company"], state_id=ids["state_mh"],
          gstin="27ZZXCK4821M1Z3", registration_date="2020-01-01")
        for bu, name, pack in (("elec", "Consumer Electronics", "electronics"), ("home", "Home & Kitchen", "home_kitchen")):
            i(f"bu_{bu}", "business_units", business_unit_code=bu.upper(), business_unit_name=name, industry_pack_code=pack,
              revenue_account_id=ids[f"acc_sales_{bu}"], sales_returns_account_id=ids[f"acc_returns_{bu}"],
              cogs_account_id=ids[f"acc_cogs_{bu}"], launched_date="2019-07-01")
        i("cat_rent", "expense_categories", category_code="RENT", category_name="Rent", category_group="OCCUPANCY",
          gl_account_id=ids["acc_rent"], default_gst_rate=18, is_itc_eligible=True, recurrence="MONTHLY", payee_type="SUPPLIER")
        i("tax_acc", "tax_accounts", company_gst_registration_id=ids["reg_ka"], tax_component="CGST",
          tax_direction="INPUT", gl_account_id=ids["acc_input_cgst"])

        # --- locations, organisation -----------------------------------------------------------
        i("wh_dc", "warehouses", warehouse_code="DC-BLR", warehouse_name="Bengaluru DC", warehouse_type="DISTRIBUTION_CENTRE",
          company_gst_registration_id=ids["reg_ka"], state_id=ids["state_ka"], address_line1="Plot 7, Hoskote Industrial Area",
          city="Bengaluru", pincode="562114", latitude=Decimal("12.971599"), longitude=Decimal("77.594566"),
          storage_area_sqft=120000, opened_date="2019-07-01")
        i("wh_store", "warehouses", warehouse_code="ST-BLR-01", warehouse_name="Indiranagar Store", warehouse_type="RETAIL_STORE",
          company_gst_registration_id=ids["reg_ka"], state_id=ids["state_ka"], address_line1="100 Feet Road",
          city="Bengaluru", pincode="560038", latitude=Decimal("12.978400"), longitude=Decimal("77.640800"),
          storage_area_sqft=3000, replenishment_warehouse_id=ids["wh_dc"], opened_date="2021-01-15")
        i("loc_rcv", "warehouse_locations", warehouse_id=ids["wh_dc"], location_code="RCV-01", zone="RECEIVING", location_type="FLOOR")
        i("bank", "bank_accounts", account_code="BNK-COLL-01", account_name="Collections", account_type="CURRENT",
          account_purpose="COLLECTIONS", bank_name="Deccan Commercial Bank", branch_name="MG Road", ifsc_code="DCBK0004127",
          account_number="501000123456", currency_code="INR", gl_account_id=ids["acc_bank"], opened_date="2019-07-01")
        i("cc_corp", "cost_centers", cost_center_code="CORP", cost_center_name="Corporate", cost_center_type="CORPORATE")
        i("cc_elec", "cost_centers", cost_center_code="BU-ELEC", cost_center_name="BU Electronics", cost_center_type="BUSINESS_UNIT",
          business_unit_id=ids["bu_elec"], parent_cost_center_id=ids["cc_corp"])
        i("emp", "employees", employee_code="EMP-00001", first_name="Asha", last_name="Rao", email="asha.rao@kaverinexus.example",
          phone="9900011122", department_id=ids["dept_fin"], cost_center_id=ids["cc_corp"], job_title="Finance Head",
          job_level="HEAD", approval_limit=10_000_000, monthly_gross_salary=250_000, hire_date="2019-07-01")
        i("period", "accounting_periods", fiscal_year=2026, fiscal_year_label="2026-27", period_number=1, period_code="2026-04",
          period_name="Apr 2026", start_date="2026-04-01", end_date="2026-04-30", period_status="OPEN",
          opened_at="2026-04-01 00:00:00")

        # --- catalogue ------------------------------------------------------------------------------
        i("cat_audio", "product_categories", category_code="ELEC-AUD", category_name="Audio", business_unit_id=ids["bu_elec"],
          category_level=1, is_leaf=False, return_window_days=7)
        i("cat_earphones", "product_categories", category_code="ELEC-AUD-EAR", category_name="Earphones",
          parent_category_id=ids["cat_audio"], business_unit_id=ids["bu_elec"], category_level=2, is_leaf=True, return_window_days=7)
        i("cat_kitchen", "product_categories", category_code="HOME-KIT", category_name="Kitchen", business_unit_id=ids["bu_home"],
          category_level=1, is_leaf=True, return_window_days=10)
        i("product", "products", sku="ELEC-EAR-0001", product_name="Voltrix Wireless Earphones", business_unit_id=ids["bu_elec"],
          product_category_id=ids["cat_earphones"], product_brand_id=ids["brand"], base_unit_of_measure_id=ids["uom_pcs"],
          hsn_code="85183000", list_price=1000, mrp=1500, weight_kg=Decimal("0.150"), volume_litres=Decimal("0.300"),
          warranty_months=12, reorder_level=10, reorder_quantity=50, launch_date="2025-01-01")
        i("tax_rate", "product_tax_rates", product_id=ids["product"], gst_rate=18, effective_from="2025-01-01", change_reason="INITIAL")

        # --- parties ----------------------------------------------------------------------------------
        i("supplier", "suppliers", supplier_code="SUP-00001", supplier_name="Shree Ganesh Electronics", supplier_type="GOODS",
          gst_registration_type="REGISTERED", gstin="29ZZXPA1234B1Z5", pan="ZZXPA1234B", payment_term_id=ids["term_net30"],
          contact_person_name="Ravi Kumar", email="sales@shreeganesh.example", phone="9811122233", supplier_rating=Decimal("4.20"),
          onboarded_date="2020-02-01")
        i("supplier_svc", "suppliers", supplier_code="SUP-00002", supplier_name="Swiftline Logistics", supplier_type="SERVICES",
          gst_registration_type="REGISTERED", gstin="29ZZXPQ9999Z1Z1", pan="ZZXPQ9999Z", payment_term_id=ids["term_net30"],
          default_expense_category_id=ids["cat_rent"], contact_person_name="Meena Iyer", email="billing@swiftline.example",
          phone="9822233344", supplier_rating=Decimal("3.90"), onboarded_date="2020-03-01")
        i("supplier_addr", "supplier_addresses", supplier_id=ids["supplier"], address_type="REGISTERED", is_default=True,
          address_line1="44 SP Road", city="Bengaluru", state_id=ids["state_ka"], pincode="560002", valid_from="2020-02-01")
        i("supplier_product", "supplier_products", supplier_id=ids["supplier"], product_id=ids["product"],
          reference_unit_cost=500, minimum_order_quantity=10, lead_time_days=7, is_preferred=True, approved_date="2025-01-01")
        i("carrier", "shipping_carriers", carrier_code="SWIFT", carrier_name="Swiftline Express", carrier_type="EXPRESS_COURIER",
          supplier_id=ids["supplier_svc"], supports_cod=True, supports_reverse_pickup=True, tracking_number_prefix="SWF")
        i("campaign", "marketing_campaigns", campaign_code="CMP-2026-001", campaign_name="Summer Audio - Instagram",
          marketing_channel_id=ids["mkt_insta"], business_unit_id=ids["bu_elec"], target_customer_type="ALL",
          season_tag="SUMMER", start_date="2026-04-01", end_date="2026-04-30", budget_amount=100000,
          owner_employee_id=ids["emp"], campaign_status="ACTIVE")
        i("customer", "customers", customer_code="CUST-000001", customer_type="B2B", customer_segment="RETAILER",
          customer_name="Metro Mobiles", email="buy@metromobiles.example", phone="9845098450", gst_registration_type="REGISTERED",
          gstin="29ZZXPC5678D1Z9", pan="ZZXPC5678D", payment_term_id=ids["term_net30"], credit_limit=500000,
          sales_employee_id=ids["emp"], acquisition_marketing_channel_id=ids["mkt_insta"], acquisition_date="2025-06-01")
        i("customer_b2c", "customers", customer_code="CUST-000002", customer_type="B2C", customer_segment="CONSUMER",
          customer_name="Priya Sharma", email="priya@mail.example", phone="9876543210", gst_registration_type="CONSUMER",
          payment_term_id=ids["term_net30"], acquisition_marketing_channel_id=ids["mkt_insta"],
          acquisition_marketing_campaign_id=ids["campaign"], acquisition_date="2026-04-05")
        i("addr", "customer_addresses", customer_id=ids["customer"], address_label="Shop", is_billing_address=True,
          is_shipping_address=True, is_default_billing=True, is_default_shipping=True, address_line1="5 Brigade Road",
          city="Bengaluru", state_id=ids["state_ka"], pincode="560001", valid_from="2025-06-01")
        i("addr_b2c", "customer_addresses", customer_id=ids["customer_b2c"], address_label="Home", is_billing_address=True,
          is_shipping_address=True, is_default_billing=True, is_default_shipping=True, address_line1="22 Koramangala 4th Block",
          city="Bengaluru", state_id=ids["state_ka"], pincode="560034", valid_from="2026-04-05")

        # --- system (needed by the ledger and journals) ---------------------------------------
        i("run", "generation_runs", run_number=1, build_id=str(uuid.UUID(int=1)), run_mode="HISTORICAL", triggered_by="CLI",
          base_seed=42, config_snapshot="{}", config_hash=hex64("config"), app_version="0.1.0", sim_start_date="2026-04-01",
          sim_end_date="2026-04-30", committed_through="2026-04-30 23:59:59.999999", run_status="COMPLETED",
          started_at="2026-11-10 09:00:00", finished_at="2026-11-10 09:05:00", rows_written=1000,
          validation_status="PASSED", validation_report_path="data/reports/kaveri/run_1_validation.json")
        i("run_step", "generation_run_steps", generation_run_id=ids["run"], step_name="DAILY_SIMULATION", step_order=5,
          step_status="COMPLETED", planned_units=30, completed_units=30, generated_rows=1000, current_sim_date="2026-04-30",
          rows_per_second=Decimal("250.00"), rows_by_table='{"orders": 2}', started_at="2026-11-10 09:00:30",
          finished_at="2026-11-10 09:04:30")
        i("seq_invoice", "document_sequences", document_type="TAX_INVOICE", company_gst_registration_id=ids["reg_ka"],
          fiscal_year=2026, last_sequence=1)
        i("seq_jv", "document_sequences", document_type="JV", fiscal_year=2026, last_sequence=1)

        # --- procurement (100 earphones @ 500, 18% intra-state) -------------------------------
        qty, price, rate = Decimal(100), Decimal(500), Decimal(18)
        taxable = money(qty * price)                 # 50000.00
        half = money(taxable * rate / 200)           # 4500.00
        po_total = taxable + 2 * half                # 59000.00
        i("rfq", "rfq_headers", rfq_number="RFQ/KA/2026-27/000001", rfq_date="2026-04-02", rfq_type="REPLENISHMENT",
          delivery_warehouse_id=ids["wh_dc"], business_unit_id=ids["bu_elec"], response_due_date="2026-04-05",
          required_by_date="2026-04-12", rfq_status="AWARDED", created_by_employee_id=ids["emp"], awarded_at="2026-04-06 10:00:00")
        i("rfq_line", "rfq_line_items", rfq_id=ids["rfq"], line_number=1, product_id=ids["product"], requested_quantity=qty,
          line_status="AWARDED")
        i("rfq_supplier", "rfq_suppliers", rfq_id=ids["rfq"], supplier_id=ids["supplier"], invited_date="2026-04-02",
          response_status="QUOTED", responded_date="2026-04-04")
        i("rfq_history", "rfq_status_history", rfq_id=ids["rfq"], to_status="DRAFT", changed_at="2026-04-02 09:00:00")
        header_amounts = dict(taxable_amount=taxable, cgst_amount=half, sgst_amount=half, igst_amount=0,
                              round_off_amount=0, total_amount=po_total)
        line_amounts = dict(unit_price=price, discount_percent=0, taxable_amount=taxable, gst_rate=rate,
                            cgst_amount=half, sgst_amount=half, igst_amount=0, line_total=po_total)
        i("quotation", "quotation_headers", quotation_number="QTN/KA/2026-27/000001", rfq_supplier_id=ids["rfq_supplier"],
          supplier_quotation_reference="SGE-Q-778", quotation_date="2026-04-04", valid_until_date="2026-04-30",
          payment_term_id=ids["term_net30"], is_inter_state=False, evaluation_score=Decimal("86.50"),
          evaluated_by_employee_id=ids["emp"], evaluated_date="2026-04-06", quotation_status="ACCEPTED", **header_amounts)
        i("quotation_line", "quotation_line_items", quotation_id=ids["quotation"], line_number=1, rfq_line_item_id=ids["rfq_line"],
          quoted_quantity=qty, lead_time_days=7, is_awarded=True, **line_amounts)
        i("quotation_history", "quotation_status_history", quotation_id=ids["quotation"], to_status="RECEIVED",
          changed_at="2026-04-04 15:00:00")
        i("po", "purchase_order_headers", po_number="PO/KA/2026-27/000001", po_date="2026-04-06", po_source="RFQ_AWARD",
          quotation_id=ids["quotation"], supplier_id=ids["supplier"], supplier_address_id=ids["supplier_addr"],
          delivery_warehouse_id=ids["wh_dc"], payment_term_id=ids["term_net30"], is_inter_state=False,
          expected_delivery_date="2026-04-13", po_status="RECEIVED", created_by_employee_id=ids["emp"],
          approved_by_employee_id=ids["emp"], approved_at="2026-04-06 12:00:00", **header_amounts)
        i("po_line", "purchase_order_line_items", purchase_order_id=ids["po"], line_number=1, product_id=ids["product"],
          quotation_line_item_id=ids["quotation_line"], ordered_quantity=qty, expected_delivery_date="2026-04-13",
          quantity_received=qty, quantity_accepted=qty, quantity_invoiced=qty, line_status="RECEIVED", **line_amounts)
        i("po_history", "purchase_order_status_history", purchase_order_id=ids["po"], to_status="DRAFT",
          changed_at="2026-04-06 10:00:00")
        i("grn", "goods_receipts", grn_number="GRN/KA/2026-27/000001", purchase_order_id=ids["po"], warehouse_id=ids["wh_dc"],
          receipt_date="2026-04-10", received_at="2026-04-10 11:00:00", supplier_delivery_note_number="DN-5521",
          eway_bill_number="341234567890", vehicle_number="KA01AB1234", received_by_employee_id=ids["emp"], grn_status="INSPECTED")
        i("grn_line", "goods_receipt_line_items", goods_receipt_id=ids["grn"], line_number=1,
          purchase_order_line_item_id=ids["po_line"], quantity_received=qty, receiving_location_id=ids["loc_rcv"])
        i("inspection", "goods_inspections", goods_receipt_line_item_id=ids["grn_line"], inspected_at="2026-04-10 12:00:00",
          inspected_by_employee_id=ids["emp"], quantity_accepted=qty, quantity_rejected=0, inspection_result="ACCEPTED")
        i("supplier_invoice", "supplier_invoices", internal_reference="PINV/KA/2026-27/000001", supplier_id=ids["supplier"],
          supplier_invoice_number="SGE/26-27/0411", purchase_order_id=ids["po"], company_gst_registration_id=ids["reg_ka"],
          supplier_gstin="29ZZXPA1234B1Z5", invoice_date="2026-04-10", received_date="2026-04-11", due_date="2026-05-10",
          is_inter_state=False, match_status="MATCHED", invoice_status="APPROVED", approved_by_employee_id=ids["emp"],
          approved_at="2026-04-12 10:00:00", **header_amounts)
        i("supplier_invoice_line", "supplier_invoice_line_items", supplier_invoice_id=ids["supplier_invoice"], line_number=1,
          purchase_order_line_item_id=ids["po_line"], invoiced_quantity=qty, po_unit_price=price, **line_amounts)
        i("supplier_payment", "supplier_payments", payment_number="SPAY/KA/2026-27/000001", supplier_id=ids["supplier"],
          payment_date="2026-04-30", bank_account_id=ids["bank"], payment_method_id=ids["method_neft"], amount=po_total,
          payment_type="AGAINST_INVOICE", bank_reference="UTR0000001", payment_status="COMPLETED",
          approved_by_employee_id=ids["emp"], processed_at="2026-04-30 10:00:00")

        # --- inventory ------------------------------------------------------------------------------
        i("adjustment", "stock_adjustments", adjustment_number="ADJ/KA/2026-27/000001", warehouse_id=ids["wh_dc"],
          adjustment_date="2026-04-01", adjustment_type="OPENING_STOCK", adjustment_status="POSTED",
          created_by_employee_id=ids["emp"], posted_at="2026-04-01 09:00:00")
        i("adjustment_line", "stock_adjustment_line_items", stock_adjustment_id=ids["adjustment"], line_number=1,
          product_id=ids["product"], stock_status="SELLABLE", quantity_change=5, unit_cost=500, value_change=2500,
          reason_code="OPENING")
        i("batch", "inventory_batches", batch_number="LOT-OPEN-0001", product_id=ids["product"],
          stock_adjustment_line_item_id=ids["adjustment_line"], received_date="2026-04-01", initial_quantity=5)
        i("inventory_dc", "inventory", warehouse_id=ids["wh_dc"], product_id=ids["product"], qty_sellable=91,
          stock_value=45500, last_unit_cost=500, reorder_level=10, reorder_quantity=50, max_stock_level=200,
          last_receipt_at="2026-04-10 12:00:00", last_issue_at="2026-04-13 12:00:00")
        i("inventory_store", "inventory", warehouse_id=ids["wh_store"], product_id=ids["product"], qty_sellable=5,
          stock_value=2500, last_unit_cost=500, reorder_level=2, reorder_quantity=5, max_stock_level=10)
        i("transfer", "stock_transfers", transfer_number="STO/KA/2026-27/000001", transfer_type="STORE_REPLENISHMENT",
          source_warehouse_id=ids["wh_dc"], destination_warehouse_id=ids["wh_store"], is_inter_gstin=False,
          requested_date="2026-04-11", dispatched_at="2026-04-11 10:00:00", received_at="2026-04-11 16:00:00",
          taxable_amount=2500, transfer_status="RECEIVED", created_by_employee_id=ids["emp"],
          approved_by_employee_id=ids["emp"], received_by_employee_id=ids["emp"])
        i("transfer_line", "stock_transfer_line_items", stock_transfer_id=ids["transfer"], line_number=1, product_id=ids["product"],
          stock_status="SELLABLE", quantity_requested=5, quantity_shipped=5, quantity_received_good=5, unit_cost=500,
          transfer_value=2500)

        # --- sales: B2B order of 10 @ 1000 (ex-GST) and a B2C order @ 499 GST-inclusive (D79) --
        o_qty, o_price = Decimal(10), Decimal(1000)
        o_taxable = money(o_qty * o_price)                   # 10000.00
        o_tax = money(o_taxable * rate / 100)                # 1800.00
        o_half = money(o_taxable * rate / 200)               # 900.00
        o_total = o_taxable + o_tax                          # 11800.00
        i("order", "orders", order_number="SO/KA/2026-27/000001", customer_id=ids["customer"],
          order_placed_at="2026-04-12 10:00:00", order_date="2026-04-12", sales_channel_id=ids["channel_web"],
          marketing_channel_id=ids["mkt_insta"], fulfilment_type="SHIP", billing_address_id=ids["addr"],
          shipping_address_id=ids["addr"], payment_term_id=ids["term_net30"], sales_employee_id=ids["emp"],
          prices_include_tax=False, items_taxable_amount=o_taxable, items_tax_amount=o_tax, order_total_estimate=o_total,
          order_status="DELIVERED")
        i("order_line", "order_line_items", order_id=ids["order"], line_number=1, product_id=ids["product"],
          quantity_ordered=o_qty, list_price=o_price, unit_price=o_price, taxable_amount=o_taxable, gst_rate=rate,
          tax_amount=o_tax, line_total=o_total, quantity_shipped=o_qty, quantity_delivered=o_qty, quantity_invoiced=o_qty,
          quantity_return_requested=2, line_status="DELIVERED")
        incl = Decimal(499)
        b2c_unit = (incl / (1 + rate / 100)).quantize(FOUR, ROUND_HALF_UP)        # 422.8814
        b2c_taxable = money(1 * incl / (1 + rate / 100))                           # 422.88
        b2c_tax = money(b2c_taxable * rate / 100)                                  # 76.12
        i("order_b2c", "orders", order_number="SO/KA/2026-27/000002", customer_id=ids["customer_b2c"],
          order_placed_at="2026-04-14 20:15:00", order_date="2026-04-14", sales_channel_id=ids["channel_web"],
          marketing_channel_id=ids["mkt_insta"], marketing_campaign_id=ids["campaign"], fulfilment_type="SHIP",
          billing_address_id=ids["addr_b2c"], shipping_address_id=ids["addr_b2c"], payment_term_id=ids["term_net30"],
          prices_include_tax=True, items_taxable_amount=b2c_taxable, items_tax_amount=b2c_tax,
          order_total_estimate=b2c_taxable + b2c_tax, order_status="CONFIRMED")
        i("order_line_b2c", "order_line_items", order_id=ids["order_b2c"], line_number=1, product_id=ids["product"],
          quantity_ordered=1, list_price=o_price, unit_price=b2c_unit, inclusive_unit_price=incl, taxable_amount=b2c_taxable,
          gst_rate=rate, tax_amount=b2c_tax, line_total=b2c_taxable + b2c_tax)
        i("order_history", "order_status_history", order_id=ids["order"], to_status="CONFIRMED", changed_at="2026-04-12 10:00:00")
        i("reservation", "inventory_reservations", order_line_item_id=ids["order_line"], warehouse_id=ids["wh_dc"],
          reserved_quantity=o_qty, fulfilled_quantity=o_qty, reserved_at="2026-04-12 10:01:00", closed_at="2026-04-13 12:00:00",
          reservation_status="FULFILLED")
        i("payment", "customer_payments", payment_number="RCPT/KA/2026-27/000001", customer_id=ids["customer"],
          payment_type="INVOICE_PAYMENT", initiated_at="2026-04-28 10:00:00", payment_date="2026-04-28",
          payment_method_id=ids["method_neft"], bank_account_id=ids["bank"], amount=o_total, gateway_reference="UTR0000002",
          payment_status="SUCCESS", completed_at="2026-04-28 10:05:00", settlement_date="2026-04-28")
        i("shipment", "shipments", shipment_number="SHP/KA/2026-27/000001", order_id=ids["order"], warehouse_id=ids["wh_dc"],
          shipping_address_id=ids["addr"], shipping_carrier_id=ids["carrier"], tracking_number="SWF000000001", package_count=1,
          total_weight_kg=Decimal("1.500"), freight_cost_amount=120, planned_dispatch_date="2026-04-13",
          dispatched_at="2026-04-13 12:00:00", expected_delivery_date="2026-04-15", delivered_at="2026-04-15 15:00:00",
          shipment_status="DELIVERED")
        i("shipment_line", "shipment_line_items", shipment_id=ids["shipment"], line_number=1, order_line_item_id=ids["order_line"],
          quantity_shipped=o_qty)
        i("shipment_history", "shipment_status_history", shipment_id=ids["shipment"], to_status="PENDING",
          changed_at="2026-04-12 11:00:00")
        i("tracking", "shipment_tracking", shipment_id=ids["shipment"], carrier_event_id="SWF-EVT-0000001", event_sequence=1,
          event_code="PICKED_UP", event_description="Picked up from Bengaluru DC", event_at="2026-04-13 12:30:00",
          received_at="2026-04-13 12:45:00", location_city="Bengaluru", state_id=ids["state_ka"],
          created_at="2026-04-13 12:45:00", updated_at="2026-04-13 12:45:00")
        i("attempt", "delivery_attempts", shipment_id=ids["shipment"], attempt_number=1, attempted_at="2026-04-15 15:00:00",
          attempt_outcome="DELIVERED", received_by_name="Store Manager", proof_of_delivery_reference="OTP-4471")

        # --- invoicing and returns ---------------------------------------------------------------
        i("invoice", "invoices", invoice_number="KA2627-I000001", order_id=ids["order"], shipment_id=ids["shipment"],
          customer_id=ids["customer"], company_gst_registration_id=ids["reg_ka"], warehouse_id=ids["wh_dc"],
          invoice_date="2026-04-13", issued_at="2026-04-13 12:00:00", customer_name="Metro Mobiles",
          customer_gstin="29ZZXPC5678D1Z9", billing_address_id=ids["addr"], shipping_address_id=ids["addr"],
          place_of_supply_state_id=ids["state_ka"], is_inter_state=False, payment_term_id=ids["term_net30"],
          due_date="2026-05-13", taxable_amount=o_taxable, cgst_amount=o_half, sgst_amount=o_half, igst_amount=0,
          round_off_amount=0, total_amount=o_total, irn=hex64("irn-1"), irn_generated_at="2026-04-13 12:00:01")
        i("invoice_line", "invoice_line_items", invoice_id=ids["invoice"], line_number=1, line_type="PRODUCT",
          order_line_item_id=ids["order_line"], shipment_line_item_id=ids["shipment_line"], product_id=ids["product"],
          hsn_code="85183000", quantity=o_qty, unit_price=o_price, taxable_amount=o_taxable, gst_rate=rate,
          cgst_amount=o_half, sgst_amount=o_half, line_total=o_total)
        i("return", "sales_returns", return_number="RMA/KA/2026-27/000001", order_id=ids["order"],
          requested_at="2026-04-16 10:00:00", return_method="CARRIER_PICKUP", return_warehouse_id=ids["wh_dc"],
          shipping_carrier_id=ids["carrier"], reverse_tracking_number="SWF000000002", return_status="CLOSED",
          approved_by_employee_id=ids["emp"], approved_at="2026-04-16 12:00:00", received_at="2026-04-18 10:00:00",
          closed_at="2026-04-20 18:00:00")
        i("return_line", "sales_return_line_items", sales_return_id=ids["return"], line_number=1,
          order_line_item_id=ids["order_line"], invoice_line_item_id=ids["invoice_line"], return_reason="DEFECTIVE",
          quantity_requested=2, quantity_approved=2, quantity_received=2, received_at="2026-04-18 10:00:00",
          line_status="CREDITED")
        i("return_inspection", "return_inspections", sales_return_line_item_id=ids["return_line"], inspection_sequence=1,
          inspected_at="2026-04-19 10:00:00", inspected_by_employee_id=ids["emp"], quantity_inspected=2, quantity_sellable=1,
          quantity_defective=1, inspection_result="MIXED", defect_reason="MANUFACTURING_DEFECT")
        cn_taxable = money(Decimal(2) * o_price)       # 2000.00
        cn_half = money(cn_taxable * rate / 200)       # 180.00
        i("credit_note", "credit_notes", credit_note_number="KA2627-C000001", invoice_id=ids["invoice"],
          customer_id=ids["customer"], company_gst_registration_id=ids["reg_ka"], credit_note_date="2026-04-20",
          reason="SALES_RETURN", sales_return_id=ids["return"], is_inter_state=False, taxable_amount=cn_taxable,
          cgst_amount=cn_half, sgst_amount=cn_half, round_off_amount=0, total_amount=cn_taxable + 2 * cn_half,
          approved_by_employee_id=ids["emp"])
        i("credit_note_line", "credit_note_line_items", credit_note_id=ids["credit_note"], line_number=1,
          invoice_line_item_id=ids["invoice_line"], sales_return_line_item_id=ids["return_line"], quantity=2,
          unit_price=o_price, taxable_amount=cn_taxable, gst_rate=rate, cgst_amount=cn_half, sgst_amount=cn_half,
          line_total=cn_taxable + 2 * cn_half)
        i("refund", "refunds", refund_number="RFD/KA/2026-27/000001", credit_note_id=ids["credit_note"],
          customer_payment_id=ids["payment"], refund_amount=cn_taxable + 2 * cn_half, payment_method_id=ids["method_neft"],
          bank_account_id=ids["bank"], requested_at="2026-04-20 10:00:00", approved_by_employee_id=ids["emp"],
          approved_at="2026-04-20 11:00:00", processed_at="2026-04-20 15:00:00", bank_reference="UTR0000003",
          refund_status="COMPLETED")
        i("defect", "defective_stock", defect_number="DEF/KA/2026-27/000001", warehouse_id=ids["wh_dc"],
          product_id=ids["product"], quantity=1, return_inspection_id=ids["return_inspection"],
          defect_reason="MANUFACTURING_DEFECT", detected_at="2026-04-19 10:00:00", disposition="SCRAP",
          disposition_status="COMPLETED", disposed_at="2026-04-25 10:00:00", disposed_by_employee_id=ids["emp"])

        # --- the movement ledger: one row per source type --------------------------------------
        def movement(key, at, wh, ttype, status, qty_, cost, **source):
            i(key, "inventory_transactions", transaction_at=at, warehouse_id=ids[wh], product_id=ids["product"],
              transaction_type=ttype, stock_status=status, quantity=qty_, unit_cost=cost, value_change=money(Decimal(qty_) * cost),
              generation_run_id=ids["run"], **source)

        movement("mv_opening", "2026-04-01 09:00:00", "wh_dc", "ADJUSTMENT", "SELLABLE", 5, 500,
                 stock_adjustment_line_item_id=ids["adjustment_line"])
        movement("mv_receipt", "2026-04-10 12:00:00", "wh_dc", "PURCHASE_RECEIPT", "SELLABLE", 100, 500,
                 goods_receipt_line_item_id=ids["grn_line"])
        movement("mv_transfer_out", "2026-04-11 10:00:00", "wh_dc", "TRANSFER_OUT", "SELLABLE", -5, 500,
                 stock_transfer_line_item_id=ids["transfer_line"])
        movement("mv_transfer_in", "2026-04-11 16:00:00", "wh_store", "TRANSFER_IN", "SELLABLE", 5, 500,
                 stock_transfer_line_item_id=ids["transfer_line"])
        movement("mv_issue", "2026-04-13 12:00:00", "wh_dc", "SALES_ISSUE", "SELLABLE", -10, 500,
                 shipment_line_item_id=ids["shipment_line"])
        movement("mv_return", "2026-04-18 10:00:00", "wh_dc", "RETURN_RECEIPT", "QUARANTINE", 2, 500,
                 sales_return_line_item_id=ids["return_line"])
        movement("mv_disp_out", "2026-04-19 10:00:00", "wh_dc", "RETURN_DISPOSITION", "QUARANTINE", -2, 500,
                 return_inspection_id=ids["return_inspection"])
        movement("mv_disp_sell", "2026-04-19 10:00:00", "wh_dc", "RETURN_DISPOSITION", "SELLABLE", 1, 500,
                 return_inspection_id=ids["return_inspection"])
        movement("mv_disp_def", "2026-04-19 10:00:00", "wh_dc", "RETURN_DISPOSITION", "DEFECTIVE", 1, 500,
                 return_inspection_id=ids["return_inspection"])
        movement("mv_scrap", "2026-04-25 10:00:00", "wh_dc", "DEFECTIVE_DISPOSITION", "DEFECTIVE", -1, 500,
                 defective_stock_id=ids["defect"])

        # --- expenses ----------------------------------------------------------------------------
        e_taxable = Decimal(50000)
        e_half = money(e_taxable * rate / 200)
        e_total = e_taxable + 2 * e_half
        i("expense", "expenses", expense_number="EXP/KA/2026-27/000001", expense_type="VENDOR_BILL",
          supplier_id=ids["supplier_svc"], vendor_bill_number="RENT-2026-04", expense_date="2026-04-05",
          received_date="2026-04-06", due_date="2026-04-20", service_period_start="2026-04-01",
          service_period_end="2026-04-30", company_gst_registration_id=ids["reg_ka"], warehouse_id=ids["wh_dc"],
          is_inter_state=False, taxable_amount=e_taxable, cgst_amount=e_half, sgst_amount=e_half, round_off_amount=0,
          total_amount=e_total, itc_eligible_tax_amount=2 * e_half, recurrence_key="RENT-DC-BLR",
          expense_status="APPROVED", submitted_by_employee_id=ids["emp"], approved_by_employee_id=ids["emp"],
          approved_at="2026-04-07 10:00:00", description="DC rent April 2026")
        i("expense_line", "expense_line_items", expense_id=ids["expense"], line_number=1,
          expense_category_id=ids["cat_rent"], cost_center_id=ids["cc_corp"], description="Rent", sac_code="997212",
          taxable_amount=e_taxable, gst_rate=rate, cgst_amount=e_half, sgst_amount=e_half, is_itc_eligible=True,
          line_total=e_total)
        i("expense_payment", "expense_payments", payment_number="EPAY/KA/2026-27/000001", expense_id=ids["expense"],
          payment_date="2026-04-20", bank_account_id=ids["bank"], payment_method_id=ids["method_neft"], amount=e_total,
          payment_status="COMPLETED", processed_at="2026-04-20 10:00:00")
        i("attachment", "expense_attachments_metadata", expense_id=ids["expense"], document_type="VENDOR_INVOICE",
          file_name="rent_april_2026.pdf", mime_type="application/pdf", file_size_bytes=182044,
          storage_uri="s3://kaveri-docs.example/expenses/2026/04/rent_april_2026.pdf", sha256_checksum=hex64("rent-pdf"),
          uploaded_at="2026-04-06 09:30:00", uploaded_by_employee_id=ids["emp"])

        # --- GST, bank, journals, subledgers ----------------------------------------------------
        i("gst_return", "gst_returns", company_gst_registration_id=ids["reg_ka"], return_type="GSTR3B", return_period="2026-04",
          due_date="2026-05-20", filed_date="2026-05-18", arn="AA290526000001X", output_cgst=o_half, output_sgst=o_half,
          itc_cgst=e_half, itc_sgst=e_half, itc_used_cgst=o_half, itc_used_sgst=o_half, return_status="FILED")
        i("gst_line", "gst_return_line_items", gst_return_id=ids["gst_return"], line_number=1, section_code="3.1A",
          document_count=1, taxable_value=o_taxable, cgst_amount=o_half, sgst_amount=o_half)
        i("gst_line_paid", "gst_return_line_items", gst_return_id=ids["gst_return"], line_number=2, section_code="6.1",
          paid_through="ITC_CGST", cgst_amount=o_half)
        i("reconciliation", "bank_reconciliations", bank_account_id=ids["bank"], accounting_period_id=ids["period"],
          statement_closing_balance=100000, book_closing_balance=100000, difference_amount=0,
          reconciliation_status="RECONCILED", reconciled_by_employee_id=ids["emp"], reconciled_at="2026-05-05 10:00:00")
        i("bank_txn", "bank_transactions", bank_account_id=ids["bank"], statement_date="2026-04-28", value_date="2026-04-28",
          transaction_type="CUSTOMER_RECEIPT", narration="NEFT CR-METRO MOBILES", bank_reference="UTR0000002",
          credit_amount=o_total, balance_after=Decimal(111800), customer_payment_id=ids["payment"],
          bank_reconciliation_id=ids["reconciliation"])
        i("journal", "journal_entries", journal_number="JV/2026-27/0000001", journal_source="SALES_INVOICE",
          journal_type="SYSTEM", posting_date="2026-04-13", document_date="2026-04-13", accounting_period_id=ids["period"],
          description="Sales invoice KA2627-I000001", total_debit=o_total, total_credit=o_total,
          invoice_id=ids["invoice"], generation_run_id=ids["run"])
        i("journal_dr", "journal_entry_lines", journal_entry_id=ids["journal"], line_number=1, account_id=ids["acc_ar"],
          debit_amount=o_total, customer_id=ids["customer"])
        i("journal_cr", "journal_entry_lines", journal_entry_id=ids["journal"], line_number=2, account_id=ids["acc_sales_elec"],
          credit_amount=o_total, cost_center_id=ids["cc_elec"])
        i("gl_balance", "gl_period_balances", account_id=ids["acc_ar"], accounting_period_id=ids["period"], opening_balance=0,
          period_debit=o_total, period_credit=0, closing_balance=o_total, rebuilt_at="2026-05-01 00:00:00")
        i("ar_item", "accounts_receivable", customer_id=ids["customer"], item_type="INVOICE", invoice_id=ids["invoice"],
          document_date="2026-04-13", due_date="2026-05-13", original_amount=o_total, open_amount=0, item_status="SETTLED",
          settled_at="2026-04-28 10:05:00", journal_entry_id=ids["journal"])
        i("allocation", "customer_payment_allocations", customer_payment_id=ids["payment"], invoice_id=ids["invoice"],
          allocated_amount=o_total, allocation_date="2026-04-28")
        i("ap_item", "accounts_payable", supplier_id=ids["supplier"], item_type="SUPPLIER_INVOICE",
          supplier_invoice_id=ids["supplier_invoice"], document_date="2026-04-10", due_date="2026-05-10",
          original_amount=po_total, open_amount=0, item_status="SETTLED", settled_at="2026-04-30 10:00:00",
          journal_entry_id=ids["journal"])
        i("supplier_allocation", "supplier_payment_allocations", supplier_payment_id=ids["supplier_payment"],
          supplier_invoice_id=ids["supplier_invoice"], allocated_amount=po_total, allocation_date="2026-04-30")
        i("tax_txn", "tax_transactions", company_gst_registration_id=ids["reg_ka"], tax_direction="OUTPUT",
          document_type="SALES_INVOICE", invoice_line_item_id=ids["invoice_line"], document_number="KA2627-I000001",
          document_date="2026-04-13", counterparty_gstin="29ZZXPC5678D1Z9", customer_id=ids["customer"],
          place_of_supply_state_id=ids["state_ka"], is_inter_state=False, hsn_code="85183000", uqc="NOS", quantity=o_qty,
          taxable_value=o_taxable, gst_rate=rate, cgst_amount=o_half, sgst_amount=o_half,
          itc_eligibility="NOT_APPLICABLE", gstr1_section="B2B", gst_return_id=ids["gst_return"])
        return ids


def build_baseline(conn: Connection) -> dict[str, int]:
    """Insert the baseline dataset and commit; returns the id map."""
    builder = BaselineBuilder(conn)
    ids = builder.build()
    builder.cursor.close()
    conn.connection.commit()
    return ids
