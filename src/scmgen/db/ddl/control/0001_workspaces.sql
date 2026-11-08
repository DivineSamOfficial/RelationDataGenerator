-- scm_control.workspaces: platform registry of company workspaces (Part 1A A2, Part 1G §5.4).
-- Platform metadata, not business data: timestamps are wall clock (D74), so ON UPDATE is fine here.
-- Naming convention for all DDL: pk_ (implicit PRIMARY), uq_<table>_<cols>, fk_<table>_<target>,
-- ck_<table>_<rule>, ix_<table>_<cols>.

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id            INT UNSIGNED     NOT NULL AUTO_INCREMENT,
    workspace_code          VARCHAR(10)      NOT NULL,
    company_name            VARCHAR(200)     NOT NULL,
    industries              JSON             NOT NULL,
    oltp_schema             VARCHAR(64)      NOT NULL,
    sim_schema              VARCHAR(64)      NOT NULL,
    duckdb_path             VARCHAR(300)     NOT NULL,
    duckdb_sim_path         VARCHAR(300)     NOT NULL,
    base_seed               BIGINT UNSIGNED  NOT NULL,
    build_id                CHAR(36)         NOT NULL,
    config_snapshot         JSON             NOT NULL,
    workspace_status        VARCHAR(20)      NOT NULL,
    last_committed_through  DATETIME(6)      NULL,
    created_at              DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at              DATETIME(6)      NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (workspace_id),
    UNIQUE KEY uq_workspaces_workspace_code (workspace_code),
    UNIQUE KEY uq_workspaces_oltp_schema (oltp_schema),
    UNIQUE KEY uq_workspaces_sim_schema (sim_schema),
    UNIQUE KEY uq_workspaces_build_id (build_id),
    -- 'c' = case-sensitive match: the column collation is case-insensitive, codes must be lower case.
    CONSTRAINT ck_workspaces_code_format CHECK (REGEXP_LIKE(workspace_code, '^[a-z][a-z0-9]{1,9}$', 'c')),
    CONSTRAINT ck_workspaces_status CHECK (workspace_status IN
        ('CREATING', 'READY', 'GENERATING', 'REBUILDING', 'NEEDS_ATTENTION', 'FAILED', 'ARCHIVED')),
    CONSTRAINT ck_workspaces_industries CHECK (JSON_TYPE(industries) = 'ARRAY' AND JSON_LENGTH(industries) >= 1),
    CONSTRAINT ck_workspaces_build_id CHECK (REGEXP_LIKE(build_id, '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', 'c')),
    CONSTRAINT ck_workspaces_distinct_schemas CHECK (oltp_schema <> sim_schema)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci;
