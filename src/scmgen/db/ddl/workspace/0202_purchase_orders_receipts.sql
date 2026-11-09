-- Workspace schema, batch 2 (Part 1C procurement), file 2/3: purchase orders, goods receipts, inspections.
-- Design: docs/design/phase1_part1c_procurement_and_inventory.md §3. Conventions: see 0101_reference.sql;
-- amount rules: see 0201_rfq_quotations.sql (D84).

CREATE TABLE purchase_order_headers (
    purchase_order_id           BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    po_number                   VARCHAR(30)      NOT NULL,
    po_date                     DATE             NOT NULL,
    po_source                   VARCHAR(30)      NOT NULL,
    quotation_id                BIGINT UNSIGNED  NULL,
    supplier_id                 BIGINT UNSIGNED  NOT NULL,
    supplier_address_id         BIGINT UNSIGNED  NOT NULL,
    delivery_warehouse_id       SMALLINT UNSIGNED NOT NULL,
    payment_term_id             SMALLINT UNSIGNED NOT NULL,
    is_inter_state              BOOLEAN          NOT NULL,
    expected_delivery_date      DATE             NOT NULL,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    round_off_amount            DECIMAL(18,2)    NOT NULL DEFAULT 0,
    total_amount                DECIMAL(18,2)    NOT NULL,
    po_status                   VARCHAR(30)      NOT NULL DEFAULT 'DRAFT',
    created_by_employee_id      BIGINT UNSIGNED  NOT NULL,
    approved_by_employee_id     BIGINT UNSIGNED  NULL,
    approved_at                 DATETIME(6)      NULL,
    cancellation_reason         VARCHAR(255)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (purchase_order_id),
    UNIQUE KEY uq_purchase_order_headers_number (po_number),
    -- One PO per awarded quotation; many NULLs allowed for direct reorders.
    UNIQUE KEY uq_purchase_order_headers_quotation (quotation_id),
    -- Targets of composite FKs: goods_receipts (warehouse) and supplier_invoices (supplier, D80).
    UNIQUE KEY uq_purchase_order_headers_id_warehouse (purchase_order_id, delivery_warehouse_id),
    UNIQUE KEY uq_purchase_order_headers_id_supplier (purchase_order_id, supplier_id),
    KEY ix_purchase_order_headers_supplier_date (supplier_id, po_date),
    KEY ix_purchase_order_headers_status_expected (po_status, expected_delivery_date),
    CONSTRAINT fk_purchase_order_headers_quotation FOREIGN KEY (quotation_id) REFERENCES quotation_headers (quotation_id),
    CONSTRAINT fk_purchase_order_headers_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (supplier_id),
    CONSTRAINT fk_purchase_order_headers_supplier_address FOREIGN KEY (supplier_address_id)
        REFERENCES supplier_addresses (supplier_address_id),
    CONSTRAINT fk_purchase_order_headers_warehouse FOREIGN KEY (delivery_warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT fk_purchase_order_headers_payment_term FOREIGN KEY (payment_term_id) REFERENCES payment_terms (payment_term_id),
    CONSTRAINT fk_purchase_order_headers_created_by FOREIGN KEY (created_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT fk_purchase_order_headers_approved_by FOREIGN KEY (approved_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_purchase_order_headers_number CHECK (REGEXP_LIKE(po_number, '^PO/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_purchase_order_headers_source CHECK (REGEXP_LIKE(po_source, '^(RFQ_AWARD|DIRECT_REORDER)$', 'c')),
    CONSTRAINT ck_purchase_order_headers_quotation CHECK ((po_source = 'RFQ_AWARD') = (quotation_id IS NOT NULL)),
    CONSTRAINT ck_purchase_order_headers_amounts CHECK (
        taxable_amount >= 0 AND cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0
        AND cgst_amount = sgst_amount AND round_off_amount BETWEEN -0.50 AND 0.50
        AND total_amount = taxable_amount + cgst_amount + sgst_amount + igst_amount + round_off_amount
        AND total_amount = ROUND(total_amount, 0)),
    CONSTRAINT ck_purchase_order_headers_tax_mode CHECK ((is_inter_state AND cgst_amount = 0) OR (NOT is_inter_state AND igst_amount = 0)),
    CONSTRAINT ck_purchase_order_headers_status CHECK (REGEXP_LIKE(po_status,
        '^(DRAFT|PENDING_APPROVAL|APPROVED|SENT|PARTIALLY_RECEIVED|RECEIVED|CLOSED|CANCELLED|SHORT_CLOSED)$', 'c')),
    CONSTRAINT ck_purchase_order_headers_approval_pair CHECK ((approved_by_employee_id IS NULL) = (approved_at IS NULL)),
    CONSTRAINT ck_purchase_order_headers_approved CHECK (po_status IN ('DRAFT', 'PENDING_APPROVAL', 'CANCELLED') OR approved_at IS NOT NULL),
    CONSTRAINT ck_purchase_order_headers_cancelled CHECK (
        (po_status IN ('CANCELLED', 'SHORT_CLOSED')) = (cancellation_reason IS NOT NULL)),
    CONSTRAINT ck_purchase_order_headers_dates CHECK (expected_delivery_date >= po_date),
    CONSTRAINT ck_purchase_order_headers_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Purchase order from an RFQ award or a direct reorder (1C 3, D30)';

CREATE TABLE purchase_order_line_items (
    purchase_order_line_item_id BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    purchase_order_id           BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    product_id                  BIGINT UNSIGNED  NOT NULL,
    quotation_line_item_id      BIGINT UNSIGNED  NULL,
    ordered_quantity            DECIMAL(18,3)    NOT NULL,
    unit_price                  DECIMAL(18,4)    NOT NULL,
    discount_percent            DECIMAL(7,4)     NOT NULL DEFAULT 0,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    gst_rate                    DECIMAL(7,4)     NOT NULL,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    line_total                  DECIMAL(18,2)    NOT NULL,
    expected_delivery_date      DATE             NOT NULL,
    quantity_received           DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_accepted           DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_rejected           DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_invoiced           DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_cancelled          DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_open               DECIMAL(18,3)    GENERATED ALWAYS AS
        (ordered_quantity - quantity_accepted - quantity_cancelled) STORED,
    line_status                 VARCHAR(30)      NOT NULL DEFAULT 'OPEN',
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (purchase_order_line_item_id),
    UNIQUE KEY uq_purchase_order_line_items_po_line (purchase_order_id, line_number),
    UNIQUE KEY uq_purchase_order_line_items_po_product (purchase_order_id, product_id),
    UNIQUE KEY uq_purchase_order_line_items_quotation_line (quotation_line_item_id),
    KEY ix_purchase_order_line_items_product_status (product_id, line_status),
    CONSTRAINT fk_purchase_order_line_items_po FOREIGN KEY (purchase_order_id) REFERENCES purchase_order_headers (purchase_order_id),
    CONSTRAINT fk_purchase_order_line_items_product FOREIGN KEY (product_id) REFERENCES products (product_id),
    CONSTRAINT fk_purchase_order_line_items_quotation_line FOREIGN KEY (quotation_line_item_id)
        REFERENCES quotation_line_items (quotation_line_item_id),
    CONSTRAINT ck_purchase_order_line_items_amounts CHECK (
        line_number > 0 AND ordered_quantity > 0 AND unit_price > 0
        AND discount_percent BETWEEN 0 AND 100 AND gst_rate BETWEEN 0 AND 50
        AND taxable_amount = ROUND(ordered_quantity * unit_price * (1 - discount_percent / 100), 2)
        AND line_total = taxable_amount + cgst_amount + sgst_amount + igst_amount),
    CONSTRAINT ck_purchase_order_line_items_tax CHECK (
        (igst_amount = 0 AND cgst_amount = ROUND(taxable_amount * gst_rate / 200, 2) AND sgst_amount = cgst_amount)
     OR (cgst_amount = 0 AND sgst_amount = 0 AND igst_amount = ROUND(taxable_amount * gst_rate / 100, 2))),
    -- Counter rules (1C): never negative, accepted+rejected <= received, open quantity never negative,
    -- pay only for accepted goods.
    CONSTRAINT ck_purchase_order_line_items_counters CHECK (
        quantity_received >= 0 AND quantity_accepted >= 0 AND quantity_rejected >= 0
        AND quantity_invoiced >= 0 AND quantity_cancelled >= 0
        AND quantity_accepted + quantity_rejected <= quantity_received
        AND quantity_received <= ordered_quantity + quantity_rejected
        AND quantity_accepted + quantity_cancelled <= ordered_quantity
        AND quantity_invoiced <= quantity_accepted),
    CONSTRAINT ck_purchase_order_line_items_status CHECK (REGEXP_LIKE(line_status,
        '^(OPEN|PARTIALLY_RECEIVED|RECEIVED|CANCELLED|CLOSED)$', 'c')),
    CONSTRAINT ck_purchase_order_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Ordered products with receipt/acceptance/invoice counters (1C 3, D32)';

CREATE TABLE purchase_order_status_history (
    purchase_order_status_history_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    purchase_order_id           BIGINT UNSIGNED  NOT NULL,
    from_status                 VARCHAR(30)      NULL,
    to_status                   VARCHAR(30)      NOT NULL,
    changed_at                  DATETIME(6)      NOT NULL,
    changed_by_employee_id      BIGINT UNSIGNED  NULL,
    change_reason               VARCHAR(255)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (purchase_order_status_history_id),
    KEY ix_purchase_order_status_history_po_changed (purchase_order_id, changed_at),
    CONSTRAINT fk_purchase_order_status_history_po FOREIGN KEY (purchase_order_id)
        REFERENCES purchase_order_headers (purchase_order_id),
    CONSTRAINT fk_purchase_order_status_history_changed_by FOREIGN KEY (changed_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_purchase_order_status_history_to CHECK (REGEXP_LIKE(to_status,
        '^(DRAFT|PENDING_APPROVAL|APPROVED|SENT|PARTIALLY_RECEIVED|RECEIVED|CLOSED|CANCELLED|SHORT_CLOSED)$', 'c')),
    CONSTRAINT ck_purchase_order_status_history_from CHECK (from_status IS NULL OR (from_status <> to_status AND REGEXP_LIKE(from_status,
        '^(DRAFT|PENDING_APPROVAL|APPROVED|SENT|PARTIALLY_RECEIVED|RECEIVED|CLOSED|CANCELLED|SHORT_CLOSED)$', 'c'))),
    CONSTRAINT ck_purchase_order_status_history_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Purchase order lifecycle, append-only (1C 2.2)';

CREATE TABLE goods_receipts (
    goods_receipt_id            BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    grn_number                  VARCHAR(30)      NOT NULL,
    purchase_order_id           BIGINT UNSIGNED  NOT NULL,
    warehouse_id                SMALLINT UNSIGNED NOT NULL,
    receipt_date                DATE             NOT NULL,
    received_at                 DATETIME(6)      NOT NULL,
    supplier_delivery_note_number VARCHAR(40)    NOT NULL,
    eway_bill_number            CHAR(12)         NULL,
    vehicle_number              VARCHAR(15)      NULL,
    received_by_employee_id     BIGINT UNSIGNED  NOT NULL,
    grn_status                  VARCHAR(30)      NOT NULL DEFAULT 'RECEIVED',
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (goods_receipt_id),
    UNIQUE KEY uq_goods_receipts_number (grn_number),
    -- Goods are received only at the warehouse the PO was raised for (1C, composite FK).
    CONSTRAINT fk_goods_receipts_po_warehouse FOREIGN KEY (purchase_order_id, warehouse_id)
        REFERENCES purchase_order_headers (purchase_order_id, delivery_warehouse_id),
    CONSTRAINT fk_goods_receipts_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT fk_goods_receipts_received_by FOREIGN KEY (received_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_goods_receipts_number CHECK (REGEXP_LIKE(grn_number, '^GRN/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_goods_receipts_eway_bill CHECK (eway_bill_number IS NULL OR REGEXP_LIKE(eway_bill_number, '^[0-9]{12}$', 'c')),
    CONSTRAINT ck_goods_receipts_vehicle CHECK (vehicle_number IS NULL
        OR REGEXP_LIKE(vehicle_number, '^[A-Z]{2}[0-9]{2}[A-Z]{1,3}[0-9]{4}$', 'c')),
    CONSTRAINT ck_goods_receipts_date CHECK (DATE(received_at) = receipt_date),
    CONSTRAINT ck_goods_receipts_status CHECK (REGEXP_LIKE(grn_status, '^(RECEIVED|INSPECTED|CANCELLED)$', 'c')),
    CONSTRAINT ck_goods_receipts_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Goods receipt note: one per supplier delivery (1C 3)';

CREATE TABLE goods_receipt_line_items (
    goods_receipt_line_item_id  BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    goods_receipt_id            BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    purchase_order_line_item_id BIGINT UNSIGNED  NOT NULL,
    quantity_received           DECIMAL(18,3)    NOT NULL,
    receiving_location_id       BIGINT UNSIGNED  NOT NULL,
    supplier_batch_number       VARCHAR(40)      NULL,
    manufacture_date            DATE             NULL,
    expiry_date                 DATE             NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (goods_receipt_line_item_id),
    UNIQUE KEY uq_goods_receipt_line_items_grn_line (goods_receipt_id, line_number),
    UNIQUE KEY uq_goods_receipt_line_items_grn_po_line (goods_receipt_id, purchase_order_line_item_id),
    CONSTRAINT fk_goods_receipt_line_items_grn FOREIGN KEY (goods_receipt_id) REFERENCES goods_receipts (goods_receipt_id),
    CONSTRAINT fk_goods_receipt_line_items_po_line FOREIGN KEY (purchase_order_line_item_id)
        REFERENCES purchase_order_line_items (purchase_order_line_item_id),
    CONSTRAINT fk_goods_receipt_line_items_location FOREIGN KEY (receiving_location_id)
        REFERENCES warehouse_locations (warehouse_location_id),
    CONSTRAINT ck_goods_receipt_line_items_values CHECK (line_number > 0 AND quantity_received > 0),
    CONSTRAINT ck_goods_receipt_line_items_dates CHECK (
        expiry_date IS NULL OR manufacture_date IS NULL OR expiry_date > manufacture_date),
    CONSTRAINT ck_goods_receipt_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Quantity received per PO line in one delivery (1C 3)';

CREATE TABLE goods_inspections (
    goods_inspection_id         BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    goods_receipt_line_item_id  BIGINT UNSIGNED  NOT NULL,
    inspected_at                DATETIME(6)      NOT NULL,
    inspected_by_employee_id    BIGINT UNSIGNED  NOT NULL,
    quantity_accepted           DECIMAL(18,3)    NOT NULL,
    quantity_rejected           DECIMAL(18,3)    NOT NULL,
    inspection_result           VARCHAR(30)      NOT NULL,
    rejection_reason            VARCHAR(30)      NULL,
    inspection_notes            VARCHAR(255)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (goods_inspection_id),
    UNIQUE KEY uq_goods_inspections_grn_line (goods_receipt_line_item_id),
    CONSTRAINT fk_goods_inspections_grn_line FOREIGN KEY (goods_receipt_line_item_id)
        REFERENCES goods_receipt_line_items (goods_receipt_line_item_id),
    CONSTRAINT fk_goods_inspections_inspected_by FOREIGN KEY (inspected_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_goods_inspections_quantities CHECK (
        quantity_accepted >= 0 AND quantity_rejected >= 0 AND quantity_accepted + quantity_rejected > 0),
    CONSTRAINT ck_goods_inspections_result CHECK (REGEXP_LIKE(inspection_result, '^(ACCEPTED|PARTIALLY_ACCEPTED|REJECTED)$', 'c')),
    CONSTRAINT ck_goods_inspections_result_quantities CHECK (
        (inspection_result = 'ACCEPTED') = (quantity_rejected = 0) AND (inspection_result = 'REJECTED') = (quantity_accepted = 0)),
    CONSTRAINT ck_goods_inspections_reason CHECK (rejection_reason IS NULL OR REGEXP_LIKE(rejection_reason,
        '^(DAMAGED_IN_TRANSIT|WRONG_ITEM|QUALITY_FAILED|SHORT_SHELF_LIFE|EXCESS_QUANTITY)$', 'c')),
    CONSTRAINT ck_goods_inspections_reason_required CHECK ((quantity_rejected > 0) = (rejection_reason IS NOT NULL)),
    CONSTRAINT ck_goods_inspections_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Dock inspection: accepted qty enters stock, rejected goes back (1C 3, D31)';
