-- Revisions and review events are append-only. Status belongs to the exact revision.
CREATE TABLE dataset_group (
    group_id text PRIMARY KEY,
    split text NOT NULL CHECK (split IN ('train', 'dev', 'test')),
    UNIQUE (group_id, split)
);

CREATE TABLE example_revision (
    example_id text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    dataset_id text NOT NULL,
    group_id text NOT NULL REFERENCES dataset_group,
    snapshot_id uuid NOT NULL REFERENCES corpus_snapshot,
    content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (example_id, revision),
    UNIQUE (example_id, revision, content_hash)
);
CREATE TABLE example_evidence (
    example_id text NOT NULL,
    revision integer NOT NULL,
    provision_id uuid NOT NULL REFERENCES provision,
    quote text NOT NULL CHECK (length(quote) > 0),
    PRIMARY KEY (example_id, revision, provision_id),
    FOREIGN KEY (example_id, revision) REFERENCES example_revision
);

CREATE TABLE review_log (
    review_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    example_id text NOT NULL,
    revision integer NOT NULL,
    content_hash text NOT NULL,
    reviewer text NOT NULL CHECK (length(trim(reviewer)) > 0),
    reviewer_role text NOT NULL CHECK (reviewer_role IN ('developer', 'external')),
    reviewer_role_detail text NOT NULL CHECK (length(trim(reviewer_role_detail)) > 0),
    external_review boolean GENERATED ALWAYS AS (reviewer_role = 'external') STORED,
    review_date timestamptz NOT NULL,
    method text NOT NULL CHECK (length(trim(method)) > 0),
    human_attested boolean NOT NULL CHECK (human_attested),
    existence text NOT NULL CHECK (existence IN ('pass', 'fail', 'uncertain', 'pending')),
    meaning text NOT NULL CHECK (meaning IN ('pass', 'fail', 'uncertain', 'pending')),
    temporal text NOT NULL CHECK (temporal IN ('pass', 'fail', 'uncertain', 'pending')),
    completeness text NOT NULL CHECK (completeness IN ('pass', 'fail', 'uncertain', 'pending')),
    reasons jsonb NOT NULL CHECK (jsonb_typeof(reasons) = 'object'),
    official_cross_checks jsonb NOT NULL DEFAULT '[]'
        CHECK (jsonb_typeof(official_cross_checks) = 'array'),
    excluded_reason text CHECK (excluded_reason IS NULL OR length(trim(excluded_reason)) > 0),
    duration_seconds integer NOT NULL CHECK (duration_seconds >= 0),
    result text GENERATED ALWAYS AS (
        CASE WHEN excluded_reason IS NOT NULL THEN 'excluded'
             WHEN existence = 'pass' AND meaning = 'pass' AND temporal = 'pass'
                  AND completeness = 'pass'
                  AND (reviewer_role = 'external' OR jsonb_array_length(official_cross_checks) > 0)
             THEN 'reviewed' ELSE 'needs_review' END
    ) STORED,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (example_id, revision, content_hash)
        REFERENCES example_revision (example_id, revision, content_hash)
);

CREATE FUNCTION reject_review_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Append-only data: create a new revision or review event';
END;
$$;
CREATE TRIGGER immutable_example BEFORE UPDATE OR DELETE ON example_revision
    FOR EACH ROW EXECUTE FUNCTION reject_review_history_mutation();
CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON example_evidence
    FOR EACH ROW EXECUTE FUNCTION reject_review_history_mutation();
CREATE TRIGGER immutable_review BEFORE UPDATE OR DELETE ON review_log
    FOR EACH ROW EXECUTE FUNCTION reject_review_history_mutation();
CREATE TRIGGER immutable_group BEFORE UPDATE OR DELETE ON dataset_group
    FOR EACH ROW EXECUTE FUNCTION reject_review_history_mutation();

CREATE VIEW example_review_state AS
SELECT e.*, g.split, COALESCE(r.result, 'draft') AS review_status,
       COALESCE(r.external_review, false) AS external_review, r.review_id
FROM example_revision e JOIN dataset_group g USING (group_id)
LEFT JOIN LATERAL (
    SELECT result, external_review, review_id FROM review_log r
    WHERE r.example_id = e.example_id AND r.revision = e.revision
    ORDER BY review_id DESC LIMIT 1
) r ON true;

CREATE VIEW current_example AS
SELECT DISTINCT ON (example_id) * FROM example_review_state
ORDER BY example_id, revision DESC;
