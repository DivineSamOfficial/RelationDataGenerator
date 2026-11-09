-- Workspace schema, batch 3 (Part 1C inventory), file 1/2: balances, batches, reservations,
-- stock adjustments and stock transfers.
-- Design: docs/design/phase1_part1c_procurement_and_inventory.md §4-§5. Conventions: see 0101_reference.sql.
--
-- Deferred foreign key (target table is created in a later batch, added there by ALTER TABLE):
--   inventory_reservations.order_line_item_id -> order_line_items   (sales batch)

CREATE TABLE inventory (
    inventory_id                BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    warehouse_id                SMALLINT UNSIGNED NOT NULL,
    product_id                  BIGINT UNSIGNED  NOT NULL,
    qty_sellable                DECIMAL(18,3)    NOT NULL DEFAULT 0,
    qty_quarantine              DECIMAL(18,3)    NOT NULL DEFAULT 0,
    qty_defective               DECIMAL(18,3)    NOT NULL DEFAULT 0,
    qty_reserved                DECIMAL(18,3)    NOT NULL DEFAULT 0,
    qty_on_hand                 DECIMAL(18,3)    GENERATED ALWAYS AS (qty_sellable + qty_quarantine + qty_defective) STORED,
    qty_available               DECIMAL(18,3)    GENERATED ALWAYS AS (qty_sellable - qty_reserved) STORED,
    stock_value                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    -- Moving-average cost = value / quantity (D27); undefined (NULL) when nothing is on hand.
    moving_average_cost         DECIMAL(18,4)    GENERATED ALWAYS AS
        (IF(qty_sellable + qty_quarantine + qty_defective > 0,
            ROUND(stock_value / (qty_sellable + qty_quarantine + qty_defective), 4), NULL)) STORED,
    last_unit_cost              DECIMAL(18,4)    NULL,
    reorder_level               DECIMAL(18,3)    NOT NULL,
    reorder_quantity            DECIMAL(18,3)    NOT NULL,
    max_stock_level             DECIMAL(18,3)    NOT NULL,
    is_stocked                  BOOLEAN          NOT NULL DEFAULT TRUE,
    last_receipt_at             DATETIME(6)      NULL,
    last_issue_at               DATETIME(6)      NULL,
    last_counted_at             DATETIME(6)      NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (inventory_id),
    UNIQUE KEY uq_inventory_warehouse_product (warehouse_id, product_id),
    KEY ix_inventory_warehouse_stocked (warehouse_id, is_stocked),
    CONSTRAINT fk_inventory_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT fk_inventory_product FOREIGN KEY (product_id) REFERENCES products (product_id),
    -- Inventory never negative, and reservations only hold sellable stock (enforced by the database).
    CONSTRAINT ck_inventory_quantities CHECK (
        qty_sellable >= 0 AND qty_quarantine >= 0 AND qty_defective >= 0
        AND qty_reserved >= 0 AND qty_reserved <= qty_sellable),
    CONSTRAINT ck_inventory_value CHECK (
        stock_value >= 0 AND ((qty_sellable + qty_quarantine + qty_defective = 0) = (stock_value = 0))),
    CONSTRAINT ck_inventory_last_cost CHECK (last_unit_cost IS NULL OR last_unit_cost >= 0),
    CONSTRAINT ck_inventory_policy CHECK (
        reorder_level >= 0 AND reorder_quantity > 0 AND max_stock_level >= reorder_level + reorder_quantity),
    CONSTRAINT ck_inventory_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Balance, value and reorder policy per warehouse x product (1C 5, D26, D27)';

-- Created before stock_adjustment_line_items; its FK to that table is added further down (two-way link,
-- both sides nullable, so rows can always be inserted in order).
CREATE TABLE inventory_batches (
    inventory_batch_id          BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    batch_number                VARCHAR(40)      NOT NULL,
    product_id                  BIGINT UNSIGNED  NOT NULL,
    goods_receipt_line_item_id  BIGINT UNSIGNED  NULL,
    stock_adjustment_line_item_id BIGINT UNSIGNED NULL,
    supplier_batch_number       VARCHAR(40)      NULL,
    manufacture_date            DATE             NULL,
    expiry_date                 DATE             NULL,
    received_date               DATE             NOT NULL,
    initial_quantity            DECIMAL(18,3)    NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (inventory_batch_id),
    UNIQUE KEY uq_inventory_batches_number (batch_number),
    UNIQUE KEY uq_inventory_batches_grn_line (goods_receipt_line_item_id),
    UNIQUE KEY uq_inventory_batches_adjustment_line (stock_adjustment_line_item_id),
    KEY ix_inventory_batches_product_expiry (product_id, expiry_date),
    CONSTRAINT fk_inventory_batches_product FOREIGN KEY (product_id) REFERENCES products (product_id),
    CONSTRAINT fk_inventory_batches_grn_line FOREIGN KEY (goods_receipt_line_item_id)
        REFERENCES goods_receipt_line_items (goods_receipt_line_item_id),
    CONSTRAINT ck_inventory_batches_number CHECK (REGEXP_LIKE(batch_number, '^LOT-[A-Z0-9-]{3,36}$', 'c')),
    CONSTRAINT ck_inventory_batches_one_source CHECK (
        (goods_receipt_line_item_id IS NOT NULL) + (stock_adjustment_line_item_id IS NOT NULL) = 1),
    CONSTRAINT ck_inventory_batches_quantity CHECK (initial_quantity > 0),
    CONSTRAINT ck_inventory_batches_dates CHECK (expiry_date IS NULL OR manufacture_date IS NULL OR expiry_date > manufacture_date),
    CONSTRAINT ck_inventory_batches_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Lot of a batch-tracked product for traceability/FEFO; no balance column, derived from ledger (1C 5, D1)';

CREATE TABLE stock_adjustments (
    stock_adjustment_id         BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    adjustment_number           VARCHAR(30)      NOT NULL,
    warehouse_id                SMALLINT UNSIGNED NOT NULL,
    adjustment_date             DATE             NOT NULL,
    adjustment_type             VARCHAR(30)      NOT NULL,
    reason                      VARCHAR(255)     NULL,
    adjustment_status           VARCHAR(30)      NOT NULL DEFAULT 'DRAFT',
    created_by_employee_id      BIGINT UNSIGNED  NOT NULL,
    approved_by_employee_id     BIGINT UNSIGNED  NULL,
    posted_at                   DATETIME(6)      NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (stock_adjustment_id),
    UNIQUE KEY uq_stock_adjustments_number (adjustment_number),
    KEY ix_stock_adjustments_warehouse_date (warehouse_id, adjustment_date),
    CONSTRAINT fk_stock_adjustments_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT fk_stock_adjustments_created_by FOREIGN KEY (created_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT fk_stock_adjustments_approved_by FOREIGN KEY (approved_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_stock_adjustments_number CHECK (REGEXP_LIKE(adjustment_number, '^ADJ/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_stock_adjustments_type CHECK (REGEXP_LIKE(adjustment_type,
        '^(OPENING_STOCK|CYCLE_COUNT|DAMAGE|SHRINKAGE|FOUND_STOCK|EXPIRY_WRITE_OFF)$', 'c')),
    CONSTRAINT ck_stock_adjustments_status CHECK (REGEXP_LIKE(adjustment_status, '^(DRAFT|PENDING_APPROVAL|POSTED|REJECTED)$', 'c')),
    CONSTRAINT ck_stock_adjustments_posted CHECK ((adjustment_status = 'POSTED') = (posted_at IS NOT NULL)),
    CONSTRAINT ck_stock_adjustments_approval CHECK (
        adjustment_status <> 'POSTED' OR approved_by_employee_id IS NOT NULL OR adjustment_type = 'OPENING_STOCK'),
    CONSTRAINT ck_stock_adjustments_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Stock adjustment document incl. opening stock (1C 5, D34)';

CREATE TABLE stock_adjustment_line_items (
    stock_adjustment_line_item_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    stock_adjustment_id         BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    product_id                  BIGINT UNSIGNED  NOT NULL,
    inventory_batch_id          BIGINT UNSIGNED  NULL,
    stock_status                VARCHAR(30)      NOT NULL,
    system_quantity             DECIMAL(18,3)    NULL,
    counted_quantity            DECIMAL(18,3)    NULL,
    quantity_change             DECIMAL(18,3)    NOT NULL,
    unit_cost                   DECIMAL(18,4)    NOT NULL,
    value_change                DECIMAL(18,2)    NOT NULL,
    reason_code                 VARCHAR(30)      NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (stock_adjustment_line_item_id),
    UNIQUE KEY uq_stock_adjustment_line_items_adjustment_line (stock_adjustment_id, line_number),
    CONSTRAINT fk_stock_adjustment_line_items_adjustment FOREIGN KEY (stock_adjustment_id)
        REFERENCES stock_adjustments (stock_adjustment_id),
    CONSTRAINT fk_stock_adjustment_line_items_product FOREIGN KEY (product_id) REFERENCES products (product_id),
    CONSTRAINT fk_stock_adjustment_line_items_batch FOREIGN KEY (inventory_batch_id)
        REFERENCES inventory_batches (inventory_batch_id),
    CONSTRAINT ck_stock_adjustment_line_items_status CHECK (REGEXP_LIKE(stock_status, '^(SELLABLE|QUARANTINE|DEFECTIVE)$', 'c')),
    CONSTRAINT ck_stock_adjustment_line_items_values CHECK (
        line_number > 0 AND quantity_change <> 0 AND unit_cost >= 0 AND quantity_change * value_change >= 0),
    CONSTRAINT ck_stock_adjustment_line_items_count CHECK (
        (system_quantity IS NULL) = (counted_quantity IS NULL)
        AND (counted_quantity IS NULL OR (counted_quantity >= 0 AND system_quantity >= 0
             AND quantity_change = counted_quantity - system_quantity))),
    CONSTRAINT ck_stock_adjustment_line_items_reason CHECK (REGEXP_LIKE(reason_code,
        '^(OPENING|COUNT_DIFFERENCE|DAMAGED|THEFT_OR_LOSS|EXPIRED|FOUND|RECLASS)$', 'c')),
    CONSTRAINT ck_stock_adjustment_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Products adjusted, one stock-status bucket per line (1C 5, R19)';

ALTER TABLE inventory_batches
    ADD CONSTRAINT fk_inventory_batches_adjustment_line FOREIGN KEY (stock_adjustment_line_item_id)
        REFERENCES stock_adjustment_line_items (stock_adjustment_line_item_id);

CREATE TABLE inventory_reservations (
    inventory_reservation_id    BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    order_line_item_id          BIGINT UNSIGNED  NOT NULL,
    warehouse_id                SMALLINT UNSIGNED NOT NULL,
    reserved_quantity           DECIMAL(18,3)    NOT NULL,
    fulfilled_quantity          DECIMAL(18,3)    NOT NULL DEFAULT 0,
    released_quantity           DECIMAL(18,3)    NOT NULL DEFAULT 0,
    outstanding_quantity        DECIMAL(18,3)    GENERATED ALWAYS AS
        (reserved_quantity - fulfilled_quantity - released_quantity) STORED,
    reserved_at                 DATETIME(6)      NOT NULL,
    closed_at                   DATETIME(6)      NULL,
    reservation_status          VARCHAR(30)      NOT NULL DEFAULT 'ACTIVE',
    release_reason              VARCHAR(100)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (inventory_reservation_id),
    UNIQUE KEY uq_inventory_reservations_order_line_warehouse (order_line_item_id, warehouse_id),
    KEY ix_inventory_reservations_warehouse_status (warehouse_id, reservation_status),
    CONSTRAINT fk_inventory_reservations_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT ck_inventory_reservations_quantities CHECK (
        reserved_quantity > 0 AND fulfilled_quantity >= 0 AND released_quantity >= 0
        AND fulfilled_quantity + released_quantity <= reserved_quantity),
    CONSTRAINT ck_inventory_reservations_status CHECK (REGEXP_LIKE(reservation_status,
        '^(ACTIVE|PARTIALLY_FULFILLED|FULFILLED|RELEASED)$', 'c')),
    CONSTRAINT ck_inventory_reservations_closed CHECK (
        (reservation_status IN ('FULFILLED', 'RELEASED')) = (closed_at IS NOT NULL)),
    CONSTRAINT ck_inventory_reservations_release_reason CHECK ((released_quantity > 0) = (release_reason IS NOT NULL)),
    CONSTRAINT ck_inventory_reservations_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Sellable stock held for an order line at a warehouse; not a movement (1C 5)';

CREATE TABLE stock_transfers (
    stock_transfer_id           BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    transfer_number             VARCHAR(30)      NOT NULL,
    transfer_type               VARCHAR(30)      NOT NULL,
    source_warehouse_id         SMALLINT UNSIGNED NOT NULL,
    destination_warehouse_id    SMALLINT UNSIGNED NOT NULL,
    is_inter_gstin              BOOLEAN          NOT NULL,
    tax_invoice_number          VARCHAR(16)      NULL,
    eway_bill_number            CHAR(12)         NULL,
    shipping_carrier_id         SMALLINT UNSIGNED NULL,
    requested_date              DATE             NOT NULL,
    dispatched_at               DATETIME(6)      NULL,
    expected_arrival_date       DATE             NULL,
    received_at                 DATETIME(6)      NULL,
    taxable_amount              DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    transfer_status             VARCHAR(30)      NOT NULL DEFAULT 'DRAFT',
    created_by_employee_id      BIGINT UNSIGNED  NOT NULL,
    approved_by_employee_id     BIGINT UNSIGNED  NULL,
    received_by_employee_id     BIGINT UNSIGNED  NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (stock_transfer_id),
    UNIQUE KEY uq_stock_transfers_number (transfer_number),
    UNIQUE KEY uq_stock_transfers_tax_invoice (tax_invoice_number),
    KEY ix_stock_transfers_status (transfer_status),
    CONSTRAINT fk_stock_transfers_source FOREIGN KEY (source_warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT fk_stock_transfers_destination FOREIGN KEY (destination_warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT fk_stock_transfers_carrier FOREIGN KEY (shipping_carrier_id) REFERENCES shipping_carriers (shipping_carrier_id),
    CONSTRAINT fk_stock_transfers_created_by FOREIGN KEY (created_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT fk_stock_transfers_approved_by FOREIGN KEY (approved_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT fk_stock_transfers_received_by FOREIGN KEY (received_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_stock_transfers_number CHECK (REGEXP_LIKE(transfer_number, '^STO/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    -- GST tax document: max 16 characters, compact format (D38).
    CONSTRAINT ck_stock_transfers_tax_invoice CHECK (tax_invoice_number IS NULL
        OR REGEXP_LIKE(tax_invoice_number, '^[A-Z]{2}[0-9]{4}-T[0-9]{6}$', 'c')),
    CONSTRAINT ck_stock_transfers_eway_bill CHECK (eway_bill_number IS NULL OR REGEXP_LIKE(eway_bill_number, '^[0-9]{12}$', 'c')),
    CONSTRAINT ck_stock_transfers_type CHECK (REGEXP_LIKE(transfer_type, '^(STORE_REPLENISHMENT|REBALANCING|RETURN_TO_DC)$', 'c')),
    CONSTRAINT ck_stock_transfers_distinct CHECK (source_warehouse_id <> destination_warehouse_id),
    CONSTRAINT ck_stock_transfers_status CHECK (REGEXP_LIKE(transfer_status, '^(DRAFT|APPROVED|IN_TRANSIT|RECEIVED|CANCELLED)$', 'c')),
    CONSTRAINT ck_stock_transfers_amounts CHECK (taxable_amount >= 0 AND igst_amount >= 0),
    -- Same-GSTIN transfers move under a delivery challan with no tax (D29).
    CONSTRAINT ck_stock_transfers_intra_gstin CHECK (is_inter_gstin OR (igst_amount = 0 AND tax_invoice_number IS NULL)),
    CONSTRAINT ck_stock_transfers_tax_invoice_by_dispatch CHECK (
        NOT is_inter_gstin OR transfer_status IN ('DRAFT', 'APPROVED', 'CANCELLED') OR tax_invoice_number IS NOT NULL),
    CONSTRAINT ck_stock_transfers_dispatched CHECK (
        transfer_status NOT IN ('IN_TRANSIT', 'RECEIVED') OR dispatched_at IS NOT NULL),
    CONSTRAINT ck_stock_transfers_received CHECK (
        (transfer_status = 'RECEIVED') = (received_at IS NOT NULL AND received_by_employee_id IS NOT NULL)),
    CONSTRAINT ck_stock_transfers_times CHECK (received_at IS NULL OR received_at >= dispatched_at),
    CONSTRAINT ck_stock_transfers_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Inter-warehouse transfer; between GSTINs it is also the IGST tax invoice (1C 5, D29, D38)';

CREATE TABLE stock_transfer_line_items (
    stock_transfer_line_item_id BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    stock_transfer_id           BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    product_id                  BIGINT UNSIGNED  NOT NULL,
    inventory_batch_id          BIGINT UNSIGNED  NULL,
    stock_status                VARCHAR(30)      NOT NULL,
    quantity_requested          DECIMAL(18,3)    NOT NULL,
    quantity_shipped            DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_received_good      DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_received_damaged   DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_lost               DECIMAL(18,3)    NOT NULL DEFAULT 0,
    unit_cost                   DECIMAL(18,4)    NULL,
    transfer_value              DECIMAL(18,2)    NOT NULL DEFAULT 0,
    gst_rate                    DECIMAL(7,4)     NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (stock_transfer_line_item_id),
    UNIQUE KEY uq_stock_transfer_line_items_transfer_line (stock_transfer_id, line_number),
    CONSTRAINT fk_stock_transfer_line_items_transfer FOREIGN KEY (stock_transfer_id) REFERENCES stock_transfers (stock_transfer_id),
    CONSTRAINT fk_stock_transfer_line_items_product FOREIGN KEY (product_id) REFERENCES products (product_id),
    CONSTRAINT fk_stock_transfer_line_items_batch FOREIGN KEY (inventory_batch_id) REFERENCES inventory_batches (inventory_batch_id),
    CONSTRAINT ck_stock_transfer_line_items_status CHECK (REGEXP_LIKE(stock_status, '^(SELLABLE|DEFECTIVE)$', 'c')),
    CONSTRAINT ck_stock_transfer_line_items_quantities CHECK (
        line_number > 0 AND quantity_requested > 0
        AND quantity_shipped >= 0 AND quantity_shipped <= quantity_requested
        AND quantity_received_good >= 0 AND quantity_received_damaged >= 0 AND quantity_lost >= 0
        AND quantity_received_good + quantity_received_damaged + quantity_lost <= quantity_shipped),
    CONSTRAINT ck_stock_transfer_line_items_cost CHECK (
        (quantity_shipped = 0 OR unit_cost IS NOT NULL) AND (unit_cost IS NULL OR unit_cost >= 0)),
    -- Transfer value = GST taxable value at moving-average cost; IGST on it (D29, D84). 0% for same-GSTIN.
    CONSTRAINT ck_stock_transfer_line_items_value CHECK (
        transfer_value = IFNULL(ROUND(quantity_shipped * unit_cost, 2), 0)
        AND gst_rate BETWEEN 0 AND 50
        AND igst_amount = ROUND(transfer_value * gst_rate / 100, 2)),
    CONSTRAINT ck_stock_transfer_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Products transferred with shipped / received good / damaged / lost quantities (1C 5)';
