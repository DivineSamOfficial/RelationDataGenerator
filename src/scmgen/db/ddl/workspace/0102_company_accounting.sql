-- Workspace schema, batch 2/5 of Part 1B: company, GST registrations, chart of accounts and the
-- master tables that point into it (dependency levels 1-3). Conventions: see 0101_reference.sql.

CREATE TABLE companies (
    company_id                  SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    company_code                VARCHAR(10)      NOT NULL,
    legal_name                  VARCHAR(200)     NOT NULL,
    trade_name                  VARCHAR(200)     NOT NULL,
    cin                         CHAR(21)         NOT NULL,
    pan                         CHAR(10)         NOT NULL,
    registered_state_id         SMALLINT UNSIGNED NOT NULL,
    registered_address_line1    VARCHAR(200)     NOT NULL,
    registered_address_line2    VARCHAR(200)     NULL,
    registered_city             VARCHAR(80)      NOT NULL,
    registered_pincode          CHAR(6)          NOT NULL,
    incorporation_date          DATE             NOT NULL,
    base_currency_code          CHAR(3)          NOT NULL DEFAULT 'INR',
    fy_start_month              TINYINT UNSIGNED NOT NULL DEFAULT 4,
    email                       VARCHAR(150)     NOT NULL,
    phone                       VARCHAR(10)      NOT NULL,
    website                     VARCHAR(150)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (company_id),
    UNIQUE KEY uq_companies_code (company_code),
    UNIQUE KEY uq_companies_cin (cin),
    UNIQUE KEY uq_companies_pan (pan),
    CONSTRAINT fk_companies_registered_state FOREIGN KEY (registered_state_id) REFERENCES states (state_id),
    CONSTRAINT ck_companies_code CHECK (REGEXP_LIKE(company_code, '^[a-z][a-z0-9]{1,9}$', 'c')),
    CONSTRAINT ck_companies_cin CHECK (REGEXP_LIKE(cin, '^[UL][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$', 'c')),
    CONSTRAINT ck_companies_pan CHECK (REGEXP_LIKE(pan, '^[A-Z]{3}C[A-Z][0-9]{4}[A-Z]$', 'c')),
    CONSTRAINT ck_companies_pincode CHECK (REGEXP_LIKE(registered_pincode, '^[1-9][0-9]{5}$', 'c')),
    CONSTRAINT ck_companies_phone CHECK (REGEXP_LIKE(phone, '^[6-9][0-9]{9}$', 'c')),
    CONSTRAINT ck_companies_currency CHECK (REGEXP_LIKE(base_currency_code, '^[A-Z]{3}$', 'c')),
    CONSTRAINT ck_companies_fy_start CHECK (fy_start_month BETWEEN 1 AND 12),
    CONSTRAINT ck_companies_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'The legal entity owning the workspace: exactly one row (1B 4.1, D13, D25)';

CREATE TABLE chart_of_accounts (
    account_id                  INT UNSIGNED     NOT NULL AUTO_INCREMENT,
    account_code                VARCHAR(10)      NOT NULL,
    account_name                VARCHAR(150)     NOT NULL,
    account_type_id             TINYINT UNSIGNED NOT NULL,
    parent_account_id           INT UNSIGNED     NULL,
    account_level               TINYINT UNSIGNED NOT NULL,
    is_postable                 BOOLEAN          NOT NULL,
    normal_balance              VARCHAR(6)       NOT NULL,
    system_role                 VARCHAR(40)      NULL,
    is_control_account          BOOLEAN          NOT NULL DEFAULT FALSE,
    control_subledger           VARCHAR(20)      NULL,
    requires_cost_center        BOOLEAN          NOT NULL DEFAULT FALSE,
    is_derived                  BOOLEAN          NOT NULL DEFAULT FALSE,
    cash_flow_category          VARCHAR(20)      NOT NULL,
    description                 VARCHAR(255)     NULL,
    is_active                   BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (account_id),
    UNIQUE KEY uq_chart_of_accounts_code (account_code),
    UNIQUE KEY uq_chart_of_accounts_name (account_name),
    UNIQUE KEY uq_chart_of_accounts_system_role (system_role),
    UNIQUE KEY uq_chart_of_accounts_id_type (account_id, account_type_id),
    CONSTRAINT fk_chart_of_accounts_type FOREIGN KEY (account_type_id) REFERENCES account_types (account_type_id),
    -- A child account always has its parent's type (1B 2.4a); NULL parent = top level, not checked.
    CONSTRAINT fk_chart_of_accounts_parent FOREIGN KEY (parent_account_id, account_type_id)
        REFERENCES chart_of_accounts (account_id, account_type_id),
    CONSTRAINT ck_chart_of_accounts_code CHECK (REGEXP_LIKE(account_code, '^[1-6][0-9]{4}$', 'c')),
    CONSTRAINT ck_chart_of_accounts_level CHECK (account_level BETWEEN 1 AND 4),
    CONSTRAINT ck_chart_of_accounts_top_level CHECK ((account_level = 1) = (parent_account_id IS NULL)),
    CONSTRAINT ck_chart_of_accounts_normal_balance CHECK (REGEXP_LIKE(normal_balance, '^(DEBIT|CREDIT)$', 'c')),
    -- Role list is extensible (1E); the engine checks the required roles exist (V-AC-04).
    CONSTRAINT ck_chart_of_accounts_system_role CHECK (system_role IS NULL OR REGEXP_LIKE(system_role, '^[A-Z][A-Z_]{2,39}$', 'c')),
    CONSTRAINT ck_chart_of_accounts_subledger CHECK (control_subledger IS NULL OR REGEXP_LIKE(control_subledger, '^(AR|AP|INVENTORY|BANK|GST)$', 'c')),
    CONSTRAINT ck_chart_of_accounts_control CHECK (is_control_account = (control_subledger IS NOT NULL)),
    CONSTRAINT ck_chart_of_accounts_header CHECK (is_postable OR (system_role IS NULL AND NOT is_control_account)),
    CONSTRAINT ck_chart_of_accounts_cash_flow CHECK (REGEXP_LIKE(cash_flow_category, '^(CASH|OPERATING|INVESTING|FINANCING|NOT_APPLICABLE)$', 'c')),
    CONSTRAINT ck_chart_of_accounts_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'General ledger accounts, hierarchical; system_role drives posting rules (1B 4.9, D16, D78)';

CREATE TABLE company_gst_registrations (
    company_gst_registration_id SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    company_id                  SMALLINT UNSIGNED NOT NULL,
    state_id                    SMALLINT UNSIGNED NOT NULL,
    gstin                       CHAR(15)         NOT NULL,
    registration_date           DATE             NOT NULL,
    is_principal_place          BOOLEAN          NOT NULL DEFAULT FALSE,
    registration_status         VARCHAR(30)      NOT NULL DEFAULT 'ACTIVE',
    cancelled_date              DATE             NULL,
    principal_key               SMALLINT UNSIGNED GENERATED ALWAYS AS (IF(is_principal_place, company_id, NULL)) STORED,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (company_gst_registration_id),
    UNIQUE KEY uq_company_gst_registrations_gstin (gstin),
    UNIQUE KEY uq_company_gst_registrations_company_state (company_id, state_id),
    UNIQUE KEY uq_company_gst_registrations_id_state (company_gst_registration_id, state_id),
    UNIQUE KEY uq_company_gst_registrations_principal (principal_key),
    CONSTRAINT fk_company_gst_registrations_company FOREIGN KEY (company_id) REFERENCES companies (company_id),
    CONSTRAINT fk_company_gst_registrations_state FOREIGN KEY (state_id) REFERENCES states (state_id),
    CONSTRAINT ck_company_gst_registrations_gstin CHECK (REGEXP_LIKE(gstin, '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$', 'c')),
    CONSTRAINT ck_company_gst_registrations_status CHECK (REGEXP_LIKE(registration_status, '^(ACTIVE|SUSPENDED|CANCELLED)$', 'c')),
    CONSTRAINT ck_company_gst_registrations_cancelled CHECK ((registration_status = 'CANCELLED') = (cancelled_date IS NOT NULL)),
    CONSTRAINT ck_company_gst_registrations_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'One company GSTIN per state with a warehouse or store (1B 4.1, D3)';

CREATE TABLE business_units (
    business_unit_id            SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    business_unit_code          VARCHAR(10)      NOT NULL,
    business_unit_name          VARCHAR(100)     NOT NULL,
    industry_pack_code          VARCHAR(40)      NOT NULL,
    description                 VARCHAR(255)     NULL,
    revenue_account_id          INT UNSIGNED     NOT NULL,
    sales_returns_account_id    INT UNSIGNED     NOT NULL,
    cogs_account_id             INT UNSIGNED     NOT NULL,
    launched_date               DATE             NOT NULL,
    is_active                   BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (business_unit_id),
    UNIQUE KEY uq_business_units_code (business_unit_code),
    UNIQUE KEY uq_business_units_name (business_unit_name),
    UNIQUE KEY uq_business_units_pack (industry_pack_code),
    UNIQUE KEY uq_business_units_revenue_account (revenue_account_id),
    UNIQUE KEY uq_business_units_returns_account (sales_returns_account_id),
    UNIQUE KEY uq_business_units_cogs_account (cogs_account_id),
    CONSTRAINT fk_business_units_revenue_account FOREIGN KEY (revenue_account_id) REFERENCES chart_of_accounts (account_id),
    CONSTRAINT fk_business_units_returns_account FOREIGN KEY (sales_returns_account_id) REFERENCES chart_of_accounts (account_id),
    CONSTRAINT fk_business_units_cogs_account FOREIGN KEY (cogs_account_id) REFERENCES chart_of_accounts (account_id),
    CONSTRAINT ck_business_units_code CHECK (REGEXP_LIKE(business_unit_code, '^[A-Z][A-Z0-9]{1,9}$', 'c')),
    CONSTRAINT ck_business_units_pack CHECK (REGEXP_LIKE(industry_pack_code, '^[a-z][a-z0-9_]{1,39}$', 'c')),
    CONSTRAINT ck_business_units_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Product divisions, one per industry pack, owning revenue/returns/COGS accounts (1B 4.2, D11, D16)';

CREATE TABLE expense_categories (
    expense_category_id         SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    category_code               VARCHAR(20)      NOT NULL,
    category_name               VARCHAR(100)     NOT NULL,
    category_group              VARCHAR(30)      NOT NULL,
    gl_account_id               INT UNSIGNED     NOT NULL,
    default_gst_rate            DECIMAL(7,4)     NULL,
    is_itc_eligible             BOOLEAN          NOT NULL,
    recurrence                  VARCHAR(20)      NOT NULL,
    payee_type                  VARCHAR(20)      NOT NULL,
    is_active                   BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (expense_category_id),
    UNIQUE KEY uq_expense_categories_code (category_code),
    UNIQUE KEY uq_expense_categories_name (category_name),
    CONSTRAINT fk_expense_categories_gl_account FOREIGN KEY (gl_account_id) REFERENCES chart_of_accounts (account_id),
    CONSTRAINT ck_expense_categories_code CHECK (REGEXP_LIKE(category_code, '^[A-Z][A-Z0-9_]{1,19}$', 'c')),
    CONSTRAINT ck_expense_categories_group CHECK (REGEXP_LIKE(category_group,
        '^(OCCUPANCY|UTILITIES|PEOPLE|TECHNOLOGY|MARKETING|LOGISTICS|PROFESSIONAL|ADMIN|FINANCE)$', 'c')),
    CONSTRAINT ck_expense_categories_gst_rate CHECK (default_gst_rate IS NULL OR default_gst_rate BETWEEN 0 AND 50),
    CONSTRAINT ck_expense_categories_itc CHECK (NOT is_itc_eligible OR default_gst_rate > 0),
    CONSTRAINT ck_expense_categories_recurrence CHECK (REGEXP_LIKE(recurrence, '^(MONTHLY|QUARTERLY|ANNUAL|AD_HOC)$', 'c')),
    CONSTRAINT ck_expense_categories_payee CHECK (REGEXP_LIKE(payee_type, '^(SUPPLIER|EMPLOYEE)$', 'c')),
    CONSTRAINT ck_expense_categories_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Expense classification mapped to GL accounts (1B 4.8, R7)';

CREATE TABLE tax_accounts (
    tax_account_id              SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    company_gst_registration_id SMALLINT UNSIGNED NOT NULL,
    tax_component               VARCHAR(10)      NOT NULL,
    tax_direction               VARCHAR(10)      NOT NULL,
    gl_account_id               INT UNSIGNED     NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (tax_account_id),
    UNIQUE KEY uq_tax_accounts_registration_component_direction (company_gst_registration_id, tax_component, tax_direction),
    UNIQUE KEY uq_tax_accounts_gl_account (gl_account_id),
    CONSTRAINT fk_tax_accounts_registration FOREIGN KEY (company_gst_registration_id)
        REFERENCES company_gst_registrations (company_gst_registration_id),
    CONSTRAINT fk_tax_accounts_gl_account FOREIGN KEY (gl_account_id) REFERENCES chart_of_accounts (account_id),
    CONSTRAINT ck_tax_accounts_component CHECK (REGEXP_LIKE(tax_component, '^(CGST|SGST|UTGST|IGST)$', 'c')),
    CONSTRAINT ck_tax_accounts_direction CHECK (REGEXP_LIKE(tax_direction, '^(INPUT|OUTPUT)$', 'c')),
    CONSTRAINT ck_tax_accounts_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'GL account per GST component and direction per GSTIN (1B 4.9)';
