-- Workspace schema, batch 4 (Part 1D sales & shipping), file 2/2: shipments, carrier tracking and
-- delivery attempts. Design: docs/design/phase1_part1d_sales_shipping_invoicing_returns.md §4.
-- Conventions: see 0101_reference.sql.

CREATE TABLE shipments (
    shipment_id                 BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    shipment_number             VARCHAR(30)      NOT NULL,
    order_id                    BIGINT UNSIGNED  NOT NULL,
    warehouse_id                SMALLINT UNSIGNED NOT NULL,
    shipping_address_id         BIGINT UNSIGNED  NOT NULL,
    shipping_carrier_id         SMALLINT UNSIGNED NOT NULL,
    tracking_number             VARCHAR(30)      NOT NULL,
    is_cod                      BOOLEAN          NOT NULL DEFAULT FALSE,
    cod_amount                  DECIMAL(18,2)    NULL,
    package_count               SMALLINT UNSIGNED NOT NULL,
    total_weight_kg             DECIMAL(10,3)    NOT NULL,
    freight_cost_amount         DECIMAL(18,2)    NOT NULL,
    eway_bill_number            CHAR(12)         NULL,
    planned_dispatch_date       DATE             NOT NULL,
    dispatched_at               DATETIME(6)      NULL,
    expected_delivery_date      DATE             NULL,
    delivered_at                DATETIME(6)      NULL,
    rto_received_at             DATETIME(6)      NULL,
    shipment_status             VARCHAR(30)      NOT NULL DEFAULT 'PENDING',
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (shipment_id),
    UNIQUE KEY uq_shipments_number (shipment_number),
    UNIQUE KEY uq_shipments_tracking_number (tracking_number),
    -- Target of the composite FK from invoices: the billed shipment belongs to the invoice's order and warehouse.
    UNIQUE KEY uq_shipments_id_order_warehouse (shipment_id, order_id, warehouse_id),
    KEY ix_shipments_status_planned (shipment_status, planned_dispatch_date),
    KEY ix_shipments_carrier_dispatched (shipping_carrier_id, dispatched_at),
    KEY ix_shipments_dispatched (dispatched_at),
    CONSTRAINT fk_shipments_order FOREIGN KEY (order_id) REFERENCES orders (order_id),
    CONSTRAINT fk_shipments_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT fk_shipments_address FOREIGN KEY (shipping_address_id) REFERENCES customer_addresses (customer_address_id),
    CONSTRAINT fk_shipments_carrier FOREIGN KEY (shipping_carrier_id) REFERENCES shipping_carriers (shipping_carrier_id),
    CONSTRAINT ck_shipments_number CHECK (REGEXP_LIKE(shipment_number, '^SHP/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_shipments_eway_bill CHECK (eway_bill_number IS NULL OR REGEXP_LIKE(eway_bill_number, '^[0-9]{12}$', 'c')),
    CONSTRAINT ck_shipments_cod CHECK (is_cod = (cod_amount IS NOT NULL) AND (cod_amount IS NULL OR cod_amount > 0)),
    CONSTRAINT ck_shipments_physical CHECK (package_count > 0 AND total_weight_kg > 0 AND freight_cost_amount >= 0),
    -- Shipment-level statuses from the master prompt; 'Order Confirmed' is an order status (R11).
    CONSTRAINT ck_shipments_status CHECK (REGEXP_LIKE(shipment_status,
        '^(PENDING|PICKING|PICKED|PACKING|PACKED|READY_FOR_PICKUP|PICKED_UP|IN_TRANSIT|OUT_FOR_DELIVERY|DELIVERED|DELIVERY_FAILED|RETURNED_TO_SENDER|CANCELLED)$', 'c')),
    CONSTRAINT ck_shipments_dispatched CHECK (
        shipment_status IN ('PENDING', 'PICKING', 'PICKED', 'PACKING', 'PACKED', 'READY_FOR_PICKUP', 'CANCELLED')
        OR dispatched_at IS NOT NULL),
    CONSTRAINT ck_shipments_delivered CHECK ((shipment_status = 'DELIVERED') = (delivered_at IS NOT NULL)),
    CONSTRAINT ck_shipments_rto CHECK ((shipment_status = 'RETURNED_TO_SENDER') = (rto_received_at IS NOT NULL)),
    CONSTRAINT ck_shipments_times CHECK (
        (delivered_at IS NULL OR delivered_at > dispatched_at) AND (rto_received_at IS NULL OR rto_received_at > dispatched_at)),
    CONSTRAINT ck_shipments_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'One dispatch from one warehouse for one order; split shipments = several rows (1D 4)';

CREATE TABLE shipment_line_items (
    shipment_line_item_id       BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    shipment_id                 BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    order_line_item_id          BIGINT UNSIGNED  NOT NULL,
    inventory_batch_id          BIGINT UNSIGNED  NULL,
    quantity_shipped            DECIMAL(18,3)    NOT NULL,
    picked_from_location_id     BIGINT UNSIGNED  NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (shipment_line_item_id),
    UNIQUE KEY uq_shipment_line_items_shipment_line (shipment_id, line_number),
    UNIQUE KEY uq_shipment_line_items_shipment_order_line_batch (shipment_id, order_line_item_id, inventory_batch_id),
    CONSTRAINT fk_shipment_line_items_shipment FOREIGN KEY (shipment_id) REFERENCES shipments (shipment_id),
    CONSTRAINT fk_shipment_line_items_order_line FOREIGN KEY (order_line_item_id) REFERENCES order_line_items (order_line_item_id),
    CONSTRAINT fk_shipment_line_items_batch FOREIGN KEY (inventory_batch_id) REFERENCES inventory_batches (inventory_batch_id),
    CONSTRAINT fk_shipment_line_items_location FOREIGN KEY (picked_from_location_id)
        REFERENCES warehouse_locations (warehouse_location_id),
    CONSTRAINT ck_shipment_line_items_values CHECK (line_number > 0 AND quantity_shipped > 0),
    CONSTRAINT ck_shipment_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Order-line quantity in a shipment, one row per batch (1D 4, D48)';

CREATE TABLE shipment_status_history (
    shipment_status_history_id  BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    shipment_id                 BIGINT UNSIGNED  NOT NULL,
    from_status                 VARCHAR(30)      NULL,
    to_status                   VARCHAR(30)      NOT NULL,
    changed_at                  DATETIME(6)      NOT NULL,
    changed_by_employee_id      BIGINT UNSIGNED  NULL,
    change_reason               VARCHAR(255)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (shipment_status_history_id),
    KEY ix_shipment_status_history_shipment_changed (shipment_id, changed_at),
    CONSTRAINT fk_shipment_status_history_shipment FOREIGN KEY (shipment_id) REFERENCES shipments (shipment_id),
    CONSTRAINT fk_shipment_status_history_changed_by FOREIGN KEY (changed_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_shipment_status_history_to CHECK (REGEXP_LIKE(to_status,
        '^(PENDING|PICKING|PICKED|PACKING|PACKED|READY_FOR_PICKUP|PICKED_UP|IN_TRANSIT|OUT_FOR_DELIVERY|DELIVERED|DELIVERY_FAILED|RETURNED_TO_SENDER|CANCELLED)$', 'c')),
    CONSTRAINT ck_shipment_status_history_from CHECK (from_status IS NULL OR (from_status <> to_status AND REGEXP_LIKE(from_status,
        '^(PENDING|PICKING|PICKED|PACKING|PACKED|READY_FOR_PICKUP|PICKED_UP|IN_TRANSIT|OUT_FOR_DELIVERY|DELIVERED|DELIVERY_FAILED|RETURNED_TO_SENDER|CANCELLED)$', 'c'))),
    CONSTRAINT ck_shipment_status_history_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Shipment lifecycle as recorded by our system, append-only (1D 4, R12)';

CREATE TABLE shipment_tracking (
    shipment_tracking_id        BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    shipment_id                 BIGINT UNSIGNED  NOT NULL,
    carrier_event_id            VARCHAR(40)      NOT NULL,
    event_sequence              SMALLINT UNSIGNED NOT NULL,
    event_code                  VARCHAR(30)      NOT NULL,
    event_description           VARCHAR(150)     NOT NULL,
    event_at                    DATETIME(6)      NOT NULL,
    received_at                 DATETIME(6)      NOT NULL,
    location_city               VARCHAR(80)      NULL,
    state_id                    SMALLINT UNSIGNED NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (shipment_tracking_id),
    UNIQUE KEY uq_shipment_tracking_carrier_event (carrier_event_id),
    UNIQUE KEY uq_shipment_tracking_shipment_sequence (shipment_id, event_sequence),
    KEY ix_shipment_tracking_shipment_event (shipment_id, event_at),
    KEY ix_shipment_tracking_received (received_at),
    CONSTRAINT fk_shipment_tracking_shipment FOREIGN KEY (shipment_id) REFERENCES shipments (shipment_id),
    CONSTRAINT fk_shipment_tracking_state FOREIGN KEY (state_id) REFERENCES states (state_id),
    CONSTRAINT ck_shipment_tracking_event_code CHECK (REGEXP_LIKE(event_code,
        '^(PICKED_UP|ARRIVED_AT_HUB|DEPARTED_HUB|IN_TRANSIT|OUT_FOR_DELIVERY|DELIVERY_ATTEMPTED|DELIVERED|RTO_INITIATED|RTO_IN_TRANSIT|RTO_DELIVERED)$', 'c')),
    CONSTRAINT ck_shipment_tracking_sequence CHECK (event_sequence > 0),
    CONSTRAINT ck_shipment_tracking_feed_lag CHECK (received_at >= event_at),
    -- The row is recorded when the feed event reaches us (Part 1H F5; loader watermark correctness, D67).
    CONSTRAINT ck_shipment_tracking_recorded_on_receipt CHECK (created_at = received_at),
    CONSTRAINT ck_shipment_tracking_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Carrier scan events from the external feed; insert-only (1D 4, R12)';

CREATE TABLE delivery_attempts (
    delivery_attempt_id         BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    shipment_id                 BIGINT UNSIGNED  NOT NULL,
    attempt_number              TINYINT UNSIGNED NOT NULL,
    attempted_at                DATETIME(6)      NOT NULL,
    attempt_outcome             VARCHAR(20)      NOT NULL,
    failure_reason              VARCHAR(30)      NULL,
    received_by_name            VARCHAR(120)     NULL,
    proof_of_delivery_reference VARCHAR(40)      NULL,
    cod_collected_amount        DECIMAL(18,2)    NULL,
    delivered_key               BIGINT UNSIGNED  GENERATED ALWAYS AS
        (IF(attempt_outcome = 'DELIVERED', shipment_id, NULL)) STORED,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (delivery_attempt_id),
    UNIQUE KEY uq_delivery_attempts_shipment_attempt (shipment_id, attempt_number),
    -- At most one successful delivery per shipment (was validation V-SH-04).
    UNIQUE KEY uq_delivery_attempts_delivered (delivered_key),
    CONSTRAINT fk_delivery_attempts_shipment FOREIGN KEY (shipment_id) REFERENCES shipments (shipment_id),
    CONSTRAINT ck_delivery_attempts_number CHECK (attempt_number BETWEEN 1 AND 3),
    CONSTRAINT ck_delivery_attempts_outcome CHECK (REGEXP_LIKE(attempt_outcome, '^(DELIVERED|FAILED)$', 'c')),
    CONSTRAINT ck_delivery_attempts_failure CHECK (
        (attempt_outcome = 'FAILED') = (failure_reason IS NOT NULL)
        AND (failure_reason IS NULL OR REGEXP_LIKE(failure_reason,
            '^(CUSTOMER_UNAVAILABLE|ADDRESS_NOT_FOUND|REFUSED|COD_CASH_NOT_READY|PREMISES_CLOSED|WEATHER_DELAY)$', 'c'))),
    CONSTRAINT ck_delivery_attempts_receiver CHECK (attempt_outcome = 'FAILED' OR received_by_name IS NOT NULL),
    CONSTRAINT ck_delivery_attempts_cod CHECK (
        cod_collected_amount IS NULL OR (attempt_outcome = 'DELIVERED' AND cod_collected_amount > 0)),
    CONSTRAINT ck_delivery_attempts_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Each delivery attempt with outcome and failure reason; max 3 then RTO (1D 4)';

-- Close a deferred FK from batch 3: dispatch and RTO movements point at the shipment line.
ALTER TABLE inventory_transactions
    ADD CONSTRAINT fk_inventory_transactions_shipment_line FOREIGN KEY (shipment_line_item_id)
        REFERENCES shipment_line_items (shipment_line_item_id);
