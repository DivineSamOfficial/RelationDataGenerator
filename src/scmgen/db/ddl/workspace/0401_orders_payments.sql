-- Workspace schema, batch 4 (Part 1D sales & shipping), file 1/2: orders, order lines, order status
-- history and customer payments.
-- Design: docs/design/phase1_part1d_sales_shipping_invoicing_returns.md §3 (+ D79, D80 from Part 1H).
-- Conventions: see 0101_reference.sql; amount rules: see 0201_rfq_quotations.sql (D84).
--
-- Order lines carry the TOTAL GST only (D39); the CGST/SGST vs IGST split is decided per invoice.
-- B2C lines are priced GST-inclusive (D79): taxable value is back-calculated from inclusive_unit_price.
--
-- Deferred foreign key (target created in a later batch, added there by ALTER TABLE):
--   customer_payments.settlement_bank_transaction_id -> bank_transactions   (Part 1E batch)

CREATE TABLE orders (
    order_id                    BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    order_number                VARCHAR(30)      NOT NULL,
    customer_id                 BIGINT UNSIGNED  NOT NULL,
    order_placed_at             DATETIME(6)      NOT NULL,
    order_date                  DATE             NOT NULL,
    sales_channel_id            SMALLINT UNSIGNED NOT NULL,
    marketing_channel_id        SMALLINT UNSIGNED NOT NULL,
    marketing_campaign_id       BIGINT UNSIGNED  NULL,
    fulfilment_type             VARCHAR(20)      NOT NULL,
    store_warehouse_id          SMALLINT UNSIGNED NULL,
    billing_address_id          BIGINT UNSIGNED  NOT NULL,
    shipping_address_id         BIGINT UNSIGNED  NULL,
    payment_term_id             SMALLINT UNSIGNED NOT NULL,
    sales_employee_id           BIGINT UNSIGNED  NULL,
    customer_po_reference       VARCHAR(40)      NULL,
    requested_delivery_date     DATE             NULL,
    prices_include_tax          BOOLEAN          NOT NULL,
    items_taxable_amount        DECIMAL(18,2)    NOT NULL,
    items_tax_amount            DECIMAL(18,2)    NOT NULL,
    shipping_charge_amount      DECIMAL(18,2)    NOT NULL DEFAULT 0,
    order_total_estimate        DECIMAL(18,2)    NOT NULL,
    order_status                VARCHAR(30)      NOT NULL,
    hold_reason                 VARCHAR(30)      NULL,
    cancelled_at                DATETIME(6)      NULL,
    cancellation_reason         VARCHAR(30)      NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (order_id),
    UNIQUE KEY uq_orders_number (order_number),
    -- Target of composite FKs from invoices and customer_payments (D80).
    UNIQUE KEY uq_orders_id_customer (order_id, customer_id),
    KEY ix_orders_customer_date (customer_id, order_date),
    KEY ix_orders_status_date (order_status, order_date),
    KEY ix_orders_date (order_date),
    KEY ix_orders_channel_date (sales_channel_id, order_date),
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    CONSTRAINT fk_orders_sales_channel FOREIGN KEY (sales_channel_id) REFERENCES sales_channels (sales_channel_id),
    CONSTRAINT fk_orders_marketing_channel FOREIGN KEY (marketing_channel_id) REFERENCES marketing_channels (marketing_channel_id),
    CONSTRAINT fk_orders_campaign FOREIGN KEY (marketing_campaign_id) REFERENCES marketing_campaigns (marketing_campaign_id),
    CONSTRAINT fk_orders_store FOREIGN KEY (store_warehouse_id) REFERENCES warehouses (warehouse_id),
    -- The order's addresses always belong to the order's customer (D80; was validation V-SO-02).
    CONSTRAINT fk_orders_billing_address FOREIGN KEY (billing_address_id, customer_id)
        REFERENCES customer_addresses (customer_address_id, customer_id),
    CONSTRAINT fk_orders_shipping_address FOREIGN KEY (shipping_address_id, customer_id)
        REFERENCES customer_addresses (customer_address_id, customer_id),
    CONSTRAINT fk_orders_payment_term FOREIGN KEY (payment_term_id) REFERENCES payment_terms (payment_term_id),
    CONSTRAINT fk_orders_sales_employee FOREIGN KEY (sales_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_orders_number CHECK (REGEXP_LIKE(order_number, '^SO/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_orders_fulfilment CHECK (REGEXP_LIKE(fulfilment_type, '^(SHIP|STORE_PICKUP|COUNTER_SALE)$', 'c')),
    CONSTRAINT ck_orders_ship_address CHECK ((fulfilment_type = 'SHIP') = (shipping_address_id IS NOT NULL)),
    CONSTRAINT ck_orders_store CHECK ((fulfilment_type = 'SHIP') = (store_warehouse_id IS NULL)),
    CONSTRAINT ck_orders_counter_no_shipping CHECK (fulfilment_type <> 'COUNTER_SALE' OR shipping_charge_amount = 0),
    CONSTRAINT ck_orders_date CHECK (order_date = DATE(order_placed_at)),
    CONSTRAINT ck_orders_amounts CHECK (
        items_taxable_amount >= 0 AND items_tax_amount >= 0 AND shipping_charge_amount >= 0
        -- The estimate is rounded to the nearest rupee (what a prepaid customer pays), so it may fall short of
        -- the exact amount by the same round-off an invoice allows (step 3.9a).
        AND order_total_estimate >= items_taxable_amount + items_tax_amount + shipping_charge_amount - 0.50),
    CONSTRAINT ck_orders_status CHECK (REGEXP_LIKE(order_status,
        '^(PENDING_PAYMENT|PENDING_CREDIT_CHECK|ON_HOLD|CONFIRMED|BACKORDERED|PARTIALLY_SHIPPED|SHIPPED|PARTIALLY_DELIVERED|DELIVERED|CLOSED|CANCELLED)$', 'c')),
    CONSTRAINT ck_orders_hold CHECK (
        (order_status = 'ON_HOLD') = (hold_reason IS NOT NULL)
        AND (hold_reason IS NULL OR REGEXP_LIKE(hold_reason, '^(CREDIT_LIMIT|OVERDUE_BALANCE)$', 'c'))),
    CONSTRAINT ck_orders_cancelled CHECK (
        (order_status = 'CANCELLED') = (cancelled_at IS NOT NULL)
        AND (cancelled_at IS NULL) = (cancellation_reason IS NULL)
        AND (cancellation_reason IS NULL OR REGEXP_LIKE(cancellation_reason,
            '^(PAYMENT_FAILED|CUSTOMER_REQUEST|OUT_OF_STOCK|CREDIT_HOLD_EXPIRED|DUPLICATE_ORDER)$', 'c'))),
    CONSTRAINT ck_orders_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Customer order with channel attribution; a commercial document, not a tax document (1D 3, D10, D39)';

CREATE TABLE order_line_items (
    order_line_item_id          BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    order_id                    BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    product_id                  BIGINT UNSIGNED  NOT NULL,
    quantity_ordered            DECIMAL(18,3)    NOT NULL,
    list_price                  DECIMAL(18,4)    NOT NULL,
    unit_price                  DECIMAL(18,4)    NOT NULL,
    inclusive_unit_price        DECIMAL(18,4)    NULL,
    discount_percent            DECIMAL(7,4)     NOT NULL DEFAULT 0,
    discount_type               VARCHAR(30)      NULL,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    gst_rate                    DECIMAL(7,4)     NOT NULL,
    tax_amount                  DECIMAL(18,2)    NOT NULL,
    line_total                  DECIMAL(18,2)    NOT NULL,
    quantity_shipped            DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_delivered          DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_invoiced           DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_cancelled          DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_rto                DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_return_requested   DECIMAL(18,3)    NOT NULL DEFAULT 0,
    quantity_returnable         DECIMAL(18,3)    GENERATED ALWAYS AS (quantity_delivered - quantity_return_requested) STORED,
    line_status                 VARCHAR(30)      NOT NULL DEFAULT 'OPEN',
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (order_line_item_id),
    UNIQUE KEY uq_order_line_items_order_line (order_id, line_number),
    UNIQUE KEY uq_order_line_items_order_product (order_id, product_id),
    -- Target of the composite FK from invoice_line_items (product always = order line's product, D80).
    UNIQUE KEY uq_order_line_items_id_product (order_line_item_id, product_id),
    KEY ix_order_line_items_status (line_status),
    CONSTRAINT fk_order_line_items_order FOREIGN KEY (order_id) REFERENCES orders (order_id),
    CONSTRAINT fk_order_line_items_product FOREIGN KEY (product_id) REFERENCES products (product_id),
    CONSTRAINT ck_order_line_items_prices CHECK (
        line_number > 0 AND quantity_ordered > 0 AND list_price > 0 AND unit_price > 0
        AND (inclusive_unit_price IS NULL OR inclusive_unit_price > 0)
        AND gst_rate BETWEEN 0 AND 50),
    CONSTRAINT ck_order_line_items_discount CHECK (
        discount_percent BETWEEN 0 AND 90 AND (discount_percent > 0) = (discount_type IS NOT NULL)
        AND (discount_type IS NULL OR REGEXP_LIKE(discount_type, '^(VOLUME|SEGMENT|CAMPAIGN_PROMO|CLEARANCE)$', 'c'))),
    -- Taxable value (D84): from the ex-GST price (B2B) or back-calculated from the GST-inclusive price (B2C, D79).
    CONSTRAINT ck_order_line_items_taxable CHECK (
        (inclusive_unit_price IS NULL
            AND taxable_amount = ROUND(quantity_ordered * unit_price * (1 - discount_percent / 100), 2))
     OR (inclusive_unit_price IS NOT NULL
            AND unit_price = ROUND(inclusive_unit_price / (1 + gst_rate / 100), 4)
            AND taxable_amount = ROUND(quantity_ordered * inclusive_unit_price * (1 - discount_percent / 100)
                                       / (1 + gst_rate / 100), 2))),
    -- Total GST only; the head split happens on the invoice (D39).
    CONSTRAINT ck_order_line_items_tax CHECK (
        tax_amount = ROUND(taxable_amount * gst_rate / 100, 2) AND line_total = taxable_amount + tax_amount),
    -- Counter rules (1D): returns never exceed delivered quantity; invoicing never exceeds dispatch.
    CONSTRAINT ck_order_line_items_counters CHECK (
        quantity_shipped >= 0 AND quantity_delivered >= 0 AND quantity_invoiced >= 0 AND quantity_cancelled >= 0
        AND quantity_rto >= 0 AND quantity_return_requested >= 0
        AND quantity_shipped + quantity_cancelled <= quantity_ordered
        AND quantity_delivered + quantity_rto <= quantity_shipped
        AND quantity_invoiced <= quantity_shipped
        AND quantity_return_requested <= quantity_delivered),
    CONSTRAINT ck_order_line_items_status CHECK (REGEXP_LIKE(line_status,
        '^(OPEN|BACKORDERED|PARTIALLY_SHIPPED|SHIPPED|DELIVERED|CANCELLED|CLOSED)$', 'c')),
    CONSTRAINT ck_order_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Ordered products with snapshot prices, total GST and fulfilment counters (1D 3, D37, D79)';

CREATE TABLE order_status_history (
    order_status_history_id     BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    order_id                    BIGINT UNSIGNED  NOT NULL,
    from_status                 VARCHAR(30)      NULL,
    to_status                   VARCHAR(30)      NOT NULL,
    changed_at                  DATETIME(6)      NOT NULL,
    changed_by_employee_id      BIGINT UNSIGNED  NULL,
    change_reason               VARCHAR(255)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (order_status_history_id),
    KEY ix_order_status_history_order_changed (order_id, changed_at),
    CONSTRAINT fk_order_status_history_order FOREIGN KEY (order_id) REFERENCES orders (order_id),
    CONSTRAINT fk_order_status_history_changed_by FOREIGN KEY (changed_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_order_status_history_to CHECK (REGEXP_LIKE(to_status,
        '^(PENDING_PAYMENT|PENDING_CREDIT_CHECK|ON_HOLD|CONFIRMED|BACKORDERED|PARTIALLY_SHIPPED|SHIPPED|PARTIALLY_DELIVERED|DELIVERED|CLOSED|CANCELLED)$', 'c')),
    CONSTRAINT ck_order_status_history_from CHECK (from_status IS NULL OR (from_status <> to_status AND REGEXP_LIKE(from_status,
        '^(PENDING_PAYMENT|PENDING_CREDIT_CHECK|ON_HOLD|CONFIRMED|BACKORDERED|PARTIALLY_SHIPPED|SHIPPED|PARTIALLY_DELIVERED|DELIVERED|CLOSED|CANCELLED)$', 'c'))),
    CONSTRAINT ck_order_status_history_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Order lifecycle, append-only (1D 3)';

CREATE TABLE customer_payments (
    customer_payment_id         BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    payment_number              VARCHAR(30)      NOT NULL,
    customer_id                 BIGINT UNSIGNED  NOT NULL,
    order_id                    BIGINT UNSIGNED  NULL,
    payment_type                VARCHAR(30)      NOT NULL,
    attempt_number              TINYINT UNSIGNED NOT NULL DEFAULT 1,
    initiated_at                DATETIME(6)      NOT NULL,
    payment_date                DATE             NOT NULL,
    payment_method_id           SMALLINT UNSIGNED NOT NULL,
    bank_account_id             SMALLINT UNSIGNED NOT NULL,
    amount                      DECIMAL(18,2)    NOT NULL,
    gateway_fee_amount          DECIMAL(18,2)    NOT NULL DEFAULT 0,
    collected_by_shipping_carrier_id SMALLINT UNSIGNED NULL,
    gateway_reference           VARCHAR(40)      NULL,
    payment_status              VARCHAR(30)      NOT NULL DEFAULT 'INITIATED',
    failure_reason              VARCHAR(40)      NULL,
    completed_at                DATETIME(6)      NULL,
    settlement_date             DATE             NULL,
    settlement_bank_transaction_id BIGINT UNSIGNED NULL,
    reversed_at                 DATETIME(6)      NULL,
    reversal_reason             VARCHAR(40)      NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (customer_payment_id),
    UNIQUE KEY uq_customer_payments_number (payment_number),
    UNIQUE KEY uq_customer_payments_order_attempt (order_id, attempt_number),
    -- Target of the composite FK from the AR/AP open-item subledger (same party, Part 1E batch).
    UNIQUE KEY uq_customer_payments_id_customer (customer_payment_id, customer_id),
    KEY ix_customer_payments_customer_date (customer_id, payment_date),
    KEY ix_customer_payments_status_date (payment_status, payment_date),
    KEY ix_customer_payments_settlement_date (settlement_date),
    KEY ix_customer_payments_settlement_txn (settlement_bank_transaction_id),
    -- The paying customer is always the order's customer; not checked when order_id is NULL (D80).
    CONSTRAINT fk_customer_payments_order_customer FOREIGN KEY (order_id, customer_id) REFERENCES orders (order_id, customer_id),
    CONSTRAINT fk_customer_payments_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    CONSTRAINT fk_customer_payments_method FOREIGN KEY (payment_method_id) REFERENCES payment_methods (payment_method_id),
    CONSTRAINT fk_customer_payments_bank_account FOREIGN KEY (bank_account_id) REFERENCES bank_accounts (bank_account_id),
    CONSTRAINT fk_customer_payments_carrier FOREIGN KEY (collected_by_shipping_carrier_id)
        REFERENCES shipping_carriers (shipping_carrier_id),
    CONSTRAINT ck_customer_payments_number CHECK (REGEXP_LIKE(payment_number, '^RCPT/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_customer_payments_type CHECK (REGEXP_LIKE(payment_type,
        '^(ORDER_PREPAYMENT|COUNTER_PAYMENT|COD_COLLECTION|INVOICE_PAYMENT|ADVANCE)$', 'c')),
    CONSTRAINT ck_customer_payments_order_required CHECK (payment_type IN ('INVOICE_PAYMENT', 'ADVANCE') OR order_id IS NOT NULL),
    CONSTRAINT ck_customer_payments_cod CHECK ((payment_type = 'COD_COLLECTION') = (collected_by_shipping_carrier_id IS NOT NULL)),
    CONSTRAINT ck_customer_payments_amounts CHECK (
        amount > 0 AND attempt_number >= 1 AND gateway_fee_amount >= 0 AND gateway_fee_amount < amount),
    CONSTRAINT ck_customer_payments_status CHECK (REGEXP_LIKE(payment_status, '^(INITIATED|SUCCESS|FAILED|REVERSED)$', 'c')),
    CONSTRAINT ck_customer_payments_failure CHECK (
        (payment_status = 'FAILED') = (failure_reason IS NOT NULL)
        AND (failure_reason IS NULL OR REGEXP_LIKE(failure_reason,
            '^(INSUFFICIENT_FUNDS|BANK_DECLINED|TIMEOUT|AUTH_FAILED|CHEQUE_BOUNCED)$', 'c'))),
    CONSTRAINT ck_customer_payments_completed CHECK (payment_status = 'INITIATED' OR completed_at IS NOT NULL),
    CONSTRAINT ck_customer_payments_settled CHECK (
        (payment_status <> 'SUCCESS' OR settlement_date IS NOT NULL)
        AND (settlement_date IS NULL OR settlement_date >= payment_date)),
    CONSTRAINT ck_customer_payments_reversed CHECK (
        (payment_status = 'REVERSED') = (reversed_at IS NOT NULL AND reversal_reason IS NOT NULL)),
    CONSTRAINT ck_customer_payments_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Every customer payment attempt and receipt; only SUCCESS is allocated (1D 3, D5, D43, D53)';

-- Close the deferred FK from batch 3: reservations hold stock for an order line.
ALTER TABLE inventory_reservations
    ADD CONSTRAINT fk_inventory_reservations_order_line FOREIGN KEY (order_line_item_id)
        REFERENCES order_line_items (order_line_item_id);
