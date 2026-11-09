-- Workspace schema, batch 2 (Part 1C procurement), file 1/3: RFQs and supplier quotations.
-- Design: docs/design/phase1_part1c_procurement_and_inventory.md §3. Conventions: see 0101_reference.sql.
--
-- Amount rules (1C §2.3, D84): the documented formulas are enforced as CHECKs on every taxable line:
--   taxable    = ROUND(qty * unit_price * (1 - discount_percent / 100), 2)
--   intra-state: cgst = sgst = ROUND(taxable * gst_rate / 200, 2), igst = 0
--   inter-state: igst = ROUND(taxable * gst_rate / 100, 2), cgst = sgst = 0
--   line_total = taxable + cgst + sgst + igst
-- Headers: sums are non-negative, cgst = sgst, |round_off| <= 0.50, total = sum + round_off, whole rupees,
-- and the tax heads used match is_inter_state. Header = SUM(lines) is cross-row: validation V-PR-01.
-- MySQL ROUND and Python Decimal ROUND_HALF_UP agree exactly on these expressions (step 2.4 test A10).

CREATE TABLE rfq_headers (
    rfq_id                      BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    rfq_number                  VARCHAR(30)      NOT NULL,
    rfq_date                    DATE             NOT NULL,
    rfq_type                    VARCHAR(30)      NOT NULL,
    delivery_warehouse_id       SMALLINT UNSIGNED NOT NULL,
    business_unit_id            SMALLINT UNSIGNED NOT NULL,
    response_due_date           DATE             NOT NULL,
    required_by_date            DATE             NOT NULL,
    rfq_status                  VARCHAR(30)      NOT NULL DEFAULT 'DRAFT',
    created_by_employee_id      BIGINT UNSIGNED  NOT NULL,
    awarded_at                  DATETIME(6)      NULL,
    cancellation_reason         VARCHAR(255)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (rfq_id),
    UNIQUE KEY uq_rfq_headers_number (rfq_number),
    KEY ix_rfq_headers_status_date (rfq_status, rfq_date),
    KEY ix_rfq_headers_warehouse_date (delivery_warehouse_id, rfq_date),
    CONSTRAINT fk_rfq_headers_warehouse FOREIGN KEY (delivery_warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT fk_rfq_headers_business_unit FOREIGN KEY (business_unit_id) REFERENCES business_units (business_unit_id),
    CONSTRAINT fk_rfq_headers_created_by FOREIGN KEY (created_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_rfq_headers_number CHECK (REGEXP_LIKE(rfq_number, '^RFQ/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_rfq_headers_type CHECK (REGEXP_LIKE(rfq_type, '^(REPLENISHMENT|NEW_PRODUCT|SEASONAL_BUILD|BULK_DEMAND)$', 'c')),
    CONSTRAINT ck_rfq_headers_status CHECK (REGEXP_LIKE(rfq_status,
        '^(DRAFT|ISSUED|UNDER_EVALUATION|AWARDED|PARTIALLY_AWARDED|NO_AWARD|CANCELLED)$', 'c')),
    CONSTRAINT ck_rfq_headers_dates CHECK (response_due_date >= rfq_date AND required_by_date >= response_due_date),
    CONSTRAINT ck_rfq_headers_awarded CHECK ((rfq_status IN ('AWARDED', 'PARTIALLY_AWARDED')) = (awarded_at IS NOT NULL)),
    CONSTRAINT ck_rfq_headers_cancelled CHECK ((rfq_status = 'CANCELLED') = (cancellation_reason IS NOT NULL)),
    CONSTRAINT ck_rfq_headers_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Request for quotation for one delivery warehouse and business unit (1C 3)';

CREATE TABLE rfq_line_items (
    rfq_line_item_id            BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    rfq_id                      BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    product_id                  BIGINT UNSIGNED  NOT NULL,
    requested_quantity          DECIMAL(18,3)    NOT NULL,
    target_unit_price           DECIMAL(18,4)    NULL,
    line_status                 VARCHAR(30)      NOT NULL DEFAULT 'OPEN',
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (rfq_line_item_id),
    UNIQUE KEY uq_rfq_line_items_rfq_line (rfq_id, line_number),
    UNIQUE KEY uq_rfq_line_items_rfq_product (rfq_id, product_id),
    CONSTRAINT fk_rfq_line_items_rfq FOREIGN KEY (rfq_id) REFERENCES rfq_headers (rfq_id),
    CONSTRAINT fk_rfq_line_items_product FOREIGN KEY (product_id) REFERENCES products (product_id),
    CONSTRAINT ck_rfq_line_items_values CHECK (
        line_number > 0 AND requested_quantity > 0 AND (target_unit_price IS NULL OR target_unit_price > 0)),
    CONSTRAINT ck_rfq_line_items_status CHECK (REGEXP_LIKE(line_status, '^(OPEN|AWARDED|NOT_AWARDED|CANCELLED)$', 'c')),
    CONSTRAINT ck_rfq_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Products and quantities requested in an RFQ (1C 3)';

CREATE TABLE rfq_suppliers (
    rfq_supplier_id             BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    rfq_id                      BIGINT UNSIGNED  NOT NULL,
    supplier_id                 BIGINT UNSIGNED  NOT NULL,
    invited_date                DATE             NOT NULL,
    response_status             VARCHAR(30)      NOT NULL DEFAULT 'INVITED',
    responded_date              DATE             NULL,
    decline_reason              VARCHAR(100)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (rfq_supplier_id),
    UNIQUE KEY uq_rfq_suppliers_rfq_supplier (rfq_id, supplier_id),
    CONSTRAINT fk_rfq_suppliers_rfq FOREIGN KEY (rfq_id) REFERENCES rfq_headers (rfq_id),
    CONSTRAINT fk_rfq_suppliers_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (supplier_id),
    CONSTRAINT ck_rfq_suppliers_status CHECK (REGEXP_LIKE(response_status, '^(INVITED|QUOTED|DECLINED|NO_RESPONSE)$', 'c')),
    CONSTRAINT ck_rfq_suppliers_responded CHECK ((response_status IN ('QUOTED', 'DECLINED')) = (responded_date IS NOT NULL)),
    CONSTRAINT ck_rfq_suppliers_declined CHECK ((response_status = 'DECLINED') = (decline_reason IS NOT NULL)),
    CONSTRAINT ck_rfq_suppliers_dates CHECK (responded_date IS NULL OR responded_date >= invited_date),
    CONSTRAINT ck_rfq_suppliers_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Suppliers invited to an RFQ and their response (M:N RFQ-supplier, 1C 3)';

CREATE TABLE rfq_status_history (
    rfq_status_history_id       BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    rfq_id                      BIGINT UNSIGNED  NOT NULL,
    from_status                 VARCHAR(30)      NULL,
    to_status                   VARCHAR(30)      NOT NULL,
    changed_at                  DATETIME(6)      NOT NULL,
    changed_by_employee_id      BIGINT UNSIGNED  NULL,
    change_reason               VARCHAR(255)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (rfq_status_history_id),
    KEY ix_rfq_status_history_rfq_changed (rfq_id, changed_at),
    CONSTRAINT fk_rfq_status_history_rfq FOREIGN KEY (rfq_id) REFERENCES rfq_headers (rfq_id),
    CONSTRAINT fk_rfq_status_history_changed_by FOREIGN KEY (changed_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_rfq_status_history_to CHECK (REGEXP_LIKE(to_status,
        '^(DRAFT|ISSUED|UNDER_EVALUATION|AWARDED|PARTIALLY_AWARDED|NO_AWARD|CANCELLED)$', 'c')),
    CONSTRAINT ck_rfq_status_history_from CHECK (from_status IS NULL OR (from_status <> to_status AND REGEXP_LIKE(from_status,
        '^(DRAFT|ISSUED|UNDER_EVALUATION|AWARDED|PARTIALLY_AWARDED|NO_AWARD|CANCELLED)$', 'c'))),
    CONSTRAINT ck_rfq_status_history_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'RFQ lifecycle, append-only (1C 2.2)';

CREATE TABLE quotation_headers (
    quotation_id                BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    quotation_number            VARCHAR(30)      NOT NULL,
    rfq_supplier_id             BIGINT UNSIGNED  NOT NULL,
    supplier_quotation_reference VARCHAR(40)     NOT NULL,
    quotation_date              DATE             NOT NULL,
    valid_until_date            DATE             NOT NULL,
    payment_term_id             SMALLINT UNSIGNED NOT NULL,
    is_inter_state              BOOLEAN          NOT NULL,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    round_off_amount            DECIMAL(18,2)    NOT NULL DEFAULT 0,
    total_amount                DECIMAL(18,2)    NOT NULL,
    evaluation_score            DECIMAL(5,2)     NULL,
    evaluated_by_employee_id    BIGINT UNSIGNED  NULL,
    evaluated_date              DATE             NULL,
    quotation_status            VARCHAR(30)      NOT NULL DEFAULT 'RECEIVED',
    rejection_reason            VARCHAR(100)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (quotation_id),
    UNIQUE KEY uq_quotation_headers_number (quotation_number),
    UNIQUE KEY uq_quotation_headers_rfq_supplier (rfq_supplier_id),
    CONSTRAINT fk_quotation_headers_rfq_supplier FOREIGN KEY (rfq_supplier_id) REFERENCES rfq_suppliers (rfq_supplier_id),
    CONSTRAINT fk_quotation_headers_payment_term FOREIGN KEY (payment_term_id) REFERENCES payment_terms (payment_term_id),
    CONSTRAINT fk_quotation_headers_evaluated_by FOREIGN KEY (evaluated_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_quotation_headers_number CHECK (REGEXP_LIKE(quotation_number, '^QTN/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_quotation_headers_dates CHECK (valid_until_date >= quotation_date),
    CONSTRAINT ck_quotation_headers_amounts CHECK (
        taxable_amount >= 0 AND cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0
        AND cgst_amount = sgst_amount AND round_off_amount BETWEEN -0.50 AND 0.50
        AND total_amount = taxable_amount + cgst_amount + sgst_amount + igst_amount + round_off_amount
        AND total_amount = ROUND(total_amount, 0)),
    CONSTRAINT ck_quotation_headers_tax_mode CHECK ((is_inter_state AND cgst_amount = 0) OR (NOT is_inter_state AND igst_amount = 0)),
    CONSTRAINT ck_quotation_headers_evaluation CHECK (
        (evaluation_score IS NULL) = (evaluated_date IS NULL) AND (evaluated_date IS NULL) = (evaluated_by_employee_id IS NULL)
        AND (evaluation_score IS NULL OR evaluation_score BETWEEN 0 AND 100)),
    CONSTRAINT ck_quotation_headers_status CHECK (REGEXP_LIKE(quotation_status,
        '^(RECEIVED|UNDER_EVALUATION|ACCEPTED|PARTIALLY_ACCEPTED|REJECTED|EXPIRED|WITHDRAWN)$', 'c')),
    CONSTRAINT ck_quotation_headers_rejected CHECK ((quotation_status = 'REJECTED') = (rejection_reason IS NOT NULL)),
    CONSTRAINT ck_quotation_headers_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Supplier quotation answering one RFQ invitation (1C 3)';

CREATE TABLE quotation_line_items (
    quotation_line_item_id      BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    quotation_id                BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    rfq_line_item_id            BIGINT UNSIGNED  NOT NULL,
    quoted_quantity             DECIMAL(18,3)    NOT NULL,
    unit_price                  DECIMAL(18,4)    NOT NULL,
    discount_percent            DECIMAL(7,4)     NOT NULL DEFAULT 0,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    gst_rate                    DECIMAL(7,4)     NOT NULL,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    line_total                  DECIMAL(18,2)    NOT NULL,
    lead_time_days              SMALLINT UNSIGNED NOT NULL,
    is_awarded                  BOOLEAN          NOT NULL DEFAULT FALSE,
    awarded_rfq_line_key        BIGINT UNSIGNED  GENERATED ALWAYS AS (IF(is_awarded, rfq_line_item_id, NULL)) STORED,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (quotation_line_item_id),
    UNIQUE KEY uq_quotation_line_items_quotation_line (quotation_id, line_number),
    UNIQUE KEY uq_quotation_line_items_quotation_rfq_line (quotation_id, rfq_line_item_id),
    -- Each RFQ line is awarded to at most one quotation line (1C D30).
    UNIQUE KEY uq_quotation_line_items_awarded (awarded_rfq_line_key),
    CONSTRAINT fk_quotation_line_items_quotation FOREIGN KEY (quotation_id) REFERENCES quotation_headers (quotation_id),
    CONSTRAINT fk_quotation_line_items_rfq_line FOREIGN KEY (rfq_line_item_id) REFERENCES rfq_line_items (rfq_line_item_id),
    CONSTRAINT ck_quotation_line_items_amounts CHECK (
        line_number > 0 AND quoted_quantity > 0 AND unit_price > 0
        AND discount_percent BETWEEN 0 AND 100 AND gst_rate BETWEEN 0 AND 50
        AND taxable_amount = ROUND(quoted_quantity * unit_price * (1 - discount_percent / 100), 2)
        AND line_total = taxable_amount + cgst_amount + sgst_amount + igst_amount),
    CONSTRAINT ck_quotation_line_items_tax CHECK (
        (igst_amount = 0 AND cgst_amount = ROUND(taxable_amount * gst_rate / 200, 2) AND sgst_amount = cgst_amount)
     OR (cgst_amount = 0 AND sgst_amount = 0 AND igst_amount = ROUND(taxable_amount * gst_rate / 100, 2))),
    CONSTRAINT ck_quotation_line_items_lead_time CHECK (lead_time_days BETWEEN 1 AND 120),
    CONSTRAINT ck_quotation_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Quoted price, quantity and lead time per RFQ line; award flag (1C 3, D30)';

CREATE TABLE quotation_status_history (
    quotation_status_history_id BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    quotation_id                BIGINT UNSIGNED  NOT NULL,
    from_status                 VARCHAR(30)      NULL,
    to_status                   VARCHAR(30)      NOT NULL,
    changed_at                  DATETIME(6)      NOT NULL,
    changed_by_employee_id      BIGINT UNSIGNED  NULL,
    change_reason               VARCHAR(255)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (quotation_status_history_id),
    KEY ix_quotation_status_history_quotation_changed (quotation_id, changed_at),
    CONSTRAINT fk_quotation_status_history_quotation FOREIGN KEY (quotation_id) REFERENCES quotation_headers (quotation_id),
    CONSTRAINT fk_quotation_status_history_changed_by FOREIGN KEY (changed_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_quotation_status_history_to CHECK (REGEXP_LIKE(to_status,
        '^(RECEIVED|UNDER_EVALUATION|ACCEPTED|PARTIALLY_ACCEPTED|REJECTED|EXPIRED|WITHDRAWN)$', 'c')),
    CONSTRAINT ck_quotation_status_history_from CHECK (from_status IS NULL OR (from_status <> to_status AND REGEXP_LIKE(from_status,
        '^(RECEIVED|UNDER_EVALUATION|ACCEPTED|PARTIALLY_ACCEPTED|REJECTED|EXPIRED|WITHDRAWN)$', 'c'))),
    CONSTRAINT ck_quotation_status_history_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Quotation lifecycle, append-only (1C 2.2)';
