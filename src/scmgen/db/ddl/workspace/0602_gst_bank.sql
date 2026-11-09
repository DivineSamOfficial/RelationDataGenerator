-- Workspace schema, batch 6 (Part 1E finance), file 2/5: GST returns, bank reconciliations and bank
-- statement lines. Design: docs/design/phase1_part1e_expenses_accounting_ar_ap_tax.md §4-§5.
-- Conventions: see 0101_reference.sql.

CREATE TABLE gst_returns (
    gst_return_id               BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    company_gst_registration_id SMALLINT UNSIGNED NOT NULL,
    return_type                 VARCHAR(10)      NOT NULL,
    return_period               CHAR(7)          NOT NULL,
    due_date                    DATE             NOT NULL,
    filed_date                  DATE             NULL,
    arn                         VARCHAR(20)      NULL,
    output_cgst                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    output_sgst                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    output_igst                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    itc_cgst                    DECIMAL(18,2)    NOT NULL DEFAULT 0,
    itc_sgst                    DECIMAL(18,2)    NOT NULL DEFAULT 0,
    itc_igst                    DECIMAL(18,2)    NOT NULL DEFAULT 0,
    itc_used_cgst               DECIMAL(18,2)    NOT NULL DEFAULT 0,
    itc_used_sgst               DECIMAL(18,2)    NOT NULL DEFAULT 0,
    itc_used_igst               DECIMAL(18,2)    NOT NULL DEFAULT 0,
    cash_paid_cgst              DECIMAL(18,2)    NOT NULL DEFAULT 0,
    cash_paid_sgst              DECIMAL(18,2)    NOT NULL DEFAULT 0,
    cash_paid_igst              DECIMAL(18,2)    NOT NULL DEFAULT 0,
    payment_date                DATE             NULL,
    bank_account_id             SMALLINT UNSIGNED NULL,
    challan_reference           VARCHAR(20)      NULL,
    return_status               VARCHAR(20)      NOT NULL DEFAULT 'DRAFT',
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (gst_return_id),
    UNIQUE KEY uq_gst_returns_registration_type_period (company_gst_registration_id, return_type, return_period),
    UNIQUE KEY uq_gst_returns_arn (arn),
    CONSTRAINT fk_gst_returns_registration FOREIGN KEY (company_gst_registration_id)
        REFERENCES company_gst_registrations (company_gst_registration_id),
    CONSTRAINT fk_gst_returns_bank_account FOREIGN KEY (bank_account_id) REFERENCES bank_accounts (bank_account_id),
    CONSTRAINT ck_gst_returns_type CHECK (REGEXP_LIKE(return_type, '^(GSTR1|GSTR3B)$', 'c')),
    CONSTRAINT ck_gst_returns_period CHECK (REGEXP_LIKE(return_period, '^[0-9]{4}-(0[1-9]|1[0-2])$', 'c')),
    CONSTRAINT ck_gst_returns_status CHECK (REGEXP_LIKE(return_status, '^(DRAFT|FILED|FILED_LATE)$', 'c')),
    CONSTRAINT ck_gst_returns_filed CHECK (
        (return_status = 'DRAFT') = (filed_date IS NULL) AND (return_status = 'DRAFT') = (arn IS NULL)
        AND (return_status = 'FILED_LATE') = (filed_date IS NOT NULL AND filed_date > due_date)),
    CONSTRAINT ck_gst_returns_amounts CHECK (
        output_cgst >= 0 AND output_sgst >= 0 AND output_igst >= 0 AND itc_cgst >= 0 AND itc_sgst >= 0 AND itc_igst >= 0
        AND itc_used_cgst >= 0 AND itc_used_sgst >= 0 AND itc_used_igst >= 0
        AND cash_paid_cgst >= 0 AND cash_paid_sgst >= 0 AND cash_paid_igst >= 0
        AND output_cgst = output_sgst AND itc_cgst = itc_sgst),
    -- GSTR-1 reports outward supplies only: no credit and no payment.
    CONSTRAINT ck_gst_returns_gstr1 CHECK (return_type = 'GSTR3B' OR (
        itc_cgst + itc_sgst + itc_igst + itc_used_cgst + itc_used_sgst + itc_used_igst
        + cash_paid_cgst + cash_paid_sgst + cash_paid_igst = 0)),
    -- A filed GSTR-3B settles its whole output liability by credit used plus cash (D58).
    CONSTRAINT ck_gst_returns_settlement CHECK (return_type = 'GSTR1' OR return_status = 'DRAFT' OR (
        itc_used_cgst + itc_used_sgst + itc_used_igst + cash_paid_cgst + cash_paid_sgst + cash_paid_igst
        = output_cgst + output_sgst + output_igst)),
    CONSTRAINT ck_gst_returns_payment CHECK (
        (cash_paid_cgst + cash_paid_sgst + cash_paid_igst > 0)
            = (payment_date IS NOT NULL AND bank_account_id IS NOT NULL AND challan_reference IS NOT NULL)),
    CONSTRAINT ck_gst_returns_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Monthly GSTR-1 / GSTR-3B per GSTIN; 3B filing settles output tax by credit and cash (1E 5, D58)';

CREATE TABLE gst_return_line_items (
    gst_return_line_item_id     BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    gst_return_id               BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    section_code                VARCHAR(20)      NOT NULL,
    gst_rate                    DECIMAL(7,4)     NULL,
    hsn_code                    VARCHAR(8)       NULL,
    place_of_supply_state_id    SMALLINT UNSIGNED NULL,
    paid_through                VARCHAR(10)      NULL,
    document_count              INT UNSIGNED     NOT NULL DEFAULT 0,
    taxable_value               DECIMAL(18,2)    NOT NULL DEFAULT 0,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (gst_return_line_item_id),
    UNIQUE KEY uq_gst_return_line_items_return_line (gst_return_id, line_number),
    CONSTRAINT fk_gst_return_line_items_return FOREIGN KEY (gst_return_id) REFERENCES gst_returns (gst_return_id),
    CONSTRAINT fk_gst_return_line_items_state FOREIGN KEY (place_of_supply_state_id) REFERENCES states (state_id),
    CONSTRAINT ck_gst_return_line_items_section CHECK (REGEXP_LIKE(section_code,
        '^(B2B|B2CL|B2CS|CDNR|CDNUR|HSN|3[.]1A|4A5|4B|6[.]1)$', 'c')),
    CONSTRAINT ck_gst_return_line_items_hsn CHECK (hsn_code IS NULL OR REGEXP_LIKE(hsn_code, '^[0-9]{4}([0-9]{2}){0,2}$', 'c')),
    CONSTRAINT ck_gst_return_line_items_rate CHECK (gst_rate IS NULL OR gst_rate BETWEEN 0 AND 50),
    CONSTRAINT ck_gst_return_line_items_paid_through CHECK (
        (section_code = '6.1') = (paid_through IS NOT NULL)
        AND (paid_through IS NULL OR REGEXP_LIKE(paid_through, '^(ITC_IGST|ITC_CGST|ITC_SGST|CASH)$', 'c'))),
    -- CGST credit can never pay SGST and vice versa (legal cross-utilisation ban, D58).
    CONSTRAINT ck_gst_return_line_items_cross_use CHECK (
        (paid_through IS NULL OR paid_through <> 'ITC_CGST' OR sgst_amount = 0)
        AND (paid_through IS NULL OR paid_through <> 'ITC_SGST' OR cgst_amount = 0)),
    CONSTRAINT ck_gst_return_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Summarised sections of a GST return, rebuilt from tax_transactions (1E 5)';

CREATE TABLE bank_reconciliations (
    bank_reconciliation_id      BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    bank_account_id             SMALLINT UNSIGNED NOT NULL,
    accounting_period_id        SMALLINT UNSIGNED NOT NULL,
    statement_closing_balance   DECIMAL(18,2)    NOT NULL,
    book_closing_balance        DECIMAL(18,2)    NOT NULL,
    deposits_in_transit_amount  DECIMAL(18,2)    NOT NULL DEFAULT 0,
    unpresented_payments_amount DECIMAL(18,2)    NOT NULL DEFAULT 0,
    unrecorded_bank_items_amount DECIMAL(18,2)   NOT NULL DEFAULT 0,
    difference_amount           DECIMAL(18,2)    NOT NULL,
    reconciliation_status       VARCHAR(30)      NOT NULL DEFAULT 'IN_PROGRESS',
    reconciled_by_employee_id   BIGINT UNSIGNED  NULL,
    reconciled_at               DATETIME(6)      NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (bank_reconciliation_id),
    UNIQUE KEY uq_bank_reconciliations_account_period (bank_account_id, accounting_period_id),
    CONSTRAINT fk_bank_reconciliations_bank_account FOREIGN KEY (bank_account_id) REFERENCES bank_accounts (bank_account_id),
    CONSTRAINT fk_bank_reconciliations_period FOREIGN KEY (accounting_period_id) REFERENCES accounting_periods (accounting_period_id),
    CONSTRAINT fk_bank_reconciliations_reconciled_by FOREIGN KEY (reconciled_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_bank_reconciliations_timing_items CHECK (deposits_in_transit_amount >= 0 AND unpresented_payments_amount >= 0),
    -- book = statement + deposits in transit - unpresented payments + unrecorded bank items (1E 4).
    CONSTRAINT ck_bank_reconciliations_difference CHECK (difference_amount = book_closing_balance
        - (statement_closing_balance + deposits_in_transit_amount - unpresented_payments_amount + unrecorded_bank_items_amount)),
    CONSTRAINT ck_bank_reconciliations_status CHECK (REGEXP_LIKE(reconciliation_status, '^(IN_PROGRESS|RECONCILED|EXCEPTION)$', 'c')),
    CONSTRAINT ck_bank_reconciliations_reconciled CHECK (
        (reconciliation_status = 'RECONCILED') = (reconciled_at IS NOT NULL)
        AND (reconciled_at IS NULL) = (reconciled_by_employee_id IS NULL)
        AND (reconciliation_status <> 'RECONCILED' OR difference_amount = 0)),
    CONSTRAINT ck_bank_reconciliations_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Monthly reconciliation of each bank account: books vs statement (1E 4)';

CREATE TABLE bank_transactions (
    bank_transaction_id         BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    bank_account_id             SMALLINT UNSIGNED NOT NULL,
    statement_date              DATE             NOT NULL,
    value_date                  DATE             NOT NULL,
    transaction_type            VARCHAR(30)      NOT NULL,
    narration                   VARCHAR(200)     NOT NULL,
    bank_reference              VARCHAR(40)      NULL,
    credit_amount               DECIMAL(18,2)    NOT NULL DEFAULT 0,
    debit_amount                DECIMAL(18,2)    NOT NULL DEFAULT 0,
    balance_after               DECIMAL(18,2)    NOT NULL,
    customer_payment_id         BIGINT UNSIGNED  NULL,
    supplier_payment_id         BIGINT UNSIGNED  NULL,
    expense_payment_id          BIGINT UNSIGNED  NULL,
    refund_id                   BIGINT UNSIGNED  NULL,
    gst_return_id               BIGINT UNSIGNED  NULL,
    counter_bank_transaction_id BIGINT UNSIGNED  NULL,
    shipping_carrier_id         SMALLINT UNSIGNED NULL,
    bank_reconciliation_id      BIGINT UNSIGNED  NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (bank_transaction_id),
    UNIQUE KEY uq_bank_transactions_counter (counter_bank_transaction_id),
    KEY ix_bank_transactions_account_date (bank_account_id, statement_date),
    KEY ix_bank_transactions_type_date (transaction_type, statement_date),
    CONSTRAINT fk_bank_transactions_bank_account FOREIGN KEY (bank_account_id) REFERENCES bank_accounts (bank_account_id),
    CONSTRAINT fk_bank_transactions_customer_payment FOREIGN KEY (customer_payment_id) REFERENCES customer_payments (customer_payment_id),
    CONSTRAINT fk_bank_transactions_supplier_payment FOREIGN KEY (supplier_payment_id) REFERENCES supplier_payments (supplier_payment_id),
    CONSTRAINT fk_bank_transactions_expense_payment FOREIGN KEY (expense_payment_id) REFERENCES expense_payments (expense_payment_id),
    CONSTRAINT fk_bank_transactions_refund FOREIGN KEY (refund_id) REFERENCES refunds (refund_id),
    CONSTRAINT fk_bank_transactions_gst_return FOREIGN KEY (gst_return_id) REFERENCES gst_returns (gst_return_id),
    CONSTRAINT fk_bank_transactions_counter FOREIGN KEY (counter_bank_transaction_id) REFERENCES bank_transactions (bank_transaction_id),
    CONSTRAINT fk_bank_transactions_carrier FOREIGN KEY (shipping_carrier_id) REFERENCES shipping_carriers (shipping_carrier_id),
    CONSTRAINT fk_bank_transactions_reconciliation FOREIGN KEY (bank_reconciliation_id)
        REFERENCES bank_reconciliations (bank_reconciliation_id),
    CONSTRAINT ck_bank_transactions_type CHECK (REGEXP_LIKE(transaction_type,
        '^(CUSTOMER_RECEIPT|GATEWAY_SETTLEMENT|COD_REMITTANCE|CHEQUE_DEPOSIT|SUPPLIER_PAYMENT|EXPENSE_PAYMENT|REFUND|GST_PAYMENT|BANK_CHARGES|INTEREST_CREDIT|INTERNAL_TRANSFER|REVERSAL)$', 'c')),
    -- Bank's sign convention: credit = money into our account. Exactly one side per line.
    CONSTRAINT ck_bank_transactions_one_side CHECK (
        (credit_amount > 0 AND debit_amount = 0) OR (debit_amount > 0 AND credit_amount = 0)),
    CONSTRAINT ck_bank_transactions_direction CHECK (
        (transaction_type IN ('CUSTOMER_RECEIPT', 'GATEWAY_SETTLEMENT', 'COD_REMITTANCE', 'CHEQUE_DEPOSIT', 'INTEREST_CREDIT')
            AND credit_amount > 0)
     OR (transaction_type IN ('SUPPLIER_PAYMENT', 'EXPENSE_PAYMENT', 'REFUND', 'GST_PAYMENT', 'BANK_CHARGES') AND debit_amount > 0)
     OR transaction_type IN ('INTERNAL_TRANSFER', 'REVERSAL')),
    CONSTRAINT ck_bank_transactions_one_document CHECK (
        (customer_payment_id IS NOT NULL) + (supplier_payment_id IS NOT NULL) + (expense_payment_id IS NOT NULL)
        + (refund_id IS NOT NULL) + (gst_return_id IS NOT NULL) <= 1),
    -- Document-originated lines link their document (they post nothing themselves); bank-originated
    -- lines link none and post their own journal (1E 4).
    CONSTRAINT ck_bank_transactions_type_document CHECK (
        (transaction_type IN ('CUSTOMER_RECEIPT', 'CHEQUE_DEPOSIT') AND customer_payment_id IS NOT NULL)
     OR (transaction_type = 'SUPPLIER_PAYMENT' AND supplier_payment_id IS NOT NULL)
     OR (transaction_type = 'EXPENSE_PAYMENT' AND expense_payment_id IS NOT NULL)
     OR (transaction_type = 'REFUND' AND refund_id IS NOT NULL)
     OR (transaction_type = 'GST_PAYMENT' AND gst_return_id IS NOT NULL)
     OR (transaction_type IN ('GATEWAY_SETTLEMENT', 'COD_REMITTANCE', 'BANK_CHARGES', 'INTEREST_CREDIT', 'INTERNAL_TRANSFER')
         AND customer_payment_id IS NULL AND supplier_payment_id IS NULL AND expense_payment_id IS NULL
         AND refund_id IS NULL AND gst_return_id IS NULL)
     OR transaction_type = 'REVERSAL'),
    CONSTRAINT ck_bank_transactions_transfer CHECK ((transaction_type = 'INTERNAL_TRANSFER') = (counter_bank_transaction_id IS NOT NULL)),
    CONSTRAINT ck_bank_transactions_cod CHECK ((transaction_type = 'COD_REMITTANCE') = (shipping_carrier_id IS NOT NULL)),
    CONSTRAINT ck_bank_transactions_value_date CHECK (value_date >= statement_date - INTERVAL 3 DAY),
    CONSTRAINT ck_bank_transactions_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Bank statement and petty-cash book lines, bank sign convention (1E 4, D53)';

-- Close the deferred FK from batch 4: clearing receipts link to the settlement line that paid them out (D63).
ALTER TABLE customer_payments
    ADD CONSTRAINT fk_customer_payments_settlement_txn FOREIGN KEY (settlement_bank_transaction_id)
        REFERENCES bank_transactions (bank_transaction_id);
