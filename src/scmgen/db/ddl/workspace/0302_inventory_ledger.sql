-- Workspace schema, batch 3 (Part 1C inventory), file 2/2: the single inventory movement ledger.
-- Design: docs/design/phase1_part1c_procurement_and_inventory.md §5.0 (movement map) and §5.
-- Conventions: see 0101_reference.sql.
--
-- Append-only (D33): corrections are new rows with reversal_of_inventory_transaction_id set and the
-- opposite sign, so the sign rules below apply to original movements only.
--
-- Deferred foreign keys (target tables are created in later batches, added there by ALTER TABLE):
--   shipment_line_item_id      -> shipment_line_items      (sales/shipping batch)
--   invoice_line_item_id       -> invoice_line_items       (invoicing/returns batch)
--   sales_return_line_item_id  -> sales_return_line_items  (invoicing/returns batch)
--   return_inspection_id       -> return_inspections       (invoicing/returns batch)
--   defective_stock_id         -> defective_stock          (invoicing/returns batch)
--   generation_run_id          -> generation_runs          (system batch)

CREATE TABLE inventory_transactions (
    inventory_transaction_id    BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    transaction_at              DATETIME(6)      NOT NULL,
    warehouse_id                SMALLINT UNSIGNED NOT NULL,
    product_id                  BIGINT UNSIGNED  NOT NULL,
    inventory_batch_id          BIGINT UNSIGNED  NULL,
    warehouse_location_id       BIGINT UNSIGNED  NULL,
    transaction_type            VARCHAR(30)      NOT NULL,
    stock_status                VARCHAR(30)      NOT NULL,
    quantity                    DECIMAL(18,3)    NOT NULL,
    unit_cost                   DECIMAL(18,4)    NOT NULL,
    value_change                DECIMAL(18,2)    NOT NULL,
    goods_receipt_line_item_id  BIGINT UNSIGNED  NULL,
    shipment_line_item_id       BIGINT UNSIGNED  NULL,
    invoice_line_item_id        BIGINT UNSIGNED  NULL,
    sales_return_line_item_id   BIGINT UNSIGNED  NULL,
    return_inspection_id        BIGINT UNSIGNED  NULL,
    stock_transfer_line_item_id BIGINT UNSIGNED  NULL,
    stock_adjustment_line_item_id BIGINT UNSIGNED NULL,
    defective_stock_id          BIGINT UNSIGNED  NULL,
    reversal_of_inventory_transaction_id BIGINT UNSIGNED NULL,
    generation_run_id           BIGINT UNSIGNED  NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (inventory_transaction_id),
    UNIQUE KEY uq_inventory_transactions_reversal_of (reversal_of_inventory_transaction_id),
    KEY ix_inventory_transactions_stock_card (warehouse_id, product_id, transaction_at),
    KEY ix_inventory_transactions_type_time (transaction_type, transaction_at),
    KEY ix_inventory_transactions_product_time (product_id, transaction_at),
    KEY ix_inventory_transactions_created (created_at),
    KEY ix_inventory_transactions_shipment_line (shipment_line_item_id),
    KEY ix_inventory_transactions_invoice_line (invoice_line_item_id),
    KEY ix_inventory_transactions_return_line (sales_return_line_item_id),
    KEY ix_inventory_transactions_return_inspection (return_inspection_id),
    KEY ix_inventory_transactions_defective_stock (defective_stock_id),
    KEY ix_inventory_transactions_generation_run (generation_run_id),
    CONSTRAINT fk_inventory_transactions_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT fk_inventory_transactions_product FOREIGN KEY (product_id) REFERENCES products (product_id),
    CONSTRAINT fk_inventory_transactions_batch FOREIGN KEY (inventory_batch_id) REFERENCES inventory_batches (inventory_batch_id),
    CONSTRAINT fk_inventory_transactions_location FOREIGN KEY (warehouse_location_id)
        REFERENCES warehouse_locations (warehouse_location_id),
    CONSTRAINT fk_inventory_transactions_grn_line FOREIGN KEY (goods_receipt_line_item_id)
        REFERENCES goods_receipt_line_items (goods_receipt_line_item_id),
    CONSTRAINT fk_inventory_transactions_transfer_line FOREIGN KEY (stock_transfer_line_item_id)
        REFERENCES stock_transfer_line_items (stock_transfer_line_item_id),
    CONSTRAINT fk_inventory_transactions_adjustment_line FOREIGN KEY (stock_adjustment_line_item_id)
        REFERENCES stock_adjustment_line_items (stock_adjustment_line_item_id),
    CONSTRAINT fk_inventory_transactions_reversal_of FOREIGN KEY (reversal_of_inventory_transaction_id)
        REFERENCES inventory_transactions (inventory_transaction_id),
    CONSTRAINT ck_inventory_transactions_type CHECK (REGEXP_LIKE(transaction_type,
        '^(PURCHASE_RECEIPT|SALES_ISSUE|RETURN_RECEIPT|RETURN_DISPOSITION|TRANSFER_OUT|TRANSFER_IN|ADJUSTMENT|DEFECTIVE_DISPOSITION|RTO_RECEIPT)$', 'c')),
    CONSTRAINT ck_inventory_transactions_status CHECK (REGEXP_LIKE(stock_status, '^(SELLABLE|QUARANTINE|DEFECTIVE)$', 'c')),
    -- Quantity never zero; value moves in the same direction as quantity (D27).
    CONSTRAINT ck_inventory_transactions_values CHECK (quantity <> 0 AND unit_cost >= 0 AND quantity * value_change >= 0),
    -- Exactly one source document (D28).
    CONSTRAINT ck_inventory_transactions_one_source CHECK (
        (goods_receipt_line_item_id IS NOT NULL) + (shipment_line_item_id IS NOT NULL) + (invoice_line_item_id IS NOT NULL)
        + (sales_return_line_item_id IS NOT NULL) + (return_inspection_id IS NOT NULL)
        + (stock_transfer_line_item_id IS NOT NULL) + (stock_adjustment_line_item_id IS NOT NULL)
        + (defective_stock_id IS NOT NULL) = 1),
    -- Movement map, 1C §5.0: each type has its source document and stock-status bucket ...
    CONSTRAINT ck_inventory_transactions_type_source CHECK (
        (transaction_type = 'PURCHASE_RECEIPT'      AND goods_receipt_line_item_id IS NOT NULL    AND stock_status = 'SELLABLE')
     OR (transaction_type = 'SALES_ISSUE'           AND (shipment_line_item_id IS NOT NULL OR invoice_line_item_id IS NOT NULL)
                                                    AND stock_status = 'SELLABLE')
     OR (transaction_type = 'RETURN_RECEIPT'        AND sales_return_line_item_id IS NOT NULL     AND stock_status = 'QUARANTINE')
     OR (transaction_type = 'RETURN_DISPOSITION'    AND return_inspection_id IS NOT NULL)
     OR (transaction_type IN ('TRANSFER_OUT', 'TRANSFER_IN') AND stock_transfer_line_item_id IS NOT NULL
                                                    AND stock_status IN ('SELLABLE', 'DEFECTIVE'))
     OR (transaction_type = 'ADJUSTMENT'            AND stock_adjustment_line_item_id IS NOT NULL)
     OR (transaction_type = 'DEFECTIVE_DISPOSITION' AND defective_stock_id IS NOT NULL            AND stock_status IN ('DEFECTIVE', 'SELLABLE'))
     OR (transaction_type = 'RTO_RECEIPT'           AND shipment_line_item_id IS NOT NULL         AND stock_status = 'SELLABLE')),
    -- ... and its direction. Reversal rows (D33) carry the opposite sign and are exempt.
    CONSTRAINT ck_inventory_transactions_type_sign CHECK (
        reversal_of_inventory_transaction_id IS NOT NULL
     OR (transaction_type IN ('PURCHASE_RECEIPT', 'RETURN_RECEIPT', 'TRANSFER_IN', 'RTO_RECEIPT') AND quantity > 0)
     OR (transaction_type IN ('SALES_ISSUE', 'TRANSFER_OUT') AND quantity < 0)
     OR (transaction_type = 'RETURN_DISPOSITION'
         AND ((quantity < 0 AND stock_status = 'QUARANTINE') OR (quantity > 0 AND stock_status IN ('SELLABLE', 'DEFECTIVE'))))
     OR (transaction_type = 'DEFECTIVE_DISPOSITION'
         AND ((quantity < 0 AND stock_status = 'DEFECTIVE') OR (quantity > 0 AND stock_status = 'SELLABLE')))
     OR transaction_type = 'ADJUSTMENT'),
    CONSTRAINT ck_inventory_transactions_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'The only inventory movement ledger; append-only, exactly one source document per row (1C 5, D28, D33)';
