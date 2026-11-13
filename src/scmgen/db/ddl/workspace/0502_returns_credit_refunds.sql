-- Workspace schema, batch 5 (Part 1D invoicing & returns), file 2/2: sales returns, return inspections,
-- credit notes, refunds and the defective-stock register.
-- Design: docs/design/phase1_part1d_sales_shipping_invoicing_returns.md §5-§6. Conventions: see 0101_reference.sql.
--
-- Credit-note lines carry the tax-split and total CHECKs but no taxable-value formula: credits can be
-- value-only (price adjustments, quantity 0). "Never credit more than invoiced" spans rows: V-IV-04.

CREATE TABLE sales_returns (
    sales_return_id             BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    return_number               VARCHAR(30)      NOT NULL,
    order_id                    BIGINT UNSIGNED  NOT NULL,
    requested_at                DATETIME(6)      NOT NULL,
    return_method               VARCHAR(30)      NOT NULL,
    return_warehouse_id         SMALLINT UNSIGNED NOT NULL,
    shipping_carrier_id         SMALLINT UNSIGNED NULL,
    reverse_tracking_number     VARCHAR(30)      NULL,
    return_status               VARCHAR(30)      NOT NULL DEFAULT 'REQUESTED',
    approved_by_employee_id     BIGINT UNSIGNED  NULL,
    approved_at                 DATETIME(6)      NULL,
    rejection_reason            VARCHAR(40)      NULL,
    received_at                 DATETIME(6)      NULL,
    closed_at                   DATETIME(6)      NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (sales_return_id),
    UNIQUE KEY uq_sales_returns_number (return_number),
    UNIQUE KEY uq_sales_returns_reverse_tracking (reverse_tracking_number),
    KEY ix_sales_returns_status_requested (return_status, requested_at),
    CONSTRAINT fk_sales_returns_order FOREIGN KEY (order_id) REFERENCES orders (order_id),
    CONSTRAINT fk_sales_returns_warehouse FOREIGN KEY (return_warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT fk_sales_returns_carrier FOREIGN KEY (shipping_carrier_id) REFERENCES shipping_carriers (shipping_carrier_id),
    CONSTRAINT fk_sales_returns_approved_by FOREIGN KEY (approved_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_sales_returns_number CHECK (REGEXP_LIKE(return_number, '^RMA/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_sales_returns_method CHECK (REGEXP_LIKE(return_method, '^(CARRIER_PICKUP|DROP_AT_STORE)$', 'c')),
    CONSTRAINT ck_sales_returns_carrier CHECK ((return_method = 'CARRIER_PICKUP') = (shipping_carrier_id IS NOT NULL)),
    CONSTRAINT ck_sales_returns_status CHECK (REGEXP_LIKE(return_status,
        '^(REQUESTED|APPROVED|REJECTED|PICKUP_SCHEDULED|IN_TRANSIT|RECEIVED|INSPECTED|CLOSED|CANCELLED)$', 'c')),
    CONSTRAINT ck_sales_returns_rejected CHECK (
        (return_status = 'REJECTED') = (rejection_reason IS NOT NULL)
        AND (rejection_reason IS NULL OR REGEXP_LIKE(rejection_reason,
            '^(OUTSIDE_RETURN_WINDOW|NON_RETURNABLE_PRODUCT|NOT_ELIGIBLE)$', 'c'))),
    CONSTRAINT ck_sales_returns_approved CHECK (
        (approved_by_employee_id IS NULL) = (approved_at IS NULL)
        AND (return_status IN ('REQUESTED', 'REJECTED', 'CANCELLED') OR approved_at IS NOT NULL)),
    CONSTRAINT ck_sales_returns_times CHECK (
        (received_at IS NULL OR received_at > requested_at) AND (closed_at IS NULL OR closed_at >= received_at)),
    CONSTRAINT ck_sales_returns_closed CHECK ((return_status = 'CLOSED') = (closed_at IS NOT NULL)),
    CONSTRAINT ck_sales_returns_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Return authorisation (RMA) against one order (1D 6)';

CREATE TABLE sales_return_line_items (
    sales_return_line_item_id   BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    sales_return_id             BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    order_line_item_id          BIGINT UNSIGNED  NOT NULL,
    invoice_line_item_id        BIGINT UNSIGNED  NOT NULL,
    return_reason               VARCHAR(30)      NOT NULL,
    quantity_requested          DECIMAL(18,3)    NOT NULL,
    quantity_approved           DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_received           DECIMAL(18,3)    NOT NULL DEFAULT 0,
    received_at                 DATETIME(6)      NULL,
    receiving_location_id       BIGINT UNSIGNED  NULL,
    line_status                 VARCHAR(30)      NOT NULL DEFAULT 'REQUESTED',
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (sales_return_line_item_id),
    UNIQUE KEY uq_sales_return_line_items_return_line (sales_return_id, line_number),
    UNIQUE KEY uq_sales_return_line_items_return_invoice_line (sales_return_id, invoice_line_item_id),
    CONSTRAINT fk_sales_return_line_items_return FOREIGN KEY (sales_return_id) REFERENCES sales_returns (sales_return_id),
    -- The returned order line is the one the invoice line billed (D80).
    CONSTRAINT fk_sales_return_line_items_invoice_line FOREIGN KEY (invoice_line_item_id, order_line_item_id)
        REFERENCES invoice_line_items (invoice_line_item_id, order_line_item_id),
    CONSTRAINT fk_sales_return_line_items_order_line FOREIGN KEY (order_line_item_id) REFERENCES order_line_items (order_line_item_id),
    CONSTRAINT fk_sales_return_line_items_location FOREIGN KEY (receiving_location_id)
        REFERENCES warehouse_locations (warehouse_location_id),
    CONSTRAINT ck_sales_return_line_items_reason CHECK (REGEXP_LIKE(return_reason,
        '^(DEFECTIVE|DAMAGED_IN_TRANSIT|WRONG_ITEM|NOT_AS_DESCRIBED|SIZE_OR_FIT|CHANGED_MIND|WARRANTY_CLAIM|NEAR_EXPIRY)$', 'c')),
    CONSTRAINT ck_sales_return_line_items_quantities CHECK (
        line_number > 0 AND quantity_requested > 0
        AND quantity_approved >= 0 AND quantity_approved <= quantity_requested
        AND quantity_received >= 0 AND quantity_received <= quantity_approved),
    CONSTRAINT ck_sales_return_line_items_received CHECK ((quantity_received > 0) = (received_at IS NOT NULL)),
    CONSTRAINT ck_sales_return_line_items_status CHECK (REGEXP_LIKE(line_status,
        '^(REQUESTED|APPROVED|REJECTED|RECEIVED|INSPECTED|CREDITED)$', 'c')),
    CONSTRAINT ck_sales_return_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Returned quantity per invoiced line incl. physical receipt (absorbs returned_goods, R4; 1D 6)';

CREATE TABLE return_inspections (
    return_inspection_id        BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    sales_return_line_item_id   BIGINT UNSIGNED  NOT NULL,
    inspection_sequence         TINYINT UNSIGNED NOT NULL,
    inspected_at                DATETIME(6)      NOT NULL,
    inspected_by_employee_id    BIGINT UNSIGNED  NOT NULL,
    quantity_inspected          DECIMAL(18,3)    NOT NULL,
    quantity_sellable           DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_defective          DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_further_inspection DECIMAL(18,3)    NOT NULL DEFAULT 0,
    inspection_result           VARCHAR(30)      NOT NULL,
    defect_reason               VARCHAR(40)      NULL,
    inspection_notes            VARCHAR(255)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (return_inspection_id),
    UNIQUE KEY uq_return_inspections_line_sequence (sales_return_line_item_id, inspection_sequence),
    CONSTRAINT fk_return_inspections_return_line FOREIGN KEY (sales_return_line_item_id)
        REFERENCES sales_return_line_items (sales_return_line_item_id),
    CONSTRAINT fk_return_inspections_inspected_by FOREIGN KEY (inspected_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_return_inspections_quantities CHECK (
        inspection_sequence > 0 AND quantity_inspected > 0
        AND quantity_sellable >= 0 AND quantity_defective >= 0 AND quantity_further_inspection >= 0
        AND quantity_sellable + quantity_defective + quantity_further_inspection = quantity_inspected),
    CONSTRAINT ck_return_inspections_result CHECK (REGEXP_LIKE(inspection_result,
        '^(PASSED|DEFECTIVE|MIXED|FURTHER_INSPECTION)$', 'c')),
    -- The result names the single outcome when everything went one way; MIXED otherwise.
    CONSTRAINT ck_return_inspections_result_quantities CHECK (
        (inspection_result = 'PASSED') = (quantity_sellable = quantity_inspected)
        AND (inspection_result = 'DEFECTIVE') = (quantity_defective = quantity_inspected)
        AND (inspection_result = 'FURTHER_INSPECTION') = (quantity_further_inspection = quantity_inspected)),
    CONSTRAINT ck_return_inspections_defect_reason CHECK (
        (quantity_defective > 0) = (defect_reason IS NOT NULL)
        AND (defect_reason IS NULL OR REGEXP_LIKE(defect_reason,
            '^(MANUFACTURING_DEFECT|PHYSICAL_DAMAGE|MISSING_PARTS|USED_OR_SOILED|EXPIRED)$', 'c'))),
    CONSTRAINT ck_return_inspections_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Inspection of received return quantity: sellable / defective / further inspection (1D 6)';

CREATE TABLE credit_notes (
    credit_note_id              BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    credit_note_number          VARCHAR(16)      NOT NULL,
    invoice_id                  BIGINT UNSIGNED  NOT NULL,
    customer_id                 BIGINT UNSIGNED  NOT NULL,
    company_gst_registration_id SMALLINT UNSIGNED NOT NULL,
    credit_note_date            DATE             NOT NULL,
    reason                      VARCHAR(30)      NOT NULL,
    sales_return_id             BIGINT UNSIGNED  NULL,
    is_inter_state              BOOLEAN          NOT NULL,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    round_off_amount            DECIMAL(18,2)    NOT NULL DEFAULT 0,
    total_amount                DECIMAL(18,2)    NOT NULL,
    irn                         CHAR(64)         NULL,
    irn_generated_at            DATETIME(6)      NULL,
    approved_by_employee_id     BIGINT UNSIGNED  NOT NULL,
    credit_note_status          VARCHAR(30)      NOT NULL DEFAULT 'ISSUED',
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (credit_note_id),
    UNIQUE KEY uq_credit_notes_number (credit_note_number),
    UNIQUE KEY uq_credit_notes_irn (irn),
    -- Target of the composite FK from the AR/AP open-item subledger (same party, Part 1E batch).
    UNIQUE KEY uq_credit_notes_id_customer (credit_note_id, customer_id),
    KEY ix_credit_notes_customer_date (customer_id, credit_note_date),
    KEY ix_credit_notes_registration_date (company_gst_registration_id, credit_note_date),
    -- Same customer and GSTIN as the credited invoice (D80).
    CONSTRAINT fk_credit_notes_invoice FOREIGN KEY (invoice_id, customer_id, company_gst_registration_id)
        REFERENCES invoices (invoice_id, customer_id, company_gst_registration_id),
    CONSTRAINT fk_credit_notes_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    CONSTRAINT fk_credit_notes_registration FOREIGN KEY (company_gst_registration_id)
        REFERENCES company_gst_registrations (company_gst_registration_id),
    CONSTRAINT fk_credit_notes_sales_return FOREIGN KEY (sales_return_id) REFERENCES sales_returns (sales_return_id),
    CONSTRAINT fk_credit_notes_approved_by FOREIGN KEY (approved_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_credit_notes_number CHECK (REGEXP_LIKE(credit_note_number, '^[A-Z]{2}[0-9]{4}-C[0-9]{6}$', 'c')),
    CONSTRAINT ck_credit_notes_reason CHECK (REGEXP_LIKE(reason, '^(SALES_RETURN|RTO|PRICE_ADJUSTMENT|POST_SALE_DISCOUNT)$', 'c')),
    CONSTRAINT ck_credit_notes_return_link CHECK ((reason = 'SALES_RETURN') = (sales_return_id IS NOT NULL)),
    CONSTRAINT ck_credit_notes_amounts CHECK (
        taxable_amount >= 0 AND cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0
        AND cgst_amount = sgst_amount AND round_off_amount BETWEEN -0.99 AND 0.50
        AND total_amount = taxable_amount + cgst_amount + sgst_amount + igst_amount + round_off_amount
        AND total_amount = ROUND(total_amount, 0)),
    CONSTRAINT ck_credit_notes_tax_mode CHECK ((is_inter_state AND cgst_amount = 0) OR (NOT is_inter_state AND igst_amount = 0)),
    CONSTRAINT ck_credit_notes_irn CHECK (
        (irn IS NULL) = (irn_generated_at IS NULL) AND (irn IS NULL OR REGEXP_LIKE(irn, '^[0-9a-f]{64}$', 'c'))),
    CONSTRAINT ck_credit_notes_status CHECK (REGEXP_LIKE(credit_note_status, '^(ISSUED|CANCELLED)$', 'c')),
    CONSTRAINT ck_credit_notes_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'GST credit note against one invoice: returns, RTO, price adjustments (1D 5, D38, D41, D44)';

CREATE TABLE credit_note_line_items (
    credit_note_line_item_id    BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    credit_note_id              BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    invoice_line_item_id        BIGINT UNSIGNED  NOT NULL,
    sales_return_line_item_id   BIGINT UNSIGNED  NULL,
    quantity                    DECIMAL(18,3)    NOT NULL,
    unit_price                  DECIMAL(18,4)    NOT NULL,
    discount_percent            DECIMAL(7,4)     NOT NULL DEFAULT 0,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    gst_rate                    DECIMAL(7,4)     NOT NULL,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    line_total                  DECIMAL(18,2)    NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (credit_note_line_item_id),
    UNIQUE KEY uq_credit_note_line_items_note_line (credit_note_id, line_number),
    UNIQUE KEY uq_credit_note_line_items_note_invoice_line (credit_note_id, invoice_line_item_id),
    CONSTRAINT fk_credit_note_line_items_credit_note FOREIGN KEY (credit_note_id) REFERENCES credit_notes (credit_note_id),
    CONSTRAINT fk_credit_note_line_items_invoice_line FOREIGN KEY (invoice_line_item_id)
        REFERENCES invoice_line_items (invoice_line_item_id),
    CONSTRAINT fk_credit_note_line_items_return_line FOREIGN KEY (sales_return_line_item_id)
        REFERENCES sales_return_line_items (sales_return_line_item_id),
    CONSTRAINT ck_credit_note_line_items_values CHECK (
        line_number > 0 AND quantity >= 0 AND (quantity > 0 OR sales_return_line_item_id IS NULL)
        AND unit_price >= 0 AND discount_percent BETWEEN 0 AND 100 AND gst_rate BETWEEN 0 AND 50
        AND taxable_amount >= 0),
    CONSTRAINT ck_credit_note_line_items_tax CHECK (
        ((igst_amount = 0 AND cgst_amount = ROUND(taxable_amount * gst_rate / 200, 2) AND sgst_amount = cgst_amount)
      OR (cgst_amount = 0 AND sgst_amount = 0 AND igst_amount = ROUND(taxable_amount * gst_rate / 100, 2)))
        AND line_total = taxable_amount + cgst_amount + sgst_amount + igst_amount),
    CONSTRAINT ck_credit_note_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Credited lines with GST reversal, at the invoice line rate (1D 5, R5)';

CREATE TABLE refunds (
    refund_id                   BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    refund_number               VARCHAR(30)      NOT NULL,
    credit_note_id              BIGINT UNSIGNED  NOT NULL,
    customer_payment_id         BIGINT UNSIGNED  NULL,
    refund_amount               DECIMAL(18,2)    NOT NULL,
    payment_method_id           SMALLINT UNSIGNED NOT NULL,
    bank_account_id             SMALLINT UNSIGNED NOT NULL,
    requested_at                DATETIME(6)      NOT NULL,
    approved_by_employee_id     BIGINT UNSIGNED  NULL,
    approved_at                 DATETIME(6)      NULL,
    processed_at                DATETIME(6)      NULL,
    bank_reference              VARCHAR(40)      NULL,
    refund_status               VARCHAR(30)      NOT NULL DEFAULT 'PENDING_APPROVAL',
    failure_reason              VARCHAR(100)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (refund_id),
    UNIQUE KEY uq_refunds_number (refund_number),
    KEY ix_refunds_status_requested (refund_status, requested_at),
    CONSTRAINT fk_refunds_credit_note FOREIGN KEY (credit_note_id) REFERENCES credit_notes (credit_note_id),
    CONSTRAINT fk_refunds_customer_payment FOREIGN KEY (customer_payment_id) REFERENCES customer_payments (customer_payment_id),
    CONSTRAINT fk_refunds_method FOREIGN KEY (payment_method_id) REFERENCES payment_methods (payment_method_id),
    CONSTRAINT fk_refunds_bank_account FOREIGN KEY (bank_account_id) REFERENCES bank_accounts (bank_account_id),
    CONSTRAINT fk_refunds_approved_by FOREIGN KEY (approved_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_refunds_number CHECK (REGEXP_LIKE(refund_number, '^RFD/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_refunds_amount CHECK (refund_amount > 0),
    CONSTRAINT ck_refunds_status CHECK (REGEXP_LIKE(refund_status,
        '^(PENDING_APPROVAL|APPROVED|PROCESSING|COMPLETED|FAILED|REJECTED)$', 'c')),
    CONSTRAINT ck_refunds_approval CHECK (
        (approved_by_employee_id IS NULL) = (approved_at IS NULL)
        AND (refund_status IN ('PENDING_APPROVAL', 'REJECTED') OR approved_at IS NOT NULL)),
    CONSTRAINT ck_refunds_processed CHECK ((refund_status IN ('COMPLETED', 'FAILED')) = (processed_at IS NOT NULL)),
    CONSTRAINT ck_refunds_failure CHECK ((refund_status = 'FAILED') = (failure_reason IS NOT NULL)),
    CONSTRAINT ck_refunds_times CHECK (
        (approved_at IS NULL OR approved_at >= requested_at) AND (processed_at IS NULL OR processed_at >= requested_at)),
    CONSTRAINT ck_refunds_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Cash refund paying out (part of) a credit note (1D 6, R5, D44)';

CREATE TABLE defective_stock (
    defective_stock_id          BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    defect_number               VARCHAR(30)      NOT NULL,
    warehouse_id                SMALLINT UNSIGNED NOT NULL,
    product_id                  BIGINT UNSIGNED  NOT NULL,
    inventory_batch_id          BIGINT UNSIGNED  NULL,
    quantity                    DECIMAL(18,3)    NOT NULL,
    return_inspection_id        BIGINT UNSIGNED  NULL,
    stock_adjustment_line_item_id BIGINT UNSIGNED NULL,
    stock_transfer_line_item_id BIGINT UNSIGNED  NULL,
    defect_reason               VARCHAR(40)      NOT NULL,
    detected_at                 DATETIME(6)      NOT NULL,
    disposition                 VARCHAR(30)      NOT NULL DEFAULT 'PENDING',
    disposition_status          VARCHAR(30)      NOT NULL DEFAULT 'PENDING',
    disposed_at                 DATETIME(6)      NULL,
    disposed_by_employee_id     BIGINT UNSIGNED  NULL,
    supplier_id                 BIGINT UNSIGNED  NULL,
    repair_cost_amount          DECIMAL(18,2)    NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (defective_stock_id),
    UNIQUE KEY uq_defective_stock_number (defect_number),
    -- One register row per defect event: each source produces at most one row.
    UNIQUE KEY uq_defective_stock_return_inspection (return_inspection_id),
    UNIQUE KEY uq_defective_stock_adjustment_line (stock_adjustment_line_item_id),
    UNIQUE KEY uq_defective_stock_transfer_line (stock_transfer_line_item_id),
    KEY ix_defective_stock_warehouse_status (warehouse_id, disposition_status),
    CONSTRAINT fk_defective_stock_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT fk_defective_stock_product FOREIGN KEY (product_id) REFERENCES products (product_id),
    CONSTRAINT fk_defective_stock_batch FOREIGN KEY (inventory_batch_id) REFERENCES inventory_batches (inventory_batch_id),
    CONSTRAINT fk_defective_stock_return_inspection FOREIGN KEY (return_inspection_id)
        REFERENCES return_inspections (return_inspection_id),
    CONSTRAINT fk_defective_stock_adjustment_line FOREIGN KEY (stock_adjustment_line_item_id)
        REFERENCES stock_adjustment_line_items (stock_adjustment_line_item_id),
    CONSTRAINT fk_defective_stock_transfer_line FOREIGN KEY (stock_transfer_line_item_id)
        REFERENCES stock_transfer_line_items (stock_transfer_line_item_id),
    CONSTRAINT fk_defective_stock_disposed_by FOREIGN KEY (disposed_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT fk_defective_stock_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (supplier_id),
    CONSTRAINT ck_defective_stock_number CHECK (REGEXP_LIKE(defect_number, '^DEF/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_defective_stock_quantity CHECK (quantity > 0),
    -- Exactly one cause (D45).
    CONSTRAINT ck_defective_stock_one_source CHECK (
        (return_inspection_id IS NOT NULL) + (stock_adjustment_line_item_id IS NOT NULL)
        + (stock_transfer_line_item_id IS NOT NULL) = 1),
    CONSTRAINT ck_defective_stock_reason CHECK (REGEXP_LIKE(defect_reason,
        '^(MANUFACTURING_DEFECT|PHYSICAL_DAMAGE|MISSING_PARTS|USED_OR_SOILED|EXPIRED|HANDLING_DAMAGE|TRANSIT_DAMAGE)$', 'c')),
    CONSTRAINT ck_defective_stock_disposition CHECK (REGEXP_LIKE(disposition, '^(PENDING|SCRAP|RETURN_TO_SUPPLIER|REPAIR)$', 'c')),
    CONSTRAINT ck_defective_stock_disposition_status CHECK (
        REGEXP_LIKE(disposition_status, '^(PENDING|COMPLETED)$', 'c')
        AND (disposition = 'PENDING') = (disposition_status = 'PENDING')),
    CONSTRAINT ck_defective_stock_completed CHECK (
        (disposition_status = 'COMPLETED') = (disposed_at IS NOT NULL AND disposed_by_employee_id IS NOT NULL)
        AND (disposed_at IS NULL OR disposed_at >= detected_at)),
    CONSTRAINT ck_defective_stock_supplier CHECK ((disposition = 'RETURN_TO_SUPPLIER') = (supplier_id IS NOT NULL)),
    -- Repair cost is a memo, not posted (D60).
    CONSTRAINT ck_defective_stock_repair CHECK (
        (disposition = 'REPAIR') = (repair_cost_amount IS NOT NULL) AND (repair_cost_amount IS NULL OR repair_cost_amount >= 0)),
    CONSTRAINT ck_defective_stock_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Register of defective quantities: reason, cause and disposition (1D 6, D45, D60)';

-- Close the deferred FKs from batch 3: ledger rows caused by invoicing, returns and defect disposition.
ALTER TABLE inventory_transactions
    ADD CONSTRAINT fk_inventory_transactions_invoice_line FOREIGN KEY (invoice_line_item_id)
        REFERENCES invoice_line_items (invoice_line_item_id),
    ADD CONSTRAINT fk_inventory_transactions_return_line FOREIGN KEY (sales_return_line_item_id)
        REFERENCES sales_return_line_items (sales_return_line_item_id),
    ADD CONSTRAINT fk_inventory_transactions_return_inspection FOREIGN KEY (return_inspection_id)
        REFERENCES return_inspections (return_inspection_id),
    ADD CONSTRAINT fk_inventory_transactions_defective_stock FOREIGN KEY (defective_stock_id)
        REFERENCES defective_stock (defective_stock_id);
