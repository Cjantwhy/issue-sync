-- =========================================================
-- Sheets <-> Jira ticket sync: database schema (PostgreSQL 13+)
-- This file contains tables, constraints and indexes only.
-- Triggers and seed data live in extras.sql.
-- =========================================================

-- ---------- Constant tables ----------
-- Store internal values only, independent of any external system.
-- Mappings from internal values to Jira statuses/priorities/accounts and Slack users
-- live in each adapter's own config file.

CREATE TABLE document_type (
    id    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name  VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE document_record_status (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status_name  VARCHAR(255) NOT NULL UNIQUE,
    is_terminal  BOOLEAN NOT NULL DEFAULT FALSE         -- terminal state (e.g. done)
);

CREATE TABLE ai_judgement_type (
    id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    type_name  VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE ai_judgement_urgency (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    urgency_level_name  VARCHAR(255) NOT NULL UNIQUE
);

-- Assignment target chosen by the AI (role / team)
CREATE TABLE assignee_role (
    id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role_name  VARCHAR(255) NOT NULL UNIQUE
);

-- ---------- Source spreadsheets ----------

CREATE TABLE document (
    id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_type_id  INTEGER NOT NULL REFERENCES document_type (id),
    name              VARCHAR(255) NOT NULL,
    spreadsheet_id    VARCHAR(255) NOT NULL,            -- Google spreadsheet ID (string)
    worksheet_name    VARCHAR(255) NOT NULL,
    mapping           JSONB NOT NULL,                   -- {"title": "Issue title", "description": "Issue description"}
    sync_enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    create_time       TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_time       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (spreadsheet_id, worksheet_name)
);

-- ---------- Ticket records (current snapshot) ----------

CREATE TABLE document_record (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id         INTEGER NOT NULL REFERENCES document (id),
    system_id           UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),  -- written back to the sheet; used in detail page links

    -- Fields owned by the spreadsheet
    title               VARCHAR(255),
    description         TEXT,
    reporter            VARCHAR(255),
    contact             VARCHAR(255),

    -- Fields owned by Jira (updated via webhook)
    status_id           INTEGER REFERENCES document_record_status (id),
    current_priority    VARCHAR(255),
    current_assignee    VARCHAR(255),

    -- Raw data and change detection
    raw                 JSONB,
    raw_hash            CHAR(64),                       -- SHA-256 hex
    sheet_written_hash  CHAR(64),                       -- content hash of the last successful sheet write-back
    sheet_written_at    TIMESTAMPTZ,
    source_deleted_at   TIMESTAMPTZ,                    -- when the row was deleted from the sheet

    create_time         TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_time         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Event log (append-only) ----------

CREATE TABLE document_record_history (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_record_id  BIGINT NOT NULL REFERENCES document_record (id),
    event_type          VARCHAR(32) NOT NULL
        CHECK (event_type IN ('created', 'status_changed', 'source_modified',
                              'comment', 'source_deleted')),
    from_status_id      INTEGER REFERENCES document_record_status (id),
    to_status_id        INTEGER REFERENCES document_record_status (id),
    comment             TEXT,
    attachments         JSONB,                          -- [{"name": "...", "url": "..."}]
    detail              JSONB,                          -- e.g. before/after values of an external change
    created_by          VARCHAR(255) NOT NULL,
    create_time         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- AI judgements (a record may be judged multiple times) ----------

CREATE TABLE ai_judgement (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_record_id  BIGINT NOT NULL REFERENCES document_record (id),
    type_id             INTEGER NOT NULL REFERENCES ai_judgement_type (id),
    urgency_id          INTEGER NOT NULL REFERENCES ai_judgement_urgency (id),
    assignee_role_id    INTEGER NOT NULL REFERENCES assignee_role (id),
    summary             TEXT NOT NULL,
    reason              TEXT NOT NULL,
    model               VARCHAR(100) NOT NULL,
    create_time         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Ticket sync state (one-to-one with records, for the active ticket backend) ----------

CREATE TABLE ticket_sync (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_record_id  BIGINT NOT NULL UNIQUE REFERENCES document_record (id),
    issue_key           VARCHAR(50) UNIQUE,             -- NULL until the ticket is created

    -- Outbound to the ticket system
    push_status         VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (push_status IN ('pending', 'success', 'failed')),
    pushed_hash         CHAR(64),
    pushed_at           TIMESTAMPTZ,
    last_error          TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,

    -- Inbound from the ticket system
    ticket_updated_at     TIMESTAMPTZ,                    -- last ticket update; webhook events older than this are dropped

    create_time         TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_time         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Indexes (PostgreSQL does not index foreign keys automatically) ----------

CREATE INDEX idx_document_document_type        ON document (document_type_id);
CREATE INDEX idx_record_document               ON document_record (document_id);
CREATE INDEX idx_record_status                 ON document_record (status_id);
CREATE INDEX idx_history_record_time           ON document_record_history (document_record_id, create_time);
CREATE INDEX idx_ai_judgement_record_time      ON ai_judgement (document_record_id, create_time);
CREATE INDEX idx_ticket_sync_unfinished          ON ticket_sync (push_status) WHERE push_status <> 'success';
