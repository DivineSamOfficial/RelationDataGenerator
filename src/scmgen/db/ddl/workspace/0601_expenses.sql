-- Workspace schema, batch 6 (Part 1E finance), file 1/5: expenses.
-- Design: docs/design/phase1_part1e_expenses_accounting_ar_ap_tax.md §2. Conventions: see 0101_reference.sql;
-- amount rules: see 0201_rfq_quotations.sql (D84). Expense lines have no quantity: taxable value is entered.

CREATE TABLE expenses (
    expense_id                  BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    expense_number              VARCHAR(30)      NOT NULL,
    expense_type                VARCHAR(30)      NOT NULL,
    supplier_id                 BIGINT UNSIGNED  NULL,
    employee_id                 BIGINT UNSIGNED  NULL,
    vendor_bill_number          VARCHAR(40)      NULL,
    expense_date                DATE             NOT NULL,
    received_date               DATE             NOT NULL,
    due_date                    DATE             NOT NULL,
    service_period_start        DATE             NULL,
    service_period_end          DATE             NULL,
    company_gst_registration_id SMALLINT UNSIGNED NULL,
    warehouse_id                SMALLINT UNSIGNED NULL,
    is_inter_state              BOOLEAN          NOT NULL DEFAULT FALSE,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    round_off_amount            DECIMAL(18,2)    NOT NULL DEFAULT 0,
    total_amount                DECIMAL(18,2)    NOT NULL,
    itc_eligible_tax_amount     DECIMAL(18,2)    NOT NULL DEFAULT 0,
    recurrence_key              VARCHAR(40)      NULL,
    expense_status              VARCHAR(30)      NOT NULL DEFAULT 'DRAFT',
    submitted_by_employee_id    BIGINT UNSIGNED  NOT NULL,
    approved_by_employee_id     BIGINT UNSIGNED  NULL,
    approved_at                 DATETIME(6)      NULL,
    rejection_reason            VARCHAR(100)     NULL,
    description                 VARCHAR(255)     NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (expense_id),
    UNIQUE KEY uq_expenses_number (expense_number),
    -- Duplicate-bill guard (1E 2).
    UNIQUE KEY uq_expenses_supplier_bill (supplier_id, vendor_bill_number),
    -- Targets of the composite FKs from accounts_payable (same payee).
    UNIQUE KEY uq_expenses_id_supplier (expense_id, supplier_id),
    UNIQUE KEY uq_expenses_id_employee (expense_id, employee_id),
    KEY ix_expenses_date (expense_date),
    KEY ix_expenses_supplier_date (supplier_id, expense_date),
    KEY ix_expenses_status (expense_status),
    KEY ix_expenses_recurrence (recurrence_key, service_period_start),
    CONSTRAINT fk_expenses_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (supplier_id),
    CONSTRAINT fk_expenses_employee FOREIGN KEY (employee_id) REFERENCES employees (employee_id),
    CONSTRAINT fk_expenses_registration FOREIGN KEY (company_gst_registration_id)
        REFERENCES company_gst_registrations (company_gst_registration_id),
    CONSTRAINT fk_expenses_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (warehouse_id),
    CONSTRAINT fk_expenses_submitted_by FOREIGN KEY (submitted_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT fk_expenses_approved_by FOREIGN KEY (approved_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_expenses_number CHECK (REGEXP_LIKE(expense_number, '^EXP/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_expenses_type CHECK (REGEXP_LIKE(expense_type, '^(VENDOR_BILL|EMPLOYEE_CLAIM|PETTY_CASH|PAYROLL)$', 'c')),
    -- Payee by type (1E 2).
    CONSTRAINT ck_expenses_payee CHECK (
        (expense_type = 'VENDOR_BILL'    AND supplier_id IS NOT NULL AND employee_id IS NULL)
     OR (expense_type = 'EMPLOYEE_CLAIM' AND employee_id IS NOT NULL AND supplier_id IS NULL)
     OR (expense_type = 'PAYROLL'        AND supplier_id IS NULL     AND employee_id IS NULL)
     OR (expense_type = 'PETTY_CASH'     AND employee_id IS NULL     AND warehouse_id IS NOT NULL)),
    CONSTRAINT ck_expenses_bill_number CHECK (vendor_bill_number IS NULL OR supplier_id IS NOT NULL),
    CONSTRAINT ck_expenses_dates CHECK (
        received_date >= expense_date AND due_date >= expense_date
        AND (service_period_end IS NULL OR service_period_end >= service_period_start)
        AND (service_period_start IS NULL) = (service_period_end IS NULL)),
    CONSTRAINT ck_expenses_amounts CHECK (
        taxable_amount >= 0 AND cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0
        AND cgst_amount = sgst_amount AND round_off_amount BETWEEN -0.50 AND 0.50
        AND total_amount = taxable_amount + cgst_amount + sgst_amount + igst_amount + round_off_amount
        AND total_amount = ROUND(total_amount, 0)),
    CONSTRAINT ck_expenses_tax_mode CHECK ((is_inter_state AND cgst_amount = 0) OR (NOT is_inter_state AND igst_amount = 0)),
    CONSTRAINT ck_expenses_gst_registration CHECK (
        (company_gst_registration_id IS NULL) = (cgst_amount + sgst_amount + igst_amount = 0)),
    CONSTRAINT ck_expenses_itc CHECK (
        itc_eligible_tax_amount >= 0 AND itc_eligible_tax_amount <= cgst_amount + sgst_amount + igst_amount),
    CONSTRAINT ck_expenses_recurrence_key CHECK (recurrence_key IS NULL OR REGEXP_LIKE(recurrence_key, '^[A-Z][A-Z0-9-]{1,39}$', 'c')),
    CONSTRAINT ck_expenses_status CHECK (REGEXP_LIKE(expense_status, '^(DRAFT|SUBMITTED|APPROVED|REJECTED|CANCELLED)$', 'c')),
    CONSTRAINT ck_expenses_approval CHECK (
        (approved_by_employee_id IS NULL) = (approved_at IS NULL)
        AND (expense_status = 'APPROVED') = (approved_at IS NOT NULL)),
    CONSTRAINT ck_expenses_rejected CHECK ((expense_status = 'REJECTED') = (rejection_reason IS NOT NULL)),
    CONSTRAINT ck_expenses_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Vendor bill, employee claim, petty-cash voucher or monthly payroll (1E 2, D56, D57)';

CREATE TABLE expense_line_items (
    expense_line_item_id        BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    expense_id                  BIGINT UNSIGNED  NOT NULL,
    line_number                 SMALLINT UNSIGNED NOT NULL,
    expense_category_id         SMALLINT UNSIGNED NOT NULL,
    cost_center_id              SMALLINT UNSIGNED NOT NULL,
    marketing_campaign_id       BIGINT UNSIGNED  NULL,
    description                 VARCHAR(255)     NOT NULL,
    sac_code                    VARCHAR(8)       NULL,
    taxable_amount              DECIMAL(18,2)    NOT NULL,
    gst_rate                    DECIMAL(7,4)     NOT NULL DEFAULT 0,
    cgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    sgst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    igst_amount                 DECIMAL(18,2)    NOT NULL DEFAULT 0,
    is_itc_eligible             BOOLEAN          NOT NULL DEFAULT FALSE,
    line_total                  DECIMAL(18,2)    NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (expense_line_item_id),
    UNIQUE KEY uq_expense_line_items_expense_line (expense_id, line_number),
    CONSTRAINT fk_expense_line_items_expense FOREIGN KEY (expense_id) REFERENCES expenses (expense_id),
    CONSTRAINT fk_expense_line_items_category FOREIGN KEY (expense_category_id) REFERENCES expense_categories (expense_category_id),
    CONSTRAINT fk_expense_line_items_cost_center FOREIGN KEY (cost_center_id) REFERENCES cost_centers (cost_center_id),
    CONSTRAINT fk_expense_line_items_campaign FOREIGN KEY (marketing_campaign_id) REFERENCES marketing_campaigns (marketing_campaign_id),
    CONSTRAINT ck_expense_line_items_sac CHECK (sac_code IS NULL OR REGEXP_LIKE(sac_code, '^[0-9]{6}$', 'c')),
    CONSTRAINT ck_expense_line_items_values CHECK (
        line_number > 0 AND taxable_amount > 0 AND gst_rate BETWEEN 0 AND 50 AND (gst_rate > 0 OR NOT is_itc_eligible)),
    CONSTRAINT ck_expense_line_items_tax CHECK (
        ((igst_amount = 0 AND cgst_amount = ROUND(taxable_amount * gst_rate / 200, 2) AND sgst_amount = cgst_amount)
      OR (cgst_amount = 0 AND sgst_amount = 0 AND igst_amount = ROUND(taxable_amount * gst_rate / 100, 2)))
        AND line_total = taxable_amount + cgst_amount + sgst_amount + igst_amount),
    CONSTRAINT ck_expense_line_items_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Expense lines: category, cost centre, optional campaign, GST and input-credit eligibility (1E 2, A1)';

CREATE TABLE expense_payments (
    expense_payment_id          BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    payment_number              VARCHAR(30)      NOT NULL,
    expense_id                  BIGINT UNSIGNED  NOT NULL,
    payment_date                DATE             NOT NULL,
    bank_account_id             SMALLINT UNSIGNED NOT NULL,
    payment_method_id           SMALLINT UNSIGNED NOT NULL,
    amount                      DECIMAL(18,2)    NOT NULL,
    bank_reference              VARCHAR(40)      NULL,
    payment_status              VARCHAR(30)      NOT NULL DEFAULT 'INITIATED',
    failure_reason              VARCHAR(100)     NULL,
    processed_at                DATETIME(6)      NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (expense_payment_id),
    UNIQUE KEY uq_expense_payments_number (payment_number),
    KEY ix_expense_payments_status (payment_status),
    CONSTRAINT fk_expense_payments_expense FOREIGN KEY (expense_id) REFERENCES expenses (expense_id),
    CONSTRAINT fk_expense_payments_bank_account FOREIGN KEY (bank_account_id) REFERENCES bank_accounts (bank_account_id),
    CONSTRAINT fk_expense_payments_method FOREIGN KEY (payment_method_id) REFERENCES payment_methods (payment_method_id),
    CONSTRAINT ck_expense_payments_number CHECK (REGEXP_LIKE(payment_number, '^EPAY/[A-Z]{2}/[0-9]{4}-[0-9]{2}/[0-9]{6}$', 'c')),
    CONSTRAINT ck_expense_payments_amount CHECK (amount > 0),
    CONSTRAINT ck_expense_payments_status CHECK (REGEXP_LIKE(payment_status, '^(INITIATED|COMPLETED|FAILED|REVERSED)$', 'c')),
    CONSTRAINT ck_expense_payments_failure CHECK ((payment_status = 'FAILED') = (failure_reason IS NOT NULL)),
    CONSTRAINT ck_expense_payments_processed CHECK (payment_status = 'INITIATED' OR processed_at IS NOT NULL),
    CONSTRAINT ck_expense_payments_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Payments settling one expense; the FK to expenses is the allocation (1E 2)';

CREATE TABLE expense_attachments_metadata (
    expense_attachment_id       BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    expense_id                  BIGINT UNSIGNED  NOT NULL,
    document_type               VARCHAR(30)      NOT NULL,
    file_name                   VARCHAR(200)     NOT NULL,
    mime_type                   VARCHAR(60)      NOT NULL,
    file_size_bytes             INT UNSIGNED     NOT NULL,
    storage_uri                 VARCHAR(300)     NOT NULL,
    sha256_checksum             CHAR(64)         NOT NULL,
    uploaded_at                 DATETIME(6)      NOT NULL,
    uploaded_by_employee_id     BIGINT UNSIGNED  NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (expense_attachment_id),
    UNIQUE KEY uq_expense_attachments_metadata_checksum (sha256_checksum),
    CONSTRAINT fk_expense_attachments_metadata_expense FOREIGN KEY (expense_id) REFERENCES expenses (expense_id),
    CONSTRAINT fk_expense_attachments_metadata_uploaded_by FOREIGN KEY (uploaded_by_employee_id) REFERENCES employees (employee_id),
    CONSTRAINT ck_expense_attachments_metadata_type CHECK (REGEXP_LIKE(document_type,
        '^(VENDOR_INVOICE|RECEIPT|APPROVAL_EMAIL|CONTRACT|TRAVEL_TICKET)$', 'c')),
    CONSTRAINT ck_expense_attachments_metadata_mime CHECK (REGEXP_LIKE(mime_type, '^(application/pdf|image/jpeg|image/png)$', 'c')),
    CONSTRAINT ck_expense_attachments_metadata_size CHECK (file_size_bytes > 0),
    -- Fictional storage only (D24): the reserved .example domain.
    CONSTRAINT ck_expense_attachments_metadata_uri CHECK (REGEXP_LIKE(storage_uri, '^s3://[a-z0-9.-]+[.]example/', 'c')),
    CONSTRAINT ck_expense_attachments_metadata_checksum CHECK (REGEXP_LIKE(sha256_checksum, '^[0-9a-f]{64}$', 'c')),
    CONSTRAINT ck_expense_attachments_metadata_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Metadata of supporting documents; no binary files stored (1E 2)';
