-- Workspace schema, batch 1/5 of Part 1B: reference tables with no foreign keys (dependency level 0).
-- Design: docs/design/phase1_part1b_master_data_and_accounting_master.md
--
-- Conventions for every workspace DDL file (Part 1B §2, Phase 2 findings):
--   * PK <entity>_id; constraint names uq_/fk_/ck_/ix_<table>_...
--   * Enumerations: CHECK (REGEXP_LIKE(col, '^(A|B)$', 'c')), case-sensitive (D82).
--     A plain IN list would accept 'open' for 'OPEN' under utf8mb4_0900_ai_ci.
--   * Format checks: REGEXP_LIKE(col, pattern, 'c'), never plain REGEXP.
--   * No CHECK may reference an AUTO_INCREMENT column (MySQL error 3818, D83).
--   * FKs without ON DELETE/UPDATE clauses = RESTRICT (D15, D81).
--   * No index may start with a foreign key's columns and continue with a NULLABLE column (D85): on
--     MySQL 26.10 such an index can silently disable the FK when that trailing column is NULL.
--   * created_at / updated_at are written by the engine from the simulated clock (D21, D67):
--     DEFAULT is only a safety net, there is deliberately no ON UPDATE CURRENT_TIMESTAMP.

CREATE TABLE states (
    state_id                    SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    gst_state_code              CHAR(2)          NOT NULL,
    state_code                  CHAR(2)          NOT NULL,
    state_name                  VARCHAR(60)      NOT NULL,
    is_union_territory          BOOLEAN          NOT NULL DEFAULT FALSE,
    intra_state_tax_component   VARCHAR(5)       NOT NULL DEFAULT 'SGST',
    region                      VARCHAR(20)      NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (state_id),
    UNIQUE KEY uq_states_gst_state_code (gst_state_code),
    UNIQUE KEY uq_states_state_code (state_code),
    UNIQUE KEY uq_states_state_name (state_name),
    CONSTRAINT ck_states_gst_state_code CHECK (REGEXP_LIKE(gst_state_code, '^[0-9]{2}$', 'c')),
    CONSTRAINT ck_states_state_code CHECK (REGEXP_LIKE(state_code, '^[A-Z]{2}$', 'c')),
    CONSTRAINT ck_states_tax_component CHECK (REGEXP_LIKE(intra_state_tax_component, '^(SGST|UTGST)$', 'c')),
    CONSTRAINT ck_states_ut_component CHECK (is_union_territory OR intra_state_tax_component = 'SGST'),
    CONSTRAINT ck_states_region CHECK (REGEXP_LIKE(region, '^(NORTH|SOUTH|EAST|WEST|CENTRAL|NORTH_EAST)$', 'c')),
    CONSTRAINT ck_states_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Indian states / union territories with GST state code (1B 4.1)';

CREATE TABLE account_types (
    account_type_id             TINYINT UNSIGNED NOT NULL AUTO_INCREMENT,
    account_type_code           VARCHAR(20)      NOT NULL,
    account_type_name           VARCHAR(60)      NOT NULL,
    normal_balance              VARCHAR(6)       NOT NULL,
    financial_statement         VARCHAR(20)      NOT NULL,
    code_prefix                 CHAR(1)          NOT NULL,
    display_order               TINYINT UNSIGNED NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (account_type_id),
    UNIQUE KEY uq_account_types_code (account_type_code),
    UNIQUE KEY uq_account_types_name (account_type_name),
    UNIQUE KEY uq_account_types_code_prefix (code_prefix),
    CONSTRAINT ck_account_types_code CHECK (REGEXP_LIKE(account_type_code, '^(ASSET|LIABILITY|EQUITY|REVENUE|COGS|EXPENSE)$', 'c')),
    CONSTRAINT ck_account_types_normal_balance CHECK (REGEXP_LIKE(normal_balance, '^(DEBIT|CREDIT)$', 'c')),
    CONSTRAINT ck_account_types_statement CHECK (REGEXP_LIKE(financial_statement, '^(BALANCE_SHEET|PROFIT_AND_LOSS)$', 'c')),
    CONSTRAINT ck_account_types_code_prefix CHECK (REGEXP_LIKE(code_prefix, '^[1-9]$', 'c')),
    CONSTRAINT ck_account_types_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'GL account classes with normal balance side (1B 4.9)';

CREATE TABLE departments (
    department_id               SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    department_code             VARCHAR(10)      NOT NULL,
    department_name             VARCHAR(100)     NOT NULL,
    is_active                   BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (department_id),
    UNIQUE KEY uq_departments_code (department_code),
    UNIQUE KEY uq_departments_name (department_name),
    CONSTRAINT ck_departments_code CHECK (REGEXP_LIKE(department_code, '^[A-Z][A-Z0-9_]{1,9}$', 'c')),
    CONSTRAINT ck_departments_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Organisational departments (1B 4.2)';

CREATE TABLE product_units_of_measure (
    unit_of_measure_id          SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    uom_code                    VARCHAR(10)      NOT NULL,
    uom_name                    VARCHAR(50)      NOT NULL,
    uom_category                VARCHAR(30)      NOT NULL,
    decimal_places              TINYINT UNSIGNED NOT NULL,
    gst_uqc                     VARCHAR(3)       NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (unit_of_measure_id),
    UNIQUE KEY uq_product_units_of_measure_code (uom_code),
    CONSTRAINT ck_product_units_of_measure_code CHECK (REGEXP_LIKE(uom_code, '^[A-Z]{2,10}$', 'c')),
    CONSTRAINT ck_product_units_of_measure_category CHECK (REGEXP_LIKE(uom_category, '^(COUNT|WEIGHT|VOLUME|LENGTH)$', 'c')),
    CONSTRAINT ck_product_units_of_measure_decimals CHECK (decimal_places <= 3),
    CONSTRAINT ck_product_units_of_measure_uqc CHECK (REGEXP_LIKE(gst_uqc, '^[A-Z]{3}$', 'c')),
    CONSTRAINT ck_product_units_of_measure_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Units of measure with GST unique quantity code (1B 4.6, D18)';

CREATE TABLE product_brands (
    product_brand_id            INT UNSIGNED     NOT NULL AUTO_INCREMENT,
    brand_code                  VARCHAR(20)      NOT NULL,
    brand_name                  VARCHAR(100)     NOT NULL,
    brand_tier                  VARCHAR(30)      NOT NULL,
    is_private_label            BOOLEAN          NOT NULL DEFAULT FALSE,
    is_active                   BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (product_brand_id),
    UNIQUE KEY uq_product_brands_code (brand_code),
    UNIQUE KEY uq_product_brands_name (brand_name),
    CONSTRAINT ck_product_brands_code CHECK (REGEXP_LIKE(brand_code, '^[A-Z0-9][A-Z0-9-]{1,19}$', 'c')),
    CONSTRAINT ck_product_brands_tier CHECK (REGEXP_LIKE(brand_tier, '^(ECONOMY|MID|PREMIUM)$', 'c')),
    CONSTRAINT ck_product_brands_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Fictional product brands (1B 4.6)';

CREATE TABLE payment_methods (
    payment_method_id           SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    method_code                 VARCHAR(20)      NOT NULL,
    method_name                 VARCHAR(60)      NOT NULL,
    is_electronic               BOOLEAN          NOT NULL,
    usable_for_receipts         BOOLEAN          NOT NULL,
    usable_for_payments         BOOLEAN          NOT NULL,
    settlement_lag_days         TINYINT UNSIGNED NOT NULL DEFAULT 0,
    gateway_fee_percent         DECIMAL(7,4)     NULL,
    min_amount                  DECIMAL(18,2)    NULL,
    max_amount                  DECIMAL(18,2)    NULL,
    is_active                   BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (payment_method_id),
    UNIQUE KEY uq_payment_methods_code (method_code),
    CONSTRAINT ck_payment_methods_code CHECK (REGEXP_LIKE(method_code, '^[A-Z][A-Z0-9_]{1,19}$', 'c')),
    CONSTRAINT ck_payment_methods_usage CHECK (usable_for_receipts OR usable_for_payments),
    CONSTRAINT ck_payment_methods_fee CHECK (gateway_fee_percent IS NULL OR gateway_fee_percent BETWEEN 0 AND 100),
    CONSTRAINT ck_payment_methods_limits CHECK (
        (min_amount IS NULL OR min_amount > 0) AND (max_amount IS NULL OR max_amount > 0)
        AND (min_amount IS NULL OR max_amount IS NULL OR min_amount <= max_amount)),
    CONSTRAINT ck_payment_methods_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Payment methods with settlement lag, gateway fee and legal limits (1B 4.7)';

CREATE TABLE payment_terms (
    payment_term_id                 SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    term_code                       VARCHAR(20)      NOT NULL,
    term_name                       VARCHAR(60)      NOT NULL,
    due_days                        SMALLINT UNSIGNED NOT NULL,
    is_prepaid                      BOOLEAN          NOT NULL DEFAULT FALSE,
    early_payment_discount_percent  DECIMAL(7,4)     NULL,
    early_payment_discount_days     SMALLINT UNSIGNED NULL,
    is_active                       BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at                      DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                      DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (payment_term_id),
    UNIQUE KEY uq_payment_terms_code (term_code),
    CONSTRAINT ck_payment_terms_code CHECK (REGEXP_LIKE(term_code, '^[A-Z0-9][A-Z0-9_]{1,19}$', 'c')),
    CONSTRAINT ck_payment_terms_discount_pair CHECK (
        (early_payment_discount_percent IS NULL) = (early_payment_discount_days IS NULL)),
    CONSTRAINT ck_payment_terms_discount_values CHECK (
        early_payment_discount_percent IS NULL
        OR (early_payment_discount_percent > 0 AND early_payment_discount_percent < 100
            AND early_payment_discount_days < due_days)),
    CONSTRAINT ck_payment_terms_prepaid CHECK (NOT is_prepaid OR due_days = 0),
    CONSTRAINT ck_payment_terms_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Credit terms for customers and suppliers (1B 4.7, R15)';

CREATE TABLE sales_channels (
    sales_channel_id            SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    channel_code                VARCHAR(20)      NOT NULL,
    channel_name                VARCHAR(60)      NOT NULL,
    is_online                   BOOLEAN          NOT NULL,
    requires_shipment           BOOLEAN          NOT NULL,
    allowed_customer_type       VARCHAR(10)      NOT NULL,
    commission_percent          DECIMAL(7,4)     NULL,
    is_active                   BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (sales_channel_id),
    UNIQUE KEY uq_sales_channels_code (channel_code),
    CONSTRAINT ck_sales_channels_code CHECK (REGEXP_LIKE(channel_code, '^[A-Z][A-Z0-9_]{1,19}$', 'c')),
    CONSTRAINT ck_sales_channels_customer_type CHECK (REGEXP_LIKE(allowed_customer_type, '^(B2B|B2C|BOTH)$', 'c')),
    CONSTRAINT ck_sales_channels_commission CHECK (commission_percent IS NULL OR commission_percent BETWEEN 0 AND 100),
    CONSTRAINT ck_sales_channels_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Where an order is placed (A1 8.1, D10)';

CREATE TABLE marketing_channels (
    marketing_channel_id        SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    channel_code                VARCHAR(20)      NOT NULL,
    channel_name                VARCHAR(60)      NOT NULL,
    channel_group               VARCHAR(30)      NOT NULL,
    is_paid                     BOOLEAN          NOT NULL,
    supports_campaigns          BOOLEAN          NOT NULL,
    is_active                   BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (marketing_channel_id),
    UNIQUE KEY uq_marketing_channels_code (channel_code),
    CONSTRAINT ck_marketing_channels_code CHECK (REGEXP_LIKE(channel_code, '^[A-Z][A-Z0-9_]{1,19}$', 'c')),
    CONSTRAINT ck_marketing_channels_group CHECK (REGEXP_LIKE(channel_group,
        '^(PAID_SOCIAL|PAID_SEARCH|ORGANIC|OWNED|REFERRAL|OFFLINE|DIRECT)$', 'c')),
    CONSTRAINT ck_marketing_channels_paid_groups CHECK (channel_group NOT IN ('PAID_SOCIAL', 'PAID_SEARCH') OR is_paid),
    CONSTRAINT ck_marketing_channels_free_groups CHECK (channel_group NOT IN ('ORGANIC', 'DIRECT') OR NOT is_paid),
    CONSTRAINT ck_marketing_channels_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'How a customer came to us (A1 8.1, D10)';
