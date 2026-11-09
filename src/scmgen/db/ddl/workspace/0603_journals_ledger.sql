-- Workspace schema, batch 6 (Part 1E finance), file 3/5: journal entries, journal lines (the accounting
-- source of truth) and derived period balances.
-- Design: docs/design/phase1_part1e_expenses_accounting_ar_ap_tax.md §3, §6. Conventions: see 0101_reference.sql.
--
-- Deferred foreign key (target created in the system batch, added there by ALTER TABLE):
--   journal_entries.generation_run_id -> generation_runs

CREATE TABLE journal_entries (
    journal_entry_id            BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    journal_number              VARCHAR(30)      NOT NULL,
    journal_source              VARCHAR(40)      NOT NULL,
    journal_type                VARCHAR(20)      NOT NULL,
    posting_date                DATE             NOT NULL,
    document_date               DATE             NOT NULL,
    accounting_period_id        SMALLINT UNSIGNED NOT NULL,
    description                 VARCHAR(255)     NOT NULL,
    total_debit                 DECIMAL(18,2)    NOT NULL,
    total_credit                DECIMAL(18,2)    NOT NULL,
    entry_status                VARCHAR(20)      NOT NULL DEFAULT 'POSTED',
    reverses_journal_entry_id   BIGINT UNSIGNED  NULL,
    goods_receipt_id            BIGINT UNSIGNED  NULL,
    supplier_invoice_id         BIGINT UNSIGNED  NULL,
    supplier_payment_id         BIGINT UNSIGNED  NULL,
    invoice_id                  BIGINT UNSIGNED  NULL,
    credit_note_id              BIGINT UNSIGNED  NULL,
    customer_payment_id         BIGINT UNSIGNED  NULL,
    refund_id                   BIGINT UNSIGNED  NULL,
    sales_return_id             BIGINT UNSIGNED  NULL,
    shipment_id                 BIGINT UNSIGNED  NULL,
    stock_adjustment_id         BIGINT UNSIGNED  NULL,
    stock_transfer_id           BIGINT UNSIGNED  NULL,
    defective_stock_id          BIGINT UNSIGNED  NULL,
    expense_id                  BIGINT UNSIGNED  NULL,
    expense_payment_id          BIGINT UNSIGNED  NULL,
    bank_transaction_id         BIGINT UNSIGNED  NULL,
    gst_return_id               BIGINT UNSIGNED  NULL,
    -- One journal per (event type, source document): a second posting of the same event is rejected (D50).
    -- NULL for OPENING / RECURRING / MANUAL entries, which have no source document.
    posting_key                 VARCHAR(80)      GENERATED ALWAYS AS (CONCAT(journal_source, ':', COALESCE(
        goods_receipt_id, supplier_invoice_id, supplier_payment_id, invoice_id, credit_note_id, customer_payment_id,
        refund_id, sales_return_id, shipment_id, stock_adjustment_id, stock_transfer_id, defective_stock_id,
        expense_id, expense_payment_id, bank_transaction_id, gst_return_id, reverses_journal_entry_id))) STORED,
    created_by_employee_id      BIGINT UNSIGNED  NULL,
    generation_run_id           BIGINT UNSIGNED  NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (journal_entry_id),
    UNIQUE KEY uq_journal_entries_number (journal_number),
    UNIQUE KEY uq_journal_entries_posting_key (posting_key),
    UNIQUE KEY uq_journal_entries_reverses (reverses_journal_entry_id),
    KEY ix_journal_entries_posting_date (posting_date),
    KEY ix_journal_entries_source_date (journal_source, posting_date),
    KEY ix_journal_entries_created (created_at),
    KEY ix_journal_entries_generation_run (generation_run_id),
    CONSTRAINT fk_journal_entries_period FOREIGN KEY (accounting_period_id) REFERENCES accounting_periods (accounting_period_id),
    CONSTRAINT fk_journal_entries_reverses FOREIGN KEY (reverses_journal_entry_id) REFERENCES journal_entries (journal_entry_id),
    CONSTRAINT fk_journal_entries_goods_receipt FOREIGN KEY (goods_receipt_id) REFERENCES goods_receipts (goods_receipt_id),
    CONSTRAINT fk_journal_entries_supplier_invoice FOREIGN KEY (supplier_invoice_id) REFERENCES supplier_invoices (supplier_invoice_id),
    CONSTRAINT fk_journal_entries_supplier_payment FOREIGN KEY (supplier_payment_id) REFERENCES supplier_payments (supplier_payment_id),
    CONSTRAINT fk_journal_entries_invoice FOREIGN KEY (invoice_id) REFERENCES invoices (invoice_id),
    CONSTRAINT fk_journal_entries_credit_note FOREIGN KEY (credit_note_id) REFERENCES credit_notes (credit_note_id),
    CONSTRAINT fk_journal_entries_customer_payment FOREIGN KEY (customer_payment_id) REFERENCES customer_payments (customer_payment_id),
    CONSTRAINT fk_journal_entries_refund FOREIGN KEY (refund_id) REFERENCES refunds (refund_id),
    CONSTRAINT fk_journal_entries_sales_return FOREIGN KEY (sales_return_id) REFERENCES sales_returns (sales_return_id),
    CONSTRAINT fk_journal_entries_shipment FOREIGN KEY (shipment_id) REFERENCES shipments (shipment_id),
    CONSTRAINT fk_journal_entries_stock_adjustment FOREIGN KEY (stock_adjustment_id) REFERENCES stock_adjustments (stock_adjustment_id),
    CONSTRAINT fk_journal_entries_stock_transfer FOREIGN KEY (stock_transfer_id) REFERENCES stock_transfers (stock_transfer_id),
    CONSTRAINT fk_journal_entries_defective_stock FOREIGN KEY (defective_stock_id) REFERENCES defective_stock (defective_stock_id),
    CONSTRAINT fk_journal_entries_expense FOREIGN KEY (expense_id) REFERENCES expenses (expense_id),
    CONSTRAINT fk_journal_entries_expense_payment FOREIGN KEY (expense_payment_id) REFERENCES expense_payments (expense_payment_id),
    CONSTRAINT fk_journal_entries_bank_transaction FOREIGN KEY (bank_transaction_id) REFERENCES bank_transactions (bank_transaction_id),
    CONSTRAINT fk_journal_entries_gst_return FOREIGN KEY (gst_return_id) REFERENCES gst_returns (gst_return_id),
    CONSTRAINT fk_journal_entries_created_by FOREIGN KEY (created_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_journal_entries_number CHECK (REGEXP_LIKE(journal_number, '^JV/[0-9]{4}-[0-9]{2}/[0-9]{7}$', 'c')),
    CONSTRAINT ck_journal_entries_source CHECK (REGEXP_LIKE(journal_source,
        '^(OPENING_BALANCE|GOODS_RECEIPT|SUPPLIER_INVOICE|SUPPLIER_PAYMENT|SALES_INVOICE|CUSTOMER_PAYMENT|BANK_SETTLEMENT|CREDIT_NOTE|SALES_RETURN_RECEIPT|RTO_RECEIPT|REFUND|STOCK_ADJUSTMENT|TRANSFER_DISPATCH|TRANSFER_RECEIPT|DEFECTIVE_DISPOSITION|EXPENSE|EXPENSE_PAYMENT|BANK_CHARGES|INTEREST|INTERNAL_TRANSFER|GST_SETTLEMENT|DEPRECIATION|MANUAL_ADJUSTMENT|REVERSAL)$', 'c')),
    CONSTRAINT ck_journal_entries_type CHECK (REGEXP_LIKE(journal_type, '^(SYSTEM|RECURRING|OPENING|MANUAL|REVERSAL)$', 'c')),
    CONSTRAINT ck_journal_entries_type_source CHECK (
        (journal_type = 'OPENING')   = (journal_source = 'OPENING_BALANCE')
        AND (journal_type = 'RECURRING') = (journal_source = 'DEPRECIATION')
        AND (journal_type = 'MANUAL')    = (journal_source = 'MANUAL_ADJUSTMENT')
        AND (journal_type = 'REVERSAL')  = (journal_source = 'REVERSAL')),
    -- Balanced by construction (D62); line totals = header totals is cross-row: V-AC-10.
    CONSTRAINT ck_journal_entries_balanced CHECK (total_debit = total_credit AND total_debit > 0),
    CONSTRAINT ck_journal_entries_status CHECK (REGEXP_LIKE(entry_status, '^(POSTED|REVERSED)$', 'c')),
    CONSTRAINT ck_journal_entries_reversal_link CHECK ((journal_type = 'REVERSAL') = (reverses_journal_entry_id IS NOT NULL)),
    CONSTRAINT ck_journal_entries_one_source CHECK (
        (goods_receipt_id IS NOT NULL) + (supplier_invoice_id IS NOT NULL) + (supplier_payment_id IS NOT NULL)
        + (invoice_id IS NOT NULL) + (credit_note_id IS NOT NULL) + (customer_payment_id IS NOT NULL)
        + (refund_id IS NOT NULL) + (sales_return_id IS NOT NULL) + (shipment_id IS NOT NULL)
        + (stock_adjustment_id IS NOT NULL) + (stock_transfer_id IS NOT NULL) + (defective_stock_id IS NOT NULL)
        + (expense_id IS NOT NULL) + (expense_payment_id IS NOT NULL) + (bank_transaction_id IS NOT NULL)
        + (gst_return_id IS NOT NULL) <= 1),
    -- Each event type links its own source document (1E §6 posting rules); reversals and journals
    -- without a source document link none.
    CONSTRAINT ck_journal_entries_source_document CHECK (
        (journal_source = 'GOODS_RECEIPT'         AND goods_receipt_id IS NOT NULL)
     OR (journal_source = 'SUPPLIER_INVOICE'      AND supplier_invoice_id IS NOT NULL)
     OR (journal_source = 'SUPPLIER_PAYMENT'      AND supplier_payment_id IS NOT NULL)
     OR (journal_source = 'SALES_INVOICE'         AND invoice_id IS NOT NULL)
     OR (journal_source = 'CREDIT_NOTE'           AND credit_note_id IS NOT NULL)
     OR (journal_source = 'CUSTOMER_PAYMENT'      AND customer_payment_id IS NOT NULL)
     OR (journal_source = 'REFUND'                AND refund_id IS NOT NULL)
     OR (journal_source = 'SALES_RETURN_RECEIPT'  AND sales_return_id IS NOT NULL)
     OR (journal_source = 'RTO_RECEIPT'           AND shipment_id IS NOT NULL)
     OR (journal_source = 'STOCK_ADJUSTMENT'      AND stock_adjustment_id IS NOT NULL)
     OR (journal_source IN ('TRANSFER_DISPATCH', 'TRANSFER_RECEIPT') AND stock_transfer_id IS NOT NULL)
     OR (journal_source = 'DEFECTIVE_DISPOSITION' AND defective_stock_id IS NOT NULL)
     OR (journal_source = 'EXPENSE'               AND expense_id IS NOT NULL)
     OR (journal_source = 'EXPENSE_PAYMENT'       AND expense_payment_id IS NOT NULL)
     OR (journal_source IN ('BANK_SETTLEMENT', 'BANK_CHARGES', 'INTEREST', 'INTERNAL_TRANSFER') AND bank_transaction_id IS NOT NULL)
     OR (journal_source = 'GST_SETTLEMENT'        AND gst_return_id IS NOT NULL)
     OR (journal_source IN ('OPENING_BALANCE', 'DEPRECIATION', 'MANUAL_ADJUSTMENT', 'REVERSAL')
         AND goods_receipt_id IS NULL AND supplier_invoice_id IS NULL AND supplier_payment_id IS NULL
         AND invoice_id IS NULL AND credit_note_id IS NULL AND customer_payment_id IS NULL AND refund_id IS NULL
         AND sales_return_id IS NULL AND shipment_id IS NULL AND stock_adjustment_id IS NULL
         AND stock_transfer_id IS NULL AND defective_stock_id IS NULL AND expense_id IS NULL
         AND expense_payment_id IS NULL AND bank_transaction_id IS NULL AND gst_return_id IS NULL)),
    CONSTRAINT ck_journal_entries_dates CHECK (posting_date >= document_date OR journal_type = 'REVERSAL'),
    CONSTRAINT ck_journal_entries_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Journal header: one per business event, immutable once posted (1E 3, D50, D61, D62)';

CREATE TABLE journal_entry_lines (
    journal_entry_line_id       BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    journal_entry_id            BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    account_id                  INT UNSIGNED     NOT NULL,
    debit_amount                DECIMAL(18,2)    NOT NULL DEFAULT 0,
    credit_amount               DECIMAL(18,2)    NOT NULL DEFAULT 0,
    cost_center_id              SMALLINT UNSIGNED NULL,
    customer_id                 BIGINT UNSIGNED  NULL,
    supplier_id                 BIGINT UNSIGNED  NULL,
    employee_id                 BIGINT UNSIGNED  NULL,
    warehouse_id                SMALLINT UNSIGNED NULL,
    line_description            VARCHAR(255)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (journal_entry_line_id),
    UNIQUE KEY uq_journal_entry_lines_entry_line (journal_entry_id, line_number),
    KEY ix_journal_entry_lines_account_entry (account_id, journal_entry_id),
    KEY ix_journal_entry_lines_created (created_at),
    CONSTRAINT fk_journal_entry_lines_entry FOREIGN KEY (journal_entry_id) REFERENCES journal_entries (journal_entry_id),
    CONSTRAINT fk_journal_entry_lines_account FOREIGN KEY (account_id) REFERENCES chart_of_accounts (account_id),
    CONSTRAINT fk_journal_entry_lines_cost_center FOREIGN KEY (cost_center_id) REFERENCES cost_centers (cost_center_id),
    CONSTRAINT fk_journal_entry_lines_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    CONSTRAINT fk_journal_entry_lines_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (supplier_id),
    CONSTRAINT fk_journal_entry_lines_employee FOREIGN KEY (employee_id) REFERENCES employees (employee_id),
    CONSTRAINT fk_journal_entry_lines_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT ck_journal_entry_lines_one_side CHECK (
        line_number > 0
        AND ((debit_amount > 0 AND credit_amount = 0) OR (credit_amount > 0 AND debit_amount = 0))),
    CONSTRAINT ck_journal_entry_lines_one_party CHECK (
        (customer_id IS NOT NULL) + (supplier_id IS NOT NULL) + (employee_id IS NOT NULL) <= 1),
    CONSTRAINT ck_journal_entry_lines_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Debit/credit lines: the only accounting source of truth; insert-only (1E 3)';

CREATE TABLE gl_period_balances (
    gl_period_balance_id        BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    account_id                  INT UNSIGNED     NOT NULL,
    accounting_period_id        SMALLINT UNSIGNED NOT NULL,
    opening_balance             DECIMAL(18,2)    NOT NULL,
    period_debit                DECIMAL(18,2)    NOT NULL,
    period_credit               DECIMAL(18,2)    NOT NULL,
    closing_balance             DECIMAL(18,2)    NOT NULL,
    rebuilt_at                  DATETIME(6)      NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (gl_period_balance_id),
    UNIQUE KEY uq_gl_period_balances_account_period (account_id, accounting_period_id),
    CONSTRAINT fk_gl_period_balances_account FOREIGN KEY (account_id) REFERENCES chart_of_accounts (account_id),
    CONSTRAINT fk_gl_period_balances_period FOREIGN KEY (accounting_period_id) REFERENCES accounting_periods (accounting_period_id),
    CONSTRAINT ck_gl_period_balances_movements CHECK (period_debit >= 0 AND period_credit >= 0),
    CONSTRAINT ck_gl_period_balances_closing CHECK (closing_balance = opening_balance + period_debit - period_credit),
    CONSTRAINT ck_gl_period_balances_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Derived, rebuildable balance per account per period, debit-positive (1E 3, R6, D54)';
