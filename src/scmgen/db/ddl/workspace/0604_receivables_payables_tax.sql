-- Workspace schema, batch 6 (Part 1E finance), file 4/5: AR / AP open-item subledgers, their allocations
-- and GST tax transactions. Design: docs/design/phase1_part1e_expenses_accounting_ar_ap_tax.md §5.
-- Conventions: see 0101_reference.sql.
--
-- Same-party rule (D80 pattern): every open item belongs to the same customer / supplier / employee as
-- its source document, enforced by composite FKs, so subledger = control account per party (V-AR-01, V-AP-01).

CREATE TABLE accounts_receivable (
    accounts_receivable_id      BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    customer_id                 BIGINT UNSIGNED  NOT NULL,
    item_type                   VARCHAR(20)      NOT NULL,
    invoice_id                  BIGINT UNSIGNED  NULL,
    credit_note_id              BIGINT UNSIGNED  NULL,
    customer_payment_id         BIGINT UNSIGNED  NULL,
    refund_id                   BIGINT UNSIGNED  NULL,
    document_date               DATE             NOT NULL,
    due_date                    DATE             NOT NULL,
    original_amount             DECIMAL(18,2)    NOT NULL,
    open_amount                 DECIMAL(18,2)    NOT NULL,
    item_status                 VARCHAR(20)      NOT NULL DEFAULT 'OPEN',
    settled_at                  DATETIME(6)      NULL,
    journal_entry_id            BIGINT UNSIGNED  NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (accounts_receivable_id),
    UNIQUE KEY uq_accounts_receivable_invoice (invoice_id),
    UNIQUE KEY uq_accounts_receivable_credit_note (credit_note_id),
    UNIQUE KEY uq_accounts_receivable_customer_payment (customer_payment_id),
    UNIQUE KEY uq_accounts_receivable_refund (refund_id),
    KEY ix_accounts_receivable_customer_status (customer_id, item_status),
    KEY ix_accounts_receivable_status_due (item_status, due_date),
    CONSTRAINT fk_accounts_receivable_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    CONSTRAINT fk_accounts_receivable_invoice FOREIGN KEY (invoice_id, customer_id) REFERENCES invoices (invoice_id, customer_id),
    CONSTRAINT fk_accounts_receivable_credit_note FOREIGN KEY (credit_note_id, customer_id)
        REFERENCES credit_notes (credit_note_id, customer_id),
    CONSTRAINT fk_accounts_receivable_customer_payment FOREIGN KEY (customer_payment_id, customer_id)
        REFERENCES customer_payments (customer_payment_id, customer_id),
    CONSTRAINT fk_accounts_receivable_refund FOREIGN KEY (refund_id) REFERENCES refunds (refund_id),
    CONSTRAINT fk_accounts_receivable_journal FOREIGN KEY (journal_entry_id) REFERENCES journal_entries (journal_entry_id),
    CONSTRAINT ck_accounts_receivable_type CHECK (REGEXP_LIKE(item_type, '^(INVOICE|CREDIT_NOTE|PAYMENT|REFUND)$', 'c')),
    -- Exactly one source document, matching the item type (D49).
    CONSTRAINT ck_accounts_receivable_source CHECK (
        (invoice_id IS NOT NULL) + (credit_note_id IS NOT NULL) + (customer_payment_id IS NOT NULL) + (refund_id IS NOT NULL) = 1
        AND (item_type = 'INVOICE') = (invoice_id IS NOT NULL)
        AND (item_type = 'CREDIT_NOTE') = (credit_note_id IS NOT NULL)
        AND (item_type = 'PAYMENT') = (customer_payment_id IS NOT NULL)
        AND (item_type = 'REFUND') = (refund_id IS NOT NULL)),
    -- Signed: + = customer owes us (invoice, refund), - = we owe the customer (credit note, payment).
    CONSTRAINT ck_accounts_receivable_sign CHECK (
        (item_type IN ('INVOICE', 'REFUND') AND original_amount > 0 AND open_amount >= 0 AND open_amount <= original_amount)
     OR (item_type IN ('CREDIT_NOTE', 'PAYMENT') AND original_amount < 0 AND open_amount <= 0 AND open_amount >= original_amount)),
    CONSTRAINT ck_accounts_receivable_status CHECK (REGEXP_LIKE(item_status, '^(OPEN|PARTIALLY_SETTLED|SETTLED|REVERSED)$', 'c')),
    CONSTRAINT ck_accounts_receivable_open CHECK (
        (open_amount = 0) = (item_status IN ('SETTLED', 'REVERSED'))
        AND (item_status = 'OPEN') = (open_amount = original_amount)
        AND (item_status = 'SETTLED') = (settled_at IS NOT NULL)),
    CONSTRAINT ck_accounts_receivable_dates CHECK (due_date >= document_date),
    CONSTRAINT ck_accounts_receivable_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Open-item AR subledger: invoices, credit notes, payments, refunds (1E 5, D49)';

CREATE TABLE customer_payment_allocations (
    customer_payment_allocation_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    customer_payment_id         BIGINT UNSIGNED  NULL,
    credit_note_id              BIGINT UNSIGNED  NULL,
    invoice_id                  BIGINT UNSIGNED  NULL,
    refund_id                   BIGINT UNSIGNED  NULL,
    allocated_amount            DECIMAL(18,2)    NOT NULL,
    allocation_date             DATE             NOT NULL,
    allocated_by_employee_id    BIGINT UNSIGNED  NULL,
    allocation_status           VARCHAR(20)      NOT NULL DEFAULT 'ACTIVE',
    reversed_at                 DATETIME(6)      NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (customer_payment_allocation_id),
    CONSTRAINT fk_customer_payment_allocations_payment FOREIGN KEY (customer_payment_id)
        REFERENCES customer_payments (customer_payment_id),
    CONSTRAINT fk_customer_payment_allocations_credit_note FOREIGN KEY (credit_note_id) REFERENCES credit_notes (credit_note_id),
    CONSTRAINT fk_customer_payment_allocations_invoice FOREIGN KEY (invoice_id) REFERENCES invoices (invoice_id),
    CONSTRAINT fk_customer_payment_allocations_refund FOREIGN KEY (refund_id) REFERENCES refunds (refund_id),
    CONSTRAINT fk_customer_payment_allocations_allocated_by FOREIGN KEY (allocated_by_employee_id) REFERENCES employees (employee_id),
    -- Credit side (payment or credit note) cleared against a debit side (invoice or refund); refunds only pay out credit notes.
    CONSTRAINT ck_customer_payment_allocations_sides CHECK (
        (customer_payment_id IS NOT NULL) + (credit_note_id IS NOT NULL) = 1
        AND (invoice_id IS NOT NULL) + (refund_id IS NOT NULL) = 1
        AND (refund_id IS NULL OR credit_note_id IS NOT NULL)),
    CONSTRAINT ck_customer_payment_allocations_amount CHECK (allocated_amount > 0),
    CONSTRAINT ck_customer_payment_allocations_status CHECK (
        REGEXP_LIKE(allocation_status, '^(ACTIVE|REVERSED)$', 'c')
        AND (allocation_status = 'REVERSED') = (reversed_at IS NOT NULL)),
    CONSTRAINT ck_customer_payment_allocations_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Clearing of AR items: payment or credit note -> invoice or refund; posts no journal (1E 5, R1, D49)';

CREATE TABLE accounts_payable (
    accounts_payable_id         BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    supplier_id                 BIGINT UNSIGNED  NULL,
    employee_id                 BIGINT UNSIGNED  NULL,
    item_type                   VARCHAR(20)      NOT NULL,
    supplier_invoice_id         BIGINT UNSIGNED  NULL,
    expense_id                  BIGINT UNSIGNED  NULL,
    supplier_payment_id         BIGINT UNSIGNED  NULL,
    document_date               DATE             NOT NULL,
    due_date                    DATE             NOT NULL,
    original_amount             DECIMAL(18,2)    NOT NULL,
    open_amount                 DECIMAL(18,2)    NOT NULL,
    item_status                 VARCHAR(20)      NOT NULL DEFAULT 'OPEN',
    settled_at                  DATETIME(6)      NULL,
    journal_entry_id            BIGINT UNSIGNED  NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (accounts_payable_id),
    UNIQUE KEY uq_accounts_payable_supplier_invoice (supplier_invoice_id),
    UNIQUE KEY uq_accounts_payable_expense (expense_id),
    UNIQUE KEY uq_accounts_payable_supplier_payment (supplier_payment_id),
    KEY ix_accounts_payable_supplier_status (supplier_id, item_status),
    KEY ix_accounts_payable_status_due (item_status, due_date),
    CONSTRAINT fk_accounts_payable_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (supplier_id),
    CONSTRAINT fk_accounts_payable_employee FOREIGN KEY (employee_id) REFERENCES employees (employee_id),
    CONSTRAINT fk_accounts_payable_supplier_invoice FOREIGN KEY (supplier_invoice_id, supplier_id)
        REFERENCES supplier_invoices (supplier_invoice_id, supplier_id),
    CONSTRAINT fk_accounts_payable_supplier_payment FOREIGN KEY (supplier_payment_id, supplier_id)
        REFERENCES supplier_payments (supplier_payment_id, supplier_id),
    -- An expense's payee is either its supplier or its claimant employee (partly-NULL pairs are not checked).
    CONSTRAINT fk_accounts_payable_expense_supplier FOREIGN KEY (expense_id, supplier_id) REFERENCES expenses (expense_id, supplier_id),
    CONSTRAINT fk_accounts_payable_expense_employee FOREIGN KEY (expense_id, employee_id) REFERENCES expenses (expense_id, employee_id),
    CONSTRAINT fk_accounts_payable_expense FOREIGN KEY (expense_id) REFERENCES expenses (expense_id),
    CONSTRAINT fk_accounts_payable_journal FOREIGN KEY (journal_entry_id) REFERENCES journal_entries (journal_entry_id),
    CONSTRAINT ck_accounts_payable_type CHECK (REGEXP_LIKE(item_type, '^(SUPPLIER_INVOICE|EXPENSE|SUPPLIER_PAYMENT)$', 'c')),
    -- Exactly one payee (D51); employees are payees only for expense claims.
    CONSTRAINT ck_accounts_payable_payee CHECK (
        (supplier_id IS NOT NULL) + (employee_id IS NOT NULL) = 1 AND (employee_id IS NULL OR item_type = 'EXPENSE')),
    -- Exactly one source document matching the item type (Q3, D51).
    CONSTRAINT ck_accounts_payable_source CHECK (
        (supplier_invoice_id IS NOT NULL) + (expense_id IS NOT NULL) + (supplier_payment_id IS NOT NULL) = 1
        AND (item_type = 'SUPPLIER_INVOICE') = (supplier_invoice_id IS NOT NULL)
        AND (item_type = 'EXPENSE') = (expense_id IS NOT NULL)
        AND (item_type = 'SUPPLIER_PAYMENT') = (supplier_payment_id IS NOT NULL)),
    -- Signed: + = we owe (invoice, expense), - = advance / unapplied payment.
    CONSTRAINT ck_accounts_payable_sign CHECK (
        (item_type IN ('SUPPLIER_INVOICE', 'EXPENSE') AND original_amount > 0 AND open_amount >= 0 AND open_amount <= original_amount)
     OR (item_type = 'SUPPLIER_PAYMENT' AND original_amount < 0 AND open_amount <= 0 AND open_amount >= original_amount)),
    CONSTRAINT ck_accounts_payable_status CHECK (REGEXP_LIKE(item_status, '^(OPEN|PARTIALLY_SETTLED|SETTLED|REVERSED)$', 'c')),
    CONSTRAINT ck_accounts_payable_open CHECK (
        (open_amount = 0) = (item_status IN ('SETTLED', 'REVERSED'))
        AND (item_status = 'OPEN') = (open_amount = original_amount)
        AND (item_status = 'SETTLED') = (settled_at IS NOT NULL)),
    CONSTRAINT ck_accounts_payable_dates CHECK (due_date >= document_date),
    CONSTRAINT ck_accounts_payable_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Open-item AP subledger: supplier invoices, non-payroll expenses, supplier payments (1E 5, D49, D51)';

CREATE TABLE supplier_payment_allocations (
    supplier_payment_allocation_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    supplier_payment_id         BIGINT UNSIGNED  NOT NULL,
    supplier_invoice_id         BIGINT UNSIGNED  NOT NULL,
    allocated_amount            DECIMAL(18,2)    NOT NULL,
    allocation_date             DATE             NOT NULL,
    allocation_status           VARCHAR(20)      NOT NULL DEFAULT 'ACTIVE',
    reversed_at                 DATETIME(6)      NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (supplier_payment_allocation_id),
    UNIQUE KEY uq_supplier_payment_allocations_payment_invoice (supplier_payment_id, supplier_invoice_id),
    CONSTRAINT fk_supplier_payment_allocations_payment FOREIGN KEY (supplier_payment_id)
        REFERENCES supplier_payments (supplier_payment_id),
    CONSTRAINT fk_supplier_payment_allocations_invoice FOREIGN KEY (supplier_invoice_id)
        REFERENCES supplier_invoices (supplier_invoice_id),
    CONSTRAINT ck_supplier_payment_allocations_amount CHECK (allocated_amount > 0),
    CONSTRAINT ck_supplier_payment_allocations_status CHECK (
        REGEXP_LIKE(allocation_status, '^(ACTIVE|REVERSED)$', 'c')
        AND (allocation_status = 'REVERSED') = (reversed_at IS NOT NULL)),
    CONSTRAINT ck_supplier_payment_allocations_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Supplier payment -> supplier invoice applications; posts no journal (1E 5, D49)';

CREATE TABLE tax_transactions (
    tax_transaction_id          BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    company_gst_registration_id SMALLINT UNSIGNED NOT NULL,
    tax_direction               VARCHAR(10)      NOT NULL,
    document_type               VARCHAR(30)      NOT NULL,
    is_cancellation             BOOLEAN          NOT NULL DEFAULT FALSE,
    invoice_line_item_id        BIGINT UNSIGNED  NULL,
    credit_note_line_item_id    BIGINT UNSIGNED  NULL,
    supplier_invoice_line_item_id BIGINT UNSIGNED NULL,
    expense_line_item_id        BIGINT UNSIGNED  NULL,
    stock_transfer_line_item_id BIGINT UNSIGNED  NULL,
    document_number             VARCHAR(40)      NOT NULL,
    document_date               DATE             NOT NULL,
    return_period               CHAR(7)          GENERATED ALWAYS AS (DATE_FORMAT(document_date, '%Y-%m')) STORED,
    counterparty_gstin          CHAR(15)         NULL,
    customer_id                 BIGINT UNSIGNED  NULL,
    supplier_id                 BIGINT UNSIGNED  NULL,
    place_of_supply_state_id    SMALLINT UNSIGNED NOT NULL,
    is_inter_state              BOOLEAN          NOT NULL,
    hsn_code                    VARCHAR(8)       NOT NULL,
    uqc                         VARCHAR(3)       NULL,
    quantity                    DECIMAL(18,3)    NULL,
    taxable_value               DECIMAL(18,2)    NOT NULL,
    gst_rate                    DECIMAL(7,4)     NOT NULL,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    itc_eligibility             VARCHAR(20)      NOT NULL,
    gstr1_section               VARCHAR(10)      NULL,
    gst_return_id               BIGINT UNSIGNED  NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (tax_transaction_id),
    -- One row per taxable line and direction; a cancelled invoice adds exactly one negative mirror per line.
    UNIQUE KEY uq_tax_transactions_invoice_line (invoice_line_item_id, is_cancellation),
    UNIQUE KEY uq_tax_transactions_credit_note_line (credit_note_line_item_id),
    UNIQUE KEY uq_tax_transactions_supplier_invoice_line (supplier_invoice_line_item_id),
    UNIQUE KEY uq_tax_transactions_expense_line (expense_line_item_id),
    UNIQUE KEY uq_tax_transactions_transfer_line_direction (stock_transfer_line_item_id, tax_direction),
    KEY ix_tax_transactions_registration_period (company_gst_registration_id, return_period, tax_direction),
    KEY ix_tax_transactions_hsn (hsn_code),
    CONSTRAINT fk_tax_transactions_registration FOREIGN KEY (company_gst_registration_id)
        REFERENCES company_gst_registrations (company_gst_registration_id),
    CONSTRAINT fk_tax_transactions_invoice_line FOREIGN KEY (invoice_line_item_id) REFERENCES invoice_line_items (invoice_line_item_id),
    CONSTRAINT fk_tax_transactions_credit_note_line FOREIGN KEY (credit_note_line_item_id)
        REFERENCES credit_note_line_items (credit_note_line_item_id),
    CONSTRAINT fk_tax_transactions_supplier_invoice_line FOREIGN KEY (supplier_invoice_line_item_id)
        REFERENCES supplier_invoice_line_items (supplier_invoice_line_item_id),
    CONSTRAINT fk_tax_transactions_expense_line FOREIGN KEY (expense_line_item_id) REFERENCES expense_line_items (expense_line_item_id),
    CONSTRAINT fk_tax_transactions_transfer_line FOREIGN KEY (stock_transfer_line_item_id)
        REFERENCES stock_transfer_line_items (stock_transfer_line_item_id),
    CONSTRAINT fk_tax_transactions_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    CONSTRAINT fk_tax_transactions_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (supplier_id),
    CONSTRAINT fk_tax_transactions_state FOREIGN KEY (place_of_supply_state_id) REFERENCES states (state_id),
    CONSTRAINT fk_tax_transactions_gst_return FOREIGN KEY (gst_return_id) REFERENCES gst_returns (gst_return_id),
    CONSTRAINT ck_tax_transactions_direction CHECK (REGEXP_LIKE(tax_direction, '^(INPUT|OUTPUT)$', 'c')),
    CONSTRAINT ck_tax_transactions_document_type CHECK (REGEXP_LIKE(document_type,
        '^(SALES_INVOICE|CREDIT_NOTE|SUPPLIER_INVOICE|EXPENSE|TRANSFER_OUT|TRANSFER_IN)$', 'c')),
    -- Exactly one source line, matching document type and direction.
    CONSTRAINT ck_tax_transactions_source CHECK (
        (invoice_line_item_id IS NOT NULL) + (credit_note_line_item_id IS NOT NULL) + (supplier_invoice_line_item_id IS NOT NULL)
        + (expense_line_item_id IS NOT NULL) + (stock_transfer_line_item_id IS NOT NULL) = 1
        AND ((document_type = 'SALES_INVOICE'    AND invoice_line_item_id IS NOT NULL          AND tax_direction = 'OUTPUT')
          OR (document_type = 'CREDIT_NOTE'      AND credit_note_line_item_id IS NOT NULL      AND tax_direction = 'OUTPUT')
          OR (document_type = 'SUPPLIER_INVOICE' AND supplier_invoice_line_item_id IS NOT NULL AND tax_direction = 'INPUT')
          OR (document_type = 'EXPENSE'          AND expense_line_item_id IS NOT NULL          AND tax_direction = 'INPUT')
          OR (document_type = 'TRANSFER_OUT'     AND stock_transfer_line_item_id IS NOT NULL   AND tax_direction = 'OUTPUT')
          OR (document_type = 'TRANSFER_IN'      AND stock_transfer_line_item_id IS NOT NULL   AND tax_direction = 'INPUT'))),
    CONSTRAINT ck_tax_transactions_cancellation CHECK (NOT is_cancellation OR document_type = 'SALES_INVOICE'),
    -- Credit notes and cancellations reduce tax (negative); everything else is positive.
    CONSTRAINT ck_tax_transactions_sign CHECK (
        ((document_type = 'CREDIT_NOTE' OR is_cancellation)
            AND taxable_value <= 0 AND cgst_amount <= 0 AND sgst_amount <= 0 AND igst_amount <= 0)
     OR (document_type <> 'CREDIT_NOTE' AND NOT is_cancellation
            AND taxable_value >= 0 AND cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0)),
    CONSTRAINT ck_tax_transactions_heads CHECK (
        cgst_amount = sgst_amount AND gst_rate BETWEEN 0 AND 50
        AND ((is_inter_state AND cgst_amount = 0) OR (NOT is_inter_state AND igst_amount = 0))),
    CONSTRAINT ck_tax_transactions_itc CHECK (
        REGEXP_LIKE(itc_eligibility, '^(ELIGIBLE|INELIGIBLE|NOT_APPLICABLE)$', 'c')
        AND (tax_direction = 'OUTPUT') = (itc_eligibility = 'NOT_APPLICABLE')),
    CONSTRAINT ck_tax_transactions_gstr1_section CHECK (
        (tax_direction = 'OUTPUT') = (gstr1_section IS NOT NULL)
        AND (gstr1_section IS NULL OR REGEXP_LIKE(gstr1_section, '^(B2B|B2CL|B2CS|CDNR|CDNUR)$', 'c'))),
    CONSTRAINT ck_tax_transactions_parties CHECK (
        (customer_id IS NULL OR tax_direction = 'OUTPUT') AND (supplier_id IS NULL OR tax_direction = 'INPUT')),
    CONSTRAINT ck_tax_transactions_gstin CHECK (counterparty_gstin IS NULL
        OR REGEXP_LIKE(counterparty_gstin, '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$', 'c')),
    CONSTRAINT ck_tax_transactions_hsn CHECK (REGEXP_LIKE(hsn_code, '^[0-9]{4}([0-9]{2}){0,2}$', 'c')),
    CONSTRAINT ck_tax_transactions_uqc CHECK (uqc IS NULL OR REGEXP_LIKE(uqc, '^[A-Z]{3}$', 'c')),
    CONSTRAINT ck_tax_transactions_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'GST per taxable document line and direction; basis for GST reports and returns (1E 5, D58)';
