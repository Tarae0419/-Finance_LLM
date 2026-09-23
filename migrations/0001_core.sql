CREATE TABLE source_document (
    source_id uuid PRIMARY KEY,
    official_id text NOT NULL,
    title text NOT NULL,
    document_type text NOT NULL CHECK (document_type IN ('law', 'enforcement_decree', 'supervisory_regulation', 'official_faq', 'official_interpretation')),
    authority text NOT NULL,
    source_url text NOT NULL,
    UNIQUE (document_type, official_id)
);

CREATE TABLE document_version (
    version_id uuid PRIMARY KEY,
    source_id uuid NOT NULL REFERENCES source_document,
    official_version_id text NOT NULL,
    promulgated_at date,
    effective_from date,
    collected_at timestamptz NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    raw_path text NOT NULL,
    source_url text NOT NULL,
    attachments jsonb NOT NULL DEFAULT '[]',
    UNIQUE (source_id, official_version_id, content_hash)
);

CREATE TABLE provision (
    provision_id uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES document_version,
    provision_kind text NOT NULL,
    article text,
    paragraph text,
    item text,
    text text NOT NULL CHECK (length(text) > 0),
    source_offsets jsonb NOT NULL,
    parent_id uuid,
    UNIQUE (provision_id, version_id),
    FOREIGN KEY (parent_id, version_id) REFERENCES provision (provision_id, version_id),
    CHECK (parent_id IS NULL OR parent_id <> provision_id)
);
CREATE INDEX provision_version_article_idx ON provision (version_id, article);

CREATE TABLE applicability (
    provision_id uuid PRIMARY KEY REFERENCES provision,
    valid_from date,
    valid_to_exclusive date,
    transition_refs jsonb NOT NULL DEFAULT '[]',
    temporal_review_status text NOT NULL DEFAULT 'unreviewed' CHECK (temporal_review_status IN ('unreviewed', 'verified', 'needs_review')),
    reviewer text,
    reviewed_at timestamptz,
    CHECK (valid_to_exclusive IS NULL OR (valid_from IS NOT NULL AND valid_to_exclusive > valid_from)),
    CHECK (temporal_review_status <> 'verified' OR (valid_from IS NOT NULL AND reviewer IS NOT NULL AND reviewed_at IS NOT NULL))
);

CREATE TABLE provision_relation (
    relation_id uuid PRIMARY KEY,
    from_id uuid NOT NULL REFERENCES provision (provision_id),
    to_id uuid REFERENCES provision (provision_id),
    relation_type text NOT NULL,
    target_locator text NOT NULL,
    verified boolean NOT NULL DEFAULT false,
    CHECK (NOT verified OR to_id IS NOT NULL)
);

CREATE TABLE corpus_snapshot (
    snapshot_id uuid PRIMARY KEY,
    coverage jsonb NOT NULL DEFAULT '{}',
    last_success_at timestamptz,
    activated_at timestamptz,
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'verified', 'failed'))
);
CREATE TABLE snapshot_version (
    snapshot_id uuid NOT NULL REFERENCES corpus_snapshot,
    version_id uuid NOT NULL REFERENCES document_version,
    PRIMARY KEY (snapshot_id, version_id)
);
CREATE TABLE corpus_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    snapshot_id uuid NOT NULL REFERENCES corpus_snapshot
);

CREATE TABLE model_release (
    release_id uuid PRIMARY KEY,
    base_revision text NOT NULL,
    adapter_id text,
    training_dataset_id text,
    eval_report_id text,
    config_ref text
);

CREATE TABLE query_trace (
    request_id uuid PRIMARY KEY,
    as_of_date date NOT NULL,
    snapshot_id uuid REFERENCES corpus_snapshot,
    release_id uuid REFERENCES model_release,
    evidence_ids uuid[] NOT NULL DEFAULT '{}',
    status text NOT NULL CHECK (status IN ('answered', 'needs_clarification', 'insufficient_evidence', 'out_of_scope', 'system_error')),
    latency_ms integer NOT NULL CHECK (latency_ms >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
