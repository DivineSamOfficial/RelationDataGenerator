-- Workspace schema, batch 2 (Part 1C procurement), file 3/3: supplier invoices and supplier payments.
-- Design: docs/design/phase1_part1c_procurement_and_inventory.md §3. Conventions: see 0101_reference.sql;
-- amount rules: see 0201_rfq_quotations.sql (D84). Unregistered / composition suppliers charge no GST,
-- so their lines carry gst_rate = 0.

CREATE TABLE supplier_invoices (
    supplier_invoice_id         BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    internal_reference          VARCHAR(30)      NOT NULL,
    supplier_id                 BIGINT UNSIGNED  NOT NULL,
    supplier_invoice_number     VARCHAR(40)      NOT NULL,
    purchase_order_id           BIGINT UNSIGNED  NOT NULL,
    company_gst_registration_id SMALLINT UNSIGNED NOT NULL,
    supplier_gstin              CHAR(15)         NULL,
    invoice_date                DATE             NOT NULL,
    received_date               DATE             NOT NULL,
    due_date                    DATE             NOT NULL,
    is_inter_state              BOOLEAN          NOT NULL,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    round_off_amount            DECIMAL(18,2)    NOT NULL DEFAULT 0,
    total_amount                DECIMAL(18,2)    NOT NULL,
    match_status                VARCHAR(30)      NOT NULL,
    invoice_status              VARCHAR(30)      NOT NULL DEFAULT 'RECEIVED',
    approved_by_employee_id     BIGINT UNSIGNED  NULL,
    approved_at                 DATETIME(6)      NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (supplier_invoice_id),
    UNIQUE KEY uq_supplier_invoices_reference (internal_reference),
    -- Duplicate-bill guard: a supplier never bills the same number twice.
    UNIQUE KEY uq_supplier_invoices_supplier_number (supplier_id, supplier_invoice_number),
    -- Target of the composite FK from the AR/AP open-item subledger (same party, Part 1E batch).
    UNIQUE KEY uq_supplier_invoices_id_supplier (supplier_invoice_id, supplier_id),
    KEY ix_supplier_invoices_registration_date (company_gst_registration_id, invoice_date),
    KEY ix_supplier_invoices_due (due_date),
    -- The billing supplier is always the PO's supplier (D80).
    CONSTRAINT fk_supplier_invoices_po_supplier FOREIGN KEY (purchase_order_id, supplier_id)
        REFERENCES purchase_order_headers (purchase_order_id, supplier_id),
    CONSTRAINT fk_supplier_invoices_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (supplier_id),
    CONSTRAINT fk_supplier_invoices_registration FOREIGN KEY (company_gst_registration_id)
        REFERENCES company_gst_registrations (company_gst_registration_id),
    CONSTRAINT fk_supplier_invoices_approved_by FOREIGN KEY (approved_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_supplier_invoices_reference CHECK (REGEXP_LIKE(internal_reference, '^PINV/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_supplier_invoices_gstin CHECK (supplier_gstin IS NULL
        OR REGEXP_LIKE(supplier_gstin, '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$', 'c')),
    CONSTRAINT ck_supplier_invoices_dates CHECK (received_date >= invoice_date AND due_date >= invoice_date),
    CONSTRAINT ck_supplier_invoices_amounts CHECK (
        taxable_amount >= 0 AND cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0
        AND cgst_amount = sgst_amount AND round_off_amount BETWEEN -0.50 AND 0.50
        AND total_amount = taxable_amount + cgst_amount + sgst_amount + igst_amount + round_off_amount
        AND total_amount = ROUND(total_amount, 0)),
    CONSTRAINT ck_supplier_invoices_tax_mode CHECK ((is_inter_state AND cgst_amount = 0) OR (NOT is_inter_state AND igst_amount = 0)),
    CONSTRAINT ck_supplier_invoices_unregistered_no_tax CHECK (
        supplier_gstin IS NOT NULL OR (cgst_amount = 0 AND sgst_amount = 0 AND igst_amount = 0)),
    CONSTRAINT ck_supplier_invoices_match CHECK (REGEXP_LIKE(match_status, '^(MATCHED|PRICE_VARIANCE|QUANTITY_HOLD)$', 'c')),
    CONSTRAINT ck_supplier_invoices_status CHECK (REGEXP_LIKE(invoice_status, '^(RECEIVED|ON_HOLD|APPROVED|CANCELLED)$', 'c')),
    CONSTRAINT ck_supplier_invoices_approval_pair CHECK ((approved_by_employee_id IS NULL) = (approved_at IS NULL)),
    CONSTRAINT ck_supplier_invoices_approved CHECK ((invoice_status = 'APPROVED') = (approved_at IS NOT NULL)),
    CONSTRAINT ck_supplier_invoices_hold CHECK (match_status <> 'QUANTITY_HOLD' OR invoice_status <> 'APPROVED'),
    CONSTRAINT ck_supplier_invoices_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Supplier tax invoice, three-way matched to PO and receipts; paid status lives in AP (1C 3)';

CREATE TABLE supplier_invoice_line_items (
    supplier_invoice_line_item_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    supplier_invoice_id         BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    purchase_order_line_item_id BIGINT UNSIGNED  NOT NULL,
    invoiced_quantity           DECIMAL(18,3)    NOT NULL,
    unit_price                  DECIMAL(18,4)    NOT NULL,
    discount_percent            DECIMAL(7,4)     NOT NULL DEFAULT 0,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    gst_rate                    DECIMAL(7,4)     NOT NULL,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    line_total                  DECIMAL(18,2)    NOT NULL,
    po_unit_price               DECIMAL(18,4)    NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (supplier_invoice_line_item_id),
    UNIQUE KEY uq_supplier_invoice_line_items_invoice_line (supplier_invoice_id, line_number),
    UNIQUE KEY uq_supplier_invoice_line_items_invoice_po_line (supplier_invoice_id, purchase_order_line_item_id),
    CONSTRAINT fk_supplier_invoice_line_items_invoice FOREIGN KEY (supplier_invoice_id)
        REFERENCES supplier_invoices (supplier_invoice_id),
    CONSTRAINT fk_supplier_invoice_line_items_po_line FOREIGN KEY (purchase_order_line_item_id)
        REFERENCES purchase_order_line_items (purchase_order_line_item_id),
    CONSTRAINT ck_supplier_invoice_line_items_amounts CHECK (
        line_number > 0 AND invoiced_quantity > 0 AND unit_price > 0 AND po_unit_price > 0
        AND discount_percent BETWEEN 0 AND 100 AND gst_rate BETWEEN 0 AND 50
        AND taxable_amount = ROUND(invoiced_quantity * unit_price * (1 - discount_percent / 100), 2)
        AND line_total = taxable_amount + cgst_amount + sgst_amount + igst_amount),
    CONSTRAINT ck_supplier_invoice_line_items_tax CHECK (
        (igst_amount = 0 AND cgst_amount = ROUND(taxable_amount * gst_rate / 200, 2) AND sgst_amount = cgst_amount)
     OR (cgst_amount = 0 AND sgst_amount = 0 AND igst_amount = ROUND(taxable_amount * gst_rate / 100, 2))),
    CONSTRAINT ck_supplier_invoice_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Billed quantity and price per PO line; po_unit_price snapshot for price variance (1C 3, D35)';

CREATE TABLE supplier_payments (
    supplier_payment_id         BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    payment_number              VARCHAR(30)      NOT NULL,
    supplier_id                 BIGINT UNSIGNED  NOT NULL,
    payment_date                DATE             NOT NULL,
    bank_account_id             SMALLINT UNSIGNED NOT NULL,
    payment_method_id           SMALLINT UNSIGNED NOT NULL,
    amount                      DECIMAL(18,2)    NOT NULL,
    payment_type                VARCHAR(30)      NOT NULL,
    bank_reference              VARCHAR(40)      NULL,
    payment_status              VARCHAR(30)      NOT NULL DEFAULT 'INITIATED',
    failure_reason              VARCHAR(100)     NULL,
    approved_by_employee_id     BIGINT UNSIGNED  NOT NULL,
    processed_at                DATETIME(6)      NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (supplier_payment_id),
    UNIQUE KEY uq_supplier_payments_number (payment_number),
    -- Target of the composite FK from the AR/AP open-item subledger (same party, Part 1E batch).
    UNIQUE KEY uq_supplier_payments_id_supplier (supplier_payment_id, supplier_id),
    KEY ix_supplier_payments_supplier_date (supplier_id, payment_date),
    KEY ix_supplier_payments_status (payment_status),
    CONSTRAINT fk_supplier_payments_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (supplier_id),
    CONSTRAINT fk_supplier_payments_bank_account FOREIGN KEY (bank_account_id) REFERENCES bank_accounts (bank_account_id),
    CONSTRAINT fk_supplier_payments_method FOREIGN KEY (payment_method_id) REFERENCES payment_methods (payment_method_id),
    CONSTRAINT fk_supplier_payments_approved_by FOREIGN KEY (approved_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_supplier_payments_number CHECK (REGEXP_LIKE(payment_number, '^SPAY/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_supplier_payments_amount CHECK (amount > 0),
    CONSTRAINT ck_supplier_payments_type CHECK (REGEXP_LIKE(payment_type, '^(AGAINST_INVOICE|ADVANCE)$', 'c')),
    CONSTRAINT ck_supplier_payments_status CHECK (REGEXP_LIKE(payment_status, '^(INITIATED|COMPLETED|FAILED|REVERSED)$', 'c')),
    CONSTRAINT ck_supplier_payments_failure CHECK ((payment_status = 'FAILED') = (failure_reason IS NOT NULL)),
    CONSTRAINT ck_supplier_payments_processed CHECK (payment_status = 'INITIATED' OR processed_at IS NOT NULL),
    CONSTRAINT ck_supplier_payments_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Money paid to a supplier; allocation to invoices in supplier_payment_allocations (1C 3)';
