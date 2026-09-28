# Application boundaries

`core/` contains configuration and structured logging. `providers/sec/` contains SEC transport/discovery models. `ingestion/` provides parsing and the local progress journal. `database/` defines PostgreSQL persistence, journal synchronization, and CLI commands. `services/` normalizes companyfacts and resolves sourced period-aware reads. Retrieval/calculation/workflow/ML modules and public API endpoints remain later phases.
