-- =========================================================
-- Run after schema.sql: automatic update_time + seed constant data
-- =========================================================

CREATE OR REPLACE FUNCTION set_update_time() RETURNS trigger AS $$
BEGIN
    NEW.update_time := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_document_update_time
    BEFORE UPDATE ON document
    FOR EACH ROW EXECUTE FUNCTION set_update_time();

CREATE TRIGGER trg_document_record_update_time
    BEFORE UPDATE ON document_record
    FOR EACH ROW EXECUTE FUNCTION set_update_time();

CREATE TRIGGER trg_ticket_sync_update_time
    BEFORE UPDATE ON ticket_sync
    FOR EACH ROW EXECUTE FUNCTION set_update_time();

-- ---------- Seed constant data (adjust as needed) ----------

INSERT INTO document_type (name) VALUES ('google_sheets');

INSERT INTO document_record_status (status_name, is_terminal) VALUES
    ('open',        FALSE),
    ('in_progress', FALSE),
    ('done',        TRUE);

INSERT INTO ai_judgement_type (type_name) VALUES
    ('bug'), ('data_issue'), ('access_request'), ('question');

INSERT INTO ai_judgement_urgency (urgency_level_name) VALUES
    ('critical'), ('high'), ('medium'), ('low');

INSERT INTO assignee_role (role_name) VALUES
    ('backend'), ('frontend'), ('ops'), ('support');
