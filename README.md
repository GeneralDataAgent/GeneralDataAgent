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
- [Workspace planning and progress](../docs/README.md): phase plans, development status, and acceptance records.
- [Validation records](../docs/validation/README.md): historical runs and machine-readable evidence.
