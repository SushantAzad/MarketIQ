"""Relational financial schema. Financial amounts use exact PostgreSQL NUMERIC."""

from datetime import datetime
from uuid import UUID as PyUUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def identity() -> sa.Column[PyUUID]:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True)


def created() -> sa.Column[datetime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


companies = sa.Table(
    "companies",
    metadata,
    identity(),
    sa.Column("cik", sa.String(10), nullable=False, unique=True),
    sa.Column("legal_name", sa.Text, nullable=False),
    created(),
    sa.CheckConstraint("cik ~ '^[0-9]{10}$'", name="cik_format"),
)
securities = sa.Table(
    "securities",
    metadata,
    identity(),
    sa.Column("company_id", UUID, sa.ForeignKey("companies.id"), nullable=False),
    sa.Column("ticker", sa.String(20), nullable=False, unique=True),
    sa.Column("provider", sa.Text, nullable=False),
    created(),
)
source_objects = sa.Table(
    "source_objects",
    metadata,
    identity(),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("source_url", sa.Text, nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("object_key", sa.Text, nullable=False),
    sa.Column("first_fetched_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("source_timestamp", sa.Text),
    created(),
    sa.UniqueConstraint("source_url", "content_hash"),
    sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="hash_format"),
)
snapshot_observations = sa.Table(
    "snapshot_observations",
    metadata,
    sa.Column("source_id", UUID, sa.ForeignKey("source_objects.id"), primary_key=True),
    sa.Column("observed_at", sa.DateTime(timezone=True), primary_key=True),
)
filings = sa.Table(
    "filings",
    metadata,
    identity(),
    sa.Column("company_id", UUID, sa.ForeignKey("companies.id"), nullable=False),
    sa.Column("accession_number", sa.String(20), nullable=False),
    sa.Column("form_type", sa.String(12), nullable=False),
    sa.Column("filing_date", sa.Date, nullable=False),
    sa.Column("accepted_at", sa.DateTime(timezone=True)),
    sa.Column("report_period", sa.Date),
    sa.Column("source_url", sa.Text, nullable=False),
    sa.Column("metadata_source_id", UUID, sa.ForeignKey("source_objects.id"), nullable=False),
    created(),
    sa.UniqueConstraint("company_id", "accession_number"),
    sa.CheckConstraint(
        "accession_number ~ '^[0-9]{10}-[0-9]{2}-[0-9]{6}$'", name="accession_format"
    ),
)
sa.Index("ix_filings_company_date", filings.c.company_id, filings.c.filing_date)

filing_documents = sa.Table(
    "filing_documents",
    metadata,
    identity(),
    sa.Column("filing_id", UUID, sa.ForeignKey("filings.id"), nullable=False),
    sa.Column("source_id", UUID, sa.ForeignKey("source_objects.id"), nullable=False),
    sa.Column("document_hash", sa.String(64), nullable=False),
    sa.Column("parsed_hash", sa.String(64), nullable=False),
    sa.Column("parser_version", sa.Text, nullable=False),
    created(),
    sa.UniqueConstraint("filing_id", "parsed_hash"),
)
document_chunks = sa.Table(
    "document_chunks",
    metadata,
    identity(),
    sa.Column("document_id", UUID, sa.ForeignKey("filing_documents.id"), nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("section", sa.Text),
    sa.Column("start_offset", sa.Integer, nullable=False),
    sa.Column("end_offset", sa.Integer, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("token_count", sa.Integer, nullable=False),
    sa.Column("chunker_version", sa.Text, nullable=False),
    sa.UniqueConstraint("document_id", "chunker_version", "ordinal"),
    sa.CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="offsets"),
    sa.CheckConstraint("token_count > 0", name="token_count"),
)
embedding_cache = sa.Table(
    "embedding_cache",
    metadata,
    sa.Column("model_key", sa.String(64), primary_key=True),
    sa.Column("content_hash", sa.String(64), primary_key=True),
    sa.Column("vector", JSONB, nullable=False),
)
index_generations = sa.Table(
    "index_generations",
    metadata,
    identity(),
    sa.Column("collection_name", sa.Text, unique=True, nullable=False),
    sa.Column("manifest_hash", sa.String(64), nullable=False),
    sa.Column("model_key", sa.String(64), nullable=False),
    sa.Column("model_name", sa.Text, nullable=False),
    sa.Column("model_revision", sa.String(40), nullable=False),
    sa.Column("dimension", sa.Integer, nullable=False),
    sa.Column("chunk_count", sa.Integer, nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("activated_at", sa.DateTime(timezone=True)),
    created(),
    sa.CheckConstraint("state IN ('building', 'validated', 'active', 'retired')", name="state"),
)
sa.Index(
    "uq_index_generations_active",
    index_generations.c.state,
    unique=True,
    postgresql_where=index_generations.c.state == "active",
)
generation_chunks = sa.Table(
    "generation_chunks",
    metadata,
    sa.Column("generation_id", UUID, sa.ForeignKey("index_generations.id"), primary_key=True),
    sa.Column("chunk_id", UUID, sa.ForeignKey("document_chunks.id"), primary_key=True),
    sa.Column("payload", JSONB, nullable=False),
)

lexical_generations = sa.Table(
    "lexical_generations",
    metadata,
    sa.Column("generation_id", UUID, sa.ForeignKey("index_generations.id"), primary_key=True),
    sa.Column("version", sa.Text, nullable=False),
    sa.Column("corpus_manifest", sa.String(64), nullable=False),
    sa.Column("index_hash", sa.String(64), nullable=False),
    sa.Column("chunk_count", sa.Integer, nullable=False),
)
lexical_chunks = sa.Table(
    "lexical_chunks",
    metadata,
    sa.Column(
        "generation_id", UUID, sa.ForeignKey("lexical_generations.generation_id"), primary_key=True
    ),
    sa.Column("chunk_id", UUID, primary_key=True),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("terms", JSONB, nullable=False),
    sa.Column("length", sa.Integer, nullable=False),
    sa.CheckConstraint("length >= 0", name="length"),
    sa.ForeignKeyConstraint(
        ["generation_id", "chunk_id"],
        ["generation_chunks.generation_id", "generation_chunks.chunk_id"],
    ),
)

workflow_jobs = sa.Table(
    "workflow_jobs",
    metadata,
    identity(),
    sa.Column("version", sa.String(40), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("state", JSONB, nullable=False),
    sa.Column("last_error", sa.String(80)),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    created(),
    sa.CheckConstraint(
        "status IN ('QUEUED','RUNNING','FAILED','COMPLETED','NEEDS_CLARIFICATION')", name="status"
    ),
)

research_runs = sa.Table(
    "research_runs",
    metadata,
    identity(),
    sa.Column("question_hash", sa.String(64), nullable=False),
    sa.Column("filters", JSONB, nullable=False),
    sa.Column("generation_id", UUID, sa.ForeignKey("index_generations.id")),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("prompt_version", sa.Text, nullable=False),
    sa.Column("model_provider", sa.Text, nullable=False),
    sa.Column("model_name", sa.Text),
    sa.Column("retrieval_ms", sa.Float, nullable=False),
    sa.Column("generation_ms", sa.Float, nullable=False),
    sa.Column("response", JSONB, nullable=False),
    created(),
)
research_citations = sa.Table(
    "research_citations",
    metadata,
    sa.Column("run_id", UUID, sa.ForeignKey("research_runs.id"), primary_key=True),
    sa.Column("claim_id", sa.String(12), primary_key=True),
    sa.Column("generation_id", UUID, nullable=False),
    sa.Column("chunk_id", UUID, nullable=False),
    sa.Column("exact_quote", sa.Text, nullable=False),
    sa.Column("quote_hash", sa.String(64), nullable=False),
    sa.Column("start_offset", sa.Integer, nullable=False),
    sa.Column("end_offset", sa.Integer, nullable=False),
    sa.Column("verification_status", sa.Text, nullable=False),
    sa.ForeignKeyConstraint(
        ["generation_id", "chunk_id"],
        ["generation_chunks.generation_id", "generation_chunks.chunk_id"],
    ),
)
financial_statements = sa.Table(
    "financial_statements",
    metadata,
    identity(),
    sa.Column("filing_id", UUID, sa.ForeignKey("filings.id"), nullable=False),
    sa.Column("statement_type", sa.String(24), nullable=False),
    sa.Column("period_start", sa.Date),
    sa.Column("period_end", sa.Date, nullable=False),
    sa.Column("period_basis", sa.String(20), nullable=False),
    sa.Column("unit", sa.String(80), nullable=False),
    sa.Column("context_hash", sa.String(64), nullable=False),
    sa.Column("normalization_version", sa.String(40), nullable=False),
    created(),
    sa.UniqueConstraint("filing_id", "statement_type", "context_hash", "normalization_version"),
    sa.CheckConstraint("period_start IS NULL OR period_start <= period_end", name="period_order"),
)
financial_facts = sa.Table(
    "financial_facts",
    metadata,
    identity(),
    sa.Column("company_id", UUID, sa.ForeignKey("companies.id"), nullable=False),
    sa.Column("filing_id", UUID, sa.ForeignKey("filings.id"), nullable=False),
    sa.Column("statement_id", UUID, sa.ForeignKey("financial_statements.id"), nullable=False),
    sa.Column("taxonomy", sa.String(80), nullable=False),
    sa.Column("concept", sa.String(256), nullable=False),
    sa.Column("value", sa.Numeric(), nullable=False),
    sa.Column("unit", sa.String(80), nullable=False),
    sa.Column("period_start", sa.Date),
    sa.Column("period_end", sa.Date, nullable=False),
    sa.Column("period_basis", sa.String(20), nullable=False),
    # SEC fy/fp describe the submitting filing, not necessarily the fact's own period.
    sa.Column("filing_fiscal_year", sa.Integer),
    sa.Column("filing_fiscal_period", sa.String(12)),
    sa.Column("reported_available_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("availability_basis", sa.String(32), nullable=False),
    sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
    sa.Column("normalization_version", sa.String(40), nullable=False),
    created(),
    sa.CheckConstraint("period_start IS NULL OR period_start <= period_end", name="period_order"),
    sa.CheckConstraint(
        "value NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)", name="finite"
    ),
)
sa.Index(
    "ix_facts_lookup",
    financial_facts.c.company_id,
    financial_facts.c.concept,
    financial_facts.c.period_basis,
    financial_facts.c.period_end,
)
fact_sources = sa.Table(
    "fact_sources",
    metadata,
    sa.Column("fact_id", UUID, sa.ForeignKey("financial_facts.id"), primary_key=True),
    sa.Column("source_id", UUID, sa.ForeignKey("source_objects.id"), primary_key=True),
    sa.Column("source_locator", sa.Text, primary_key=True),
)
normalization_runs = sa.Table(
    "normalization_runs",
    metadata,
    identity(),
    sa.Column("company_id", UUID, sa.ForeignKey("companies.id"), nullable=False),
    sa.Column("source_id", UUID, sa.ForeignKey("source_objects.id"), nullable=False),
    sa.Column("normalization_version", sa.String(40), nullable=False),
    sa.Column("accepted_count", sa.Integer, nullable=False),
    sa.Column("rejected_count", sa.Integer, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    created(),
    sa.UniqueConstraint("source_id", "normalization_version"),
    sa.CheckConstraint("status IN ('complete','partial')", name="status_values"),
)
rejected_facts = sa.Table(
    "rejected_facts",
    metadata,
    identity(),
    sa.Column("run_id", UUID, sa.ForeignKey("normalization_runs.id"), nullable=False),
    sa.Column("source_locator", sa.Text, nullable=False),
    sa.Column("reason", sa.String(120), nullable=False),
    sa.UniqueConstraint("run_id", "source_locator"),
)
financial_metrics = sa.Table(
    "financial_metrics",
    metadata,
    identity(),
    sa.Column("company_id", UUID, sa.ForeignKey("companies.id"), nullable=False),
    sa.Column("metric_name", sa.String(80), nullable=False),
    sa.Column("period_start", sa.Date),
    sa.Column("period_end", sa.Date, nullable=False),
    sa.Column("period_basis", sa.String(20), nullable=False),
    sa.Column("value", sa.Numeric()),
    sa.Column("unit", sa.String(80), nullable=False),
    sa.Column("formula_version", sa.String(40), nullable=False),
    sa.Column("input_set_hash", sa.String(64), nullable=False),
    sa.Column("unavailable_reason", sa.Text),
    sa.Column("response", JSONB),
    created(),
    sa.UniqueConstraint(
        "company_id",
        "metric_name",
        "period_end",
        "period_basis",
        "formula_version",
        "input_set_hash",
    ),
    sa.CheckConstraint(
        "(value IS NULL) = (unavailable_reason IS NOT NULL)", name="value_or_reason"
    ),
)
metric_inputs = sa.Table(
    "metric_inputs",
    metadata,
    sa.Column("metric_id", UUID, sa.ForeignKey("financial_metrics.id"), primary_key=True),
    sa.Column("operand_name", sa.String(80), primary_key=True),
    sa.Column("fact_id", UUID, sa.ForeignKey("financial_facts.id")),
    sa.Column("input_metric_id", UUID, sa.ForeignKey("financial_metrics.id")),
    sa.CheckConstraint("(fact_id IS NULL) <> (input_metric_id IS NULL)", name="one_input"),
    sa.CheckConstraint("metric_id <> input_metric_id", name="not_self"),
)
data_fetch_logs = sa.Table(
    "data_fetch_logs",
    metadata,
    identity(),
    sa.Column("journal_key", sa.String(64), nullable=False, unique=True),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("source_url", sa.Text, nullable=False),
    sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("outcome", sa.String(16), nullable=False),
    sa.Column("http_status", sa.Integer),
    sa.Column("error", sa.Text),
    sa.Column("latency_ms", sa.Float, nullable=False),
)
data_source_state = sa.Table(
    "data_source_state",
    metadata,
    sa.Column("source_url", sa.Text, primary_key=True),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("last_attempted_fetch", sa.DateTime(timezone=True)),
    sa.Column("last_successful_fetch", sa.DateTime(timezone=True)),
    sa.Column("source_timestamp", sa.Text),
    sa.Column("data_version", sa.String(64)),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("error_message", sa.Text),
)
