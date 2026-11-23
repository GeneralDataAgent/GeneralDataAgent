# GeneralDataAgent

[![CI](https://github.com/GeneralDataAgent/GeneralDataAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/GeneralDataAgent/GeneralDataAgent/actions/workflows/ci.yml)

GeneralDataAgent is a governed multi-agent foundation for data analytics, data engineering, and
data science. Specialists operate through versioned contracts, scoped tools, deterministic
validation, and evidence-bound delivery.

## Quick start

Run from this repository root with Python 3.12 and `uv`:

```bash
uv sync
uv run python examples/metric_demo.py
```

The metric demo uses local synthetic data. Follow the [usage guide](docs/guides/usage.md) for API
startup, data-science and data-engineering examples, provider configuration, and evaluation commands.

## Documentation

- [System documentation](docs/README.md): English architecture, ADRs, usage, and operations.
- [System architecture](docs/architecture/system-architecture.md).
- [Capability catalog](docs/architecture/capability-catalog.md): implementation entry points and enablement boundaries.
- [Acceptance report format](docs/operations/acceptance-report-format.md): how to record evidence and qualification decisions.
