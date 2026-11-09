-- Workspace schema, batch 7 (Part 1G system tables): document numbering and generation-run control.
-- Design: docs/design/phase1_part1g_generation_loader_cdc_architecture.md §5. Conventions: see 0101_reference.sql.
-- These tables describe the tool's operation, not the business: started_at / finished_at are wall clock
-- (D74); simulated dates are stored separately (sim_start_date, committed_through). The loader FULL-loads them.

CREATE TABLE document_sequences (
    document_sequence_id        INT UNSIGNED     NOT NULL AUTO_INCREMENT,
    document_type               VARCHAR(20)      NOT NULL,
    company_gst_registration_id SMALLINT UNSIGNED NULL,
    fiscal_year                 SMALLINT UNSIGNED NOT NULL,
    last_sequence               INT UNSIGNED     NOT NULL DEFAULT 0,
    -- MySQL treats NULLs as distinct in UNIQUE indexes, so company-wide series (no GSTIN) use key 0.
    series_key                  SMALLINT UNSIGNED GENERATED ALWAYS AS (COALESCE(company_gst_registration_id, 0)) STORED,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (document_sequence_id),
    UNIQUE KEY uq_document_sequences_type_series_year (document_type, series_key, fiscal_year),
    CONSTRAINT fk_document_sequences_registration FOREIGN KEY (company_gst_registration_id)
        REFERENCES company_gst_registrations (company_gst_registration_id),
    -- Extensible list of series (PO, GRN, TAX_INVOICE, JV, ...): format-checked, not enumerated.
    CONSTRAINT ck_document_sequences_type CHECK (REGEXP_LIKE(document_type, '^[A-Z][A-Z_]{1,19}$', 'c')),
    CONSTRAINT ck_document_sequences_year CHECK (fiscal_year BETWEEN 2000 AND 2100),
    CONSTRAINT ck_document_sequences_range CHECK (last_sequence <= 9999999),
    CONSTRAINT ck_document_sequences_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Next document number per type x GSTIN x FY, incremented inside the day transaction (1G 5.1, R20, D75)';

CREATE TABLE generation_runs (
    generation_run_id           BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    run_number                  INT UNSIGNED     NOT NULL,
    build_id                    CHAR(36)         NOT NULL,
    run_mode                    VARCHAR(20)      NOT NULL,
    triggered_by                VARCHAR(10)      NOT NULL,
    base_seed                   BIGINT UNSIGNED  NOT NULL,
    config_snapshot             JSON             NOT NULL,
    config_hash                 CHAR(64)         NOT NULL,
    app_version                 VARCHAR(20)      NOT NULL,
    sim_start_date              DATE             NOT NULL,
    sim_end_date                DATE             NOT NULL,
    committed_through           DATETIME(6)      NULL,
    run_status                  VARCHAR(20)      NOT NULL DEFAULT 'PENDING',
    cancel_requested            BOOLEAN          NOT NULL DEFAULT FALSE,
    process_id                  INT UNSIGNED     NULL,
    started_at                  DATETIME(6)      NULL,
    finished_at                 DATETIME(6)      NULL,
    rows_written                BIGINT UNSIGNED  NOT NULL DEFAULT 0,
    error_message               TEXT             NULL,
    validation_status           VARCHAR(20)      NOT NULL DEFAULT 'NOT_RUN',
    validation_report_path      VARCHAR(300)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (generation_run_id),
    UNIQUE KEY uq_generation_runs_run_number (run_number),
    KEY ix_generation_runs_status (run_status),
    CONSTRAINT ck_generation_runs_build_id CHECK (REGEXP_LIKE(build_id,
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', 'c')),
    CONSTRAINT ck_generation_runs_mode CHECK (REGEXP_LIKE(run_mode, '^(HISTORICAL|CONTINUE)$', 'c')),
    CONSTRAINT ck_generation_runs_trigger CHECK (REGEXP_LIKE(triggered_by, '^(CLI|UI)$', 'c')),
    CONSTRAINT ck_generation_runs_config CHECK (
        JSON_TYPE(config_snapshot) = 'OBJECT' AND REGEXP_LIKE(config_hash, '^[0-9a-f]{64}$', 'c')),
    CONSTRAINT ck_generation_runs_window CHECK (
        run_number > 0 AND sim_end_date >= sim_start_date
        AND (committed_through IS NULL
             OR (DATE(committed_through) >= sim_start_date AND DATE(committed_through) <= sim_end_date))),
    CONSTRAINT ck_generation_runs_status CHECK (REGEXP_LIKE(run_status, '^(PENDING|RUNNING|COMPLETED|FAILED|CANCELLED)$', 'c')),
    CONSTRAINT ck_generation_runs_started CHECK (run_status = 'PENDING' OR started_at IS NOT NULL),
    CONSTRAINT ck_generation_runs_finished CHECK (
        (run_status IN ('COMPLETED', 'FAILED', 'CANCELLED')) = (finished_at IS NOT NULL)
        AND (finished_at IS NULL OR finished_at >= started_at)),
    CONSTRAINT ck_generation_runs_error CHECK (run_status <> 'FAILED' OR error_message IS NOT NULL),
    -- Validation gate (D76): a report exists once validation has run.
    CONSTRAINT ck_generation_runs_validation CHECK (
        REGEXP_LIKE(validation_status, '^(NOT_RUN|PASSED|FAILED)$', 'c')
        AND (validation_status = 'NOT_RUN') = (validation_report_path IS NULL)),
    CONSTRAINT ck_generation_runs_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'One row per generation run: seed, config, simulated window, committed high-water mark (1G 5.2, D64-D67, D76)';

CREATE TABLE generation_run_steps (
    generation_run_step_id      BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    generation_run_id           BIGINT UNSIGNED  NOT NULL,
    step_name                   VARCHAR(40)      NOT NULL,
    step_order                  TINYINT UNSIGNED NOT NULL,
    step_status                 VARCHAR(20)      NOT NULL DEFAULT 'PENDING',
    planned_units               BIGINT UNSIGNED  NULL,
    completed_units             BIGINT UNSIGNED  NOT NULL DEFAULT 0,
    planned_rows                BIGINT UNSIGNED  NULL,
    generated_rows              BIGINT UNSIGNED  NOT NULL DEFAULT 0,
    current_sim_date            DATE             NULL,
    rows_per_second             DECIMAL(12,2)    NULL,
    rows_by_table               JSON             NULL,
    started_at                  DATETIME(6)      NULL,
    finished_at                 DATETIME(6)      NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (generation_run_step_id),
    UNIQUE KEY uq_generation_run_steps_run_step (generation_run_id, step_name),
    UNIQUE KEY uq_generation_run_steps_run_order (generation_run_id, step_order),
    CONSTRAINT fk_generation_run_steps_run FOREIGN KEY (generation_run_id) REFERENCES generation_runs (generation_run_id),
    CONSTRAINT ck_generation_run_steps_name CHECK (REGEXP_LIKE(step_name,
        '^(SCHEMA|SEED_DATA|MASTER_DATA|OPENING_BALANCES|DAILY_SIMULATION|VALIDATION|FINALISE)$', 'c')),
    CONSTRAINT ck_generation_run_steps_status CHECK (REGEXP_LIKE(step_status, '^(PENDING|RUNNING|COMPLETED|FAILED|SKIPPED)$', 'c')),
    CONSTRAINT ck_generation_run_steps_progress CHECK (
        (planned_units IS NULL OR completed_units <= planned_units)
        AND (rows_per_second IS NULL OR rows_per_second >= 0)
        AND (rows_by_table IS NULL OR JSON_TYPE(rows_by_table) = 'OBJECT')),
    CONSTRAINT ck_generation_run_steps_times CHECK (
        (step_status IN ('PENDING', 'SKIPPED') OR started_at IS NOT NULL)
        AND (step_status IN ('COMPLETED', 'FAILED')) = (finished_at IS NOT NULL)
        AND (finished_at IS NULL OR finished_at >= started_at)),
    CONSTRAINT ck_generation_run_steps_audit CHECK (updated_at >= created_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Progress and throughput per run phase, polled by the control panel (1G 5.3, A1 8.4)';

-- Close the last deferred FKs: every ledger movement and journal records which run wrote it (1G 5.2).
ALTER TABLE inventory_transactions
    ADD CONSTRAINT fk_inventory_transactions_generation_run FOREIGN KEY (generation_run_id)
        REFERENCES generation_runs (generation_run_id);

ALTER TABLE journal_entries
    ADD CONSTRAINT fk_journal_entries_generation_run FOREIGN KEY (generation_run_id)
        REFERENCES generation_runs (generation_run_id);
