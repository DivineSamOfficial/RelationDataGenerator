-- Simulation schema scm_<code>_sim (Part 1G §8-§9, Phase 7): the control tables of CDC capture and anomaly
-- simulation. The staging copies of the 92 workspace tables are created next to them by `simulate reset`.
-- Clean generation and validation never read this schema (1G §9.1). Wall-clock columns are marked as such.

CREATE TABLE IF NOT EXISTS change_event_log (
    change_event_id             BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    source_table                VARCHAR(64)      NOT NULL,
    operation                   VARCHAR(10)      NOT NULL,
    row_pk                      BIGINT UNSIGNED  NOT NULL,
    primary_key                 JSON             NOT NULL,
    before_image                JSON             NULL,
    after_image                 JSON             NULL,
    changed_columns             JSON             NULL,
    source_committed_at         DATETIME(6)      NOT NULL,
    capture_batch_id            CHAR(12)         NOT NULL,
    generation_run_id           BIGINT UNSIGNED  NULL,
    is_simulated_anomaly        BOOLEAN          NOT NULL DEFAULT FALSE,
    anomaly_id                  BIGINT UNSIGNED  NULL,
    captured_at                 DATETIME(6)      NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (change_event_id),
    KEY ix_change_event_log_row (source_table, row_pk, change_event_id),
    KEY ix_change_event_log_batch (capture_batch_id),
    CONSTRAINT ck_change_event_log_operation CHECK (REGEXP_LIKE(operation, '^(SNAPSHOT|INSERT|UPDATE|DELETE)$', 'c')),
    CONSTRAINT ck_change_event_log_images CHECK (
        (operation = 'DELETE' AND before_image IS NOT NULL AND after_image IS NULL)
        OR (operation IN ('SNAPSHOT', 'INSERT') AND before_image IS NULL AND after_image IS NOT NULL)
        OR (operation = 'UPDATE' AND before_image IS NOT NULL AND after_image IS NOT NULL)),
    CONSTRAINT ck_change_event_log_anomaly CHECK (is_simulated_anomaly OR anomaly_id IS NULL)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Application-level change events captured from _oltp (1G 8.1, D72, D89); the PK is the LSN';

CREATE TABLE IF NOT EXISTS cdc_capture_watermarks (
    source_table                VARCHAR(64)      NOT NULL,
    last_updated_at             DATETIME(6)      NULL,
    last_pk                     BIGINT UNSIGNED  NULL,
    high_water_mark             DATETIME(6)      NULL,
    captured_events             BIGINT UNSIGNED  NOT NULL DEFAULT 0,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (source_table)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Per captured table: the (updated_at, PK) keyset position of the last capture (D89); wall clock audit';

CREATE TABLE IF NOT EXISTS sim_copy_info (
    info_key                    VARCHAR(40)      NOT NULL,
    info_value                  VARCHAR(300)     NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (info_key)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'When the staging copies were taken and from which build / committed_through (D90); wall clock';

CREATE TABLE IF NOT EXISTS anomaly_scenarios (
    anomaly_scenario_id         BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    scenario_name               VARCHAR(60)      NOT NULL,
    scenario_config             JSON             NOT NULL,
    seed                        BIGINT UNSIGNED  NOT NULL,
    scenario_status             VARCHAR(20)      NOT NULL,
    anomalies_injected          INT UNSIGNED     NOT NULL DEFAULT 0,
    started_at                  DATETIME(6)      NOT NULL,
    finished_at                 DATETIME(6)      NULL,
    error_message               TEXT             NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (anomaly_scenario_id),
    CONSTRAINT ck_anomaly_scenarios_status CHECK (REGEXP_LIKE(scenario_status, '^(RUNNING|COMPLETED|FAILED)$', 'c'))
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'One row per anomaly injection run (1G 9.3); wall clock';

CREATE TABLE IF NOT EXISTS anomaly_registry (
    anomaly_id                  BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    anomaly_scenario_id         BIGINT UNSIGNED  NOT NULL,
    anomaly_type                VARCHAR(40)      NOT NULL,
    target_table                VARCHAR(64)      NOT NULL,
    target_pk                   BIGINT UNSIGNED  NULL,
    target_column               VARCHAR(64)      NULL,
    original_value              TEXT             NULL,
    injected_value              TEXT             NULL,
    expected_failing_checks     JSON             NOT NULL,
    collateral_checks           JSON             NULL,
    injected_at                 DATETIME(6)      NOT NULL,
    created_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (anomaly_id),
    KEY ix_anomaly_registry_scenario (anomaly_scenario_id),
    CONSTRAINT fk_anomaly_registry_scenario FOREIGN KEY (anomaly_scenario_id)
        REFERENCES anomaly_scenarios (anomaly_scenario_id),
    CONSTRAINT ck_anomaly_registry_expected CHECK (JSON_TYPE(expected_failing_checks) = 'ARRAY')
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'One row per injected anomaly with the checks it must trip (1G 9.3-9.4, 15E); wall clock';
