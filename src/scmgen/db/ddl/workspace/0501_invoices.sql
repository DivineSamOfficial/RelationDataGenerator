-- Workspace schema, batch 5 (Part 1D invoicing & returns), file 1/2: tax invoices and their lines.
-- Design: docs/design/phase1_part1d_sales_shipping_invoicing_returns.md §5 (+ D79, D80, 1D §1.4).
-- Conventions: see 0101_reference.sql; amount rules: see 0201_rfq_quotations.sql (D84).

CREATE TABLE invoices (
    invoice_id                  BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    invoice_number              VARCHAR(16)      NOT NULL,
    order_id                    BIGINT UNSIGNED  NOT NULL,
    shipment_id                 BIGINT UNSIGNED  NULL,
    customer_id                 BIGINT UNSIGNED  NOT NULL,
    company_gst_registration_id SMALLINT UNSIGNED NOT NULL,
    warehouse_id                SMALLINT UNSIGNED NOT NULL,
    invoice_date                DATE             NOT NULL,
    issued_at                   DATETIME(6)      NOT NULL,
    customer_name               VARCHAR(200)     NOT NULL,
    customer_gstin              CHAR(15)         NULL,
    billing_address_id          BIGINT UNSIGNED  NOT NULL,
    shipping_address_id         BIGINT UNSIGNED  NULL,
    place_of_supply_state_id    SMALLINT UNSIGNED NOT NULL,
    is_inter_state              BOOLEAN          NOT NULL,
    payment_term_id             SMALLINT UNSIGNED NOT NULL,
    due_date                    DATE             NOT NULL,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    round_off_amount            DECIMAL(18,2)    NOT NULL DEFAULT 0,
    total_amount                DECIMAL(18,2)    NOT NULL,
    irn                         CHAR(64)         NULL,
    irn_generated_at            DATETIME(6)      NULL,
    invoice_status              VARCHAR(30)      NOT NULL DEFAULT 'ISSUED',
    cancelled_at                DATETIME(6)      NULL,
    cancellation_reason         VARCHAR(100)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (invoice_id),
    UNIQUE KEY uq_invoices_number (invoice_number),
    UNIQUE KEY uq_invoices_shipment (shipment_id),
    UNIQUE KEY uq_invoices_irn (irn),
    -- Target of the composite FK from the AR/AP open-item subledger (same party, Part 1E batch).
    UNIQUE KEY uq_invoices_id_customer (invoice_id, customer_id),
    -- Target of the composite FK from credit_notes (same customer and GSTIN, D80).
    UNIQUE KEY uq_invoices_id_customer_registration (invoice_id, customer_id, company_gst_registration_id),
    KEY ix_invoices_customer_date (customer_id, invoice_date),
    KEY ix_invoices_date (invoice_date),
    KEY ix_invoices_registration_date (company_gst_registration_id, invoice_date),
    KEY ix_invoices_due (due_date),
    -- The invoiced customer is the order's customer (D80).
    CONSTRAINT fk_invoices_order_customer FOREIGN KEY (order_id, customer_id) REFERENCES orders (order_id, customer_id),
    -- The billed shipment belongs to this order and was dispatched from this warehouse (was V-IV-01).
    CONSTRAINT fk_invoices_shipment FOREIGN KEY (shipment_id, order_id, warehouse_id)
        REFERENCES shipments (shipment_id, order_id, warehouse_id),
    -- The issuing GSTIN is the warehouse's own GSTIN (was V-IV-01).
    CONSTRAINT fk_invoices_warehouse_registration FOREIGN KEY (warehouse_id, company_gst_registration_id)
        REFERENCES warehouses (warehouse_id, company_gst_registration_id),
    CONSTRAINT fk_invoices_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    CONSTRAINT fk_invoices_registration FOREIGN KEY (company_gst_registration_id)
        REFERENCES company_gst_registrations (company_gst_registration_id),
    CONSTRAINT fk_invoices_billing_address FOREIGN KEY (billing_address_id, customer_id)
        REFERENCES customer_addresses (customer_address_id, customer_id),
    CONSTRAINT fk_invoices_shipping_address FOREIGN KEY (shipping_address_id, customer_id)
        REFERENCES customer_addresses (customer_address_id, customer_id),
    CONSTRAINT fk_invoices_place_of_supply FOREIGN KEY (place_of_supply_state_id) REFERENCES states (state_id),
    CONSTRAINT fk_invoices_payment_term FOREIGN KEY (payment_term_id) REFERENCES payment_terms (payment_term_id),
    -- GST tax document number: max 16 characters (D38).
    CONSTRAINT ck_invoices_number CHECK (REGEXP_LIKE(invoice_number, '^[A-Z]{2}[0-9]{4}-I[0-9]{6}$', 'c')),
    CONSTRAINT ck_invoices_gstin CHECK (customer_gstin IS NULL
        OR REGEXP_LIKE(customer_gstin, '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$', 'c')),
    CONSTRAINT ck_invoices_dates CHECK (due_date >= invoice_date AND invoice_date = DATE(issued_at)),
    CONSTRAINT ck_invoices_amounts CHECK (
        taxable_amount >= 0 AND cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0
        AND cgst_amount = sgst_amount AND round_off_amount BETWEEN -0.50 AND 0.50
        AND total_amount = taxable_amount + cgst_amount + sgst_amount + igst_amount + round_off_amount
        AND total_amount = ROUND(total_amount, 0)),
    CONSTRAINT ck_invoices_tax_mode CHECK ((is_inter_state AND cgst_amount = 0) OR (NOT is_inter_state AND igst_amount = 0)),
    -- e-invoice reference only for GST-registered buyers (D46).
    CONSTRAINT ck_invoices_irn CHECK (
        (irn IS NULL) = (irn_generated_at IS NULL)
        AND (irn IS NULL OR (customer_gstin IS NOT NULL AND REGEXP_LIKE(irn, '^[0-9a-f]{64}$', 'c')))),
    CONSTRAINT ck_invoices_status CHECK (REGEXP_LIKE(invoice_status, '^(ISSUED|CANCELLED)$', 'c')),
    -- Cancellation only within 24 hours of issue (D40); afterwards corrections are credit notes.
    CONSTRAINT ck_invoices_cancelled CHECK (
        (invoice_status = 'CANCELLED') = (cancelled_at IS NOT NULL AND cancellation_reason IS NOT NULL)
        AND (cancelled_at IS NULL OR (cancelled_at >= issued_at AND cancelled_at <= issued_at + INTERVAL 24 HOUR))),
    CONSTRAINT ck_invoices_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'GST tax invoice: one per shipment at dispatch, or per store handover (1D 5, D4, D38, D40, D46)';

CREATE TABLE invoice_line_items (
    invoice_line_item_id        BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    invoice_id                  BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    line_type                   VARCHAR(20)      NOT NULL,
    order_line_item_id          BIGINT UNSIGNED  NULL,
    shipment_line_item_id       BIGINT UNSIGNED  NULL,
    product_id                  BIGINT UNSIGNED  NULL,
    hsn_code                    VARCHAR(8)       NOT NULL,
    quantity                    DECIMAL(18,3)    NOT NULL,
    unit_price                  DECIMAL(18,4)    NOT NULL,
    inclusive_unit_price        DECIMAL(18,4)    NULL,
    discount_percent            DECIMAL(7,4)     NOT NULL DEFAULT 0,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    gst_rate                    DECIMAL(7,4)     NOT NULL,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    line_total                  DECIMAL(18,2)    NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (invoice_line_item_id),
    UNIQUE KEY uq_invoice_line_items_invoice_line (invoice_id, line_number),
    UNIQUE KEY uq_invoice_line_items_shipment_line (shipment_line_item_id),
    -- Target of the composite FK from sales_return_line_items (D80).
    UNIQUE KEY uq_invoice_line_items_id_order_line (invoice_line_item_id, order_line_item_id),
    KEY ix_invoice_line_items_hsn (hsn_code),
    CONSTRAINT fk_invoice_line_items_invoice FOREIGN KEY (invoice_id) REFERENCES invoices (invoice_id),
    -- The line's product is always the order line's product (D80).
    CONSTRAINT fk_invoice_line_items_order_line_product FOREIGN KEY (order_line_item_id, product_id)
        REFERENCES order_line_items (order_line_item_id, product_id),
    CONSTRAINT fk_invoice_line_items_shipment_line FOREIGN KEY (shipment_line_item_id)
        REFERENCES shipment_line_items (shipment_line_item_id),
    CONSTRAINT fk_invoice_line_items_product FOREIGN KEY (product_id) REFERENCES products (product_id),
    CONSTRAINT ck_invoice_line_items_type CHECK (REGEXP_LIKE(line_type, '^(PRODUCT|SHIPPING_CHARGE)$', 'c')),
    CONSTRAINT ck_invoice_line_items_product_link CHECK (
        (line_type = 'PRODUCT') = (order_line_item_id IS NOT NULL AND product_id IS NOT NULL)
        AND (line_type = 'PRODUCT' OR shipment_line_item_id IS NULL)),
    -- Shipping is billed as one unit at ex-GST price (D42).
    CONSTRAINT ck_invoice_line_items_shipping CHECK (
        line_type = 'PRODUCT' OR (quantity = 1 AND inclusive_unit_price IS NULL)),
    CONSTRAINT ck_invoice_line_items_hsn CHECK (REGEXP_LIKE(hsn_code, '^[0-9]{4}([0-9]{2}){0,2}$', 'c')),
    CONSTRAINT ck_invoice_line_items_prices CHECK (
        line_number > 0 AND quantity > 0 AND unit_price > 0
        AND (inclusive_unit_price IS NULL OR inclusive_unit_price > 0)
        AND discount_percent BETWEEN 0 AND 90 AND gst_rate BETWEEN 0 AND 50),
    -- Taxable value (D84), ex-GST or back-calculated from the GST-inclusive price at the invoice-date rate (D79).
    CONSTRAINT ck_invoice_line_items_taxable CHECK (
        (inclusive_unit_price IS NULL
            AND taxable_amount = ROUND(quantity * unit_price * (1 - discount_percent / 100), 2))
     OR (inclusive_unit_price IS NOT NULL
            AND unit_price = ROUND(inclusive_unit_price / (1 + gst_rate / 100), 4)
            AND taxable_amount = ROUND(quantity * inclusive_unit_price * (1 - discount_percent / 100)
                                       / (1 + gst_rate / 100), 2))),
    CONSTRAINT ck_invoice_line_items_tax CHECK (
        ((igst_amount = 0 AND cgst_amount = ROUND(taxable_amount * gst_rate / 200, 2) AND sgst_amount = cgst_amount)
      OR (cgst_amount = 0 AND sgst_amount = 0 AND igst_amount = ROUND(taxable_amount * gst_rate / 100, 2)))
        AND line_total = taxable_amount + cgst_amount + sgst_amount + igst_amount),
    CONSTRAINT ck_invoice_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Invoiced lines, 1:1 with shipment lines (or order lines for handover), GST split (1D 5, D42, D48)';
