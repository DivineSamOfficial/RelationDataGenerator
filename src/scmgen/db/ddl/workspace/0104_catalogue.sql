-- Workspace schema, batch 4/5 of Part 1B: product catalogue (dependency levels 3-5).
-- Conventions: see 0101_reference.sql.

CREATE TABLE product_categories (
    product_category_id         INT UNSIGNED     NOT NULL AUTO_INCREMENT,
    category_code               VARCHAR(20)      NOT NULL,
    category_name               VARCHAR(100)     NOT NULL,
    parent_category_id          INT UNSIGNED     NULL,
    business_unit_id            SMALLINT UNSIGNED NOT NULL,
    category_level              TINYINT UNSIGNED NOT NULL,
    is_leaf                     BOOLEAN          NOT NULL,
    return_window_days          SMALLINT UNSIGNED NOT NULL,
    is_active                   BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (product_category_id),
    UNIQUE KEY uq_product_categories_code (category_code),
    UNIQUE KEY uq_product_categories_id_bu (product_category_id, business_unit_id),
    UNIQUE KEY uq_product_categories_parent_name (parent_category_id, category_name),
    CONSTRAINT fk_product_categories_business_unit FOREIGN KEY (business_unit_id) REFERENCES business_units (business_unit_id),
    -- A child category is always in its parent's business unit (1B 2.4a).
    CONSTRAINT fk_product_categories_parent FOREIGN KEY (parent_category_id, business_unit_id)
        REFERENCES product_categories (product_category_id, business_unit_id),
    CONSTRAINT ck_product_categories_code CHECK (REGEXP_LIKE(category_code, '^[A-Z0-9][A-Z0-9-]{1,19}$', 'c')),
    CONSTRAINT ck_product_categories_level CHECK (category_level BETWEEN 1 AND 3),
    CONSTRAINT ck_product_categories_top_level CHECK ((category_level = 1) = (parent_category_id IS NULL)),
    CONSTRAINT ck_product_categories_return_window CHECK (return_window_days <= 365),
    CONSTRAINT ck_product_categories_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Hierarchical product categories, max 3 levels, owned by a business unit (1B 4.6)';

CREATE TABLE products (
    product_id                  BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    sku                         VARCHAR(30)      NOT NULL,
    product_name                VARCHAR(200)     NOT NULL,
    product_description         VARCHAR(500)     NULL,
    business_unit_id            SMALLINT UNSIGNED NOT NULL,
    product_category_id         INT UNSIGNED     NOT NULL,
    product_brand_id            INT UNSIGNED     NOT NULL,
    base_unit_of_measure_id     SMALLINT UNSIGNED NOT NULL,
    hsn_code                    VARCHAR(8)       NOT NULL,
    style_code                  VARCHAR(30)      NULL,
    variant_size                VARCHAR(20)      NULL,
    variant_colour              VARCHAR(30)      NULL,
    list_price                  DECIMAL(18,4)    NOT NULL,
    mrp                         DECIMAL(18,4)    NULL,
    pack_size                   DECIMAL(18,3)    NULL,
    weight_kg                   DECIMAL(10,3)    NOT NULL,
    volume_litres               DECIMAL(10,3)    NOT NULL,
    is_batch_tracked            BOOLEAN          NOT NULL DEFAULT FALSE,
    has_expiry                  BOOLEAN          NOT NULL DEFAULT FALSE,
    shelf_life_days             SMALLINT UNSIGNED NULL,
    warranty_months             TINYINT UNSIGNED NULL,
    is_returnable               BOOLEAN          NOT NULL DEFAULT TRUE,
    reorder_level               DECIMAL(18,3)    NOT NULL,
    reorder_quantity            DECIMAL(18,3)    NOT NULL,
    launch_date                 DATE             NOT NULL,
    discontinued_date           DATE             NULL,
    product_status              VARCHAR(30)      NOT NULL DEFAULT 'ACTIVE',
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    deleted_at                  DATETIME(6)      NULL,
    PRIMARY KEY (product_id),
    UNIQUE KEY uq_products_sku (sku),
    KEY ix_products_bu_status (business_unit_id, product_status),
    KEY ix_products_hsn (hsn_code),
    KEY ix_products_style (style_code),
    -- The product's business unit always matches its category's (1B 2.4a).
    CONSTRAINT fk_products_category_bu FOREIGN KEY (product_category_id, business_unit_id)
        REFERENCES product_categories (product_category_id, business_unit_id),
    CONSTRAINT fk_products_business_unit FOREIGN KEY (business_unit_id) REFERENCES business_units (business_unit_id),
    CONSTRAINT fk_products_brand FOREIGN KEY (product_brand_id) REFERENCES product_brands (product_brand_id),
    CONSTRAINT fk_products_uom FOREIGN KEY (base_unit_of_measure_id) REFERENCES product_units_of_measure (unit_of_measure_id),
    CONSTRAINT ck_products_sku CHECK (REGEXP_LIKE(sku, '^[A-Z0-9][A-Z0-9-]{2,29}$', 'c')),
    CONSTRAINT ck_products_hsn CHECK (REGEXP_LIKE(hsn_code, '^[0-9]{4}([0-9]{2}){0,2}$', 'c')),
    CONSTRAINT ck_products_prices CHECK (list_price > 0 AND (mrp IS NULL OR mrp >= list_price)),
    CONSTRAINT ck_products_physical CHECK (weight_kg > 0 AND volume_litres > 0 AND (pack_size IS NULL OR pack_size > 0)),
    CONSTRAINT ck_products_expiry CHECK (NOT has_expiry OR (shelf_life_days IS NOT NULL AND shelf_life_days > 0 AND is_batch_tracked)),
    CONSTRAINT ck_products_reorder CHECK (reorder_level >= 0 AND reorder_quantity > 0),
    CONSTRAINT ck_products_status CHECK (REGEXP_LIKE(product_status, '^(ACTIVE|DISCONTINUED)$', 'c')),
    CONSTRAINT ck_products_discontinued CHECK ((product_status = 'DISCONTINUED') = (discontinued_date IS NOT NULL)),
    CONSTRAINT ck_products_dates CHECK (discontinued_date IS NULL OR discontinued_date >= launch_date),
    CONSTRAINT ck_products_audit CHECK (updated_at >= created_at AND (deleted_at IS NULL OR deleted_at >= created_at))
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Sellable and purchasable SKUs; list_price excludes GST, mrp includes it (1B 4.6, D37)';

CREATE TABLE product_tax_rates (
    product_tax_rate_id         BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    product_id                  BIGINT UNSIGNED  NOT NULL,
    gst_rate                    DECIMAL(7,4)     NOT NULL,
    effective_from              DATE             NOT NULL,
    effective_to                DATE             NULL,
    change_reason               VARCHAR(30)      NOT NULL,
    notification_reference      VARCHAR(60)      NULL,
    current_key                 BIGINT UNSIGNED  GENERATED ALWAYS AS (IF(effective_to IS NULL, product_id, NULL)) STORED,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (product_tax_rate_id),
    UNIQUE KEY uq_product_tax_rates_product_from (product_id, effective_from),
    -- Exactly one open-ended (current) rate per product.
    UNIQUE KEY uq_product_tax_rates_current (current_key),
    CONSTRAINT fk_product_tax_rates_product FOREIGN KEY (product_id) REFERENCES products (product_id),
    -- Allowed slabs are validated against the industry pack (V-MD-20), not hardcoded here.
    CONSTRAINT ck_product_tax_rates_rate CHECK (gst_rate BETWEEN 0 AND 50),
    CONSTRAINT ck_product_tax_rates_dates CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CONSTRAINT ck_product_tax_rates_reason CHECK (REGEXP_LIKE(change_reason, '^(INITIAL|GST_RATE_REVISION|RECLASSIFICATION)$', 'c')),
    CONSTRAINT ck_product_tax_rates_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Effective-dated GST rate per product (1B 4.6, D17)';
