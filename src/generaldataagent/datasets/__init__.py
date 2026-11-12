"""Reproducible open-data preparation pipelines."""

from generaldataagent.datasets.tlc import (
    TlcDatasetManifest,
    build_tlc_metric_catalog,
    download_tlc_sources,
    prepare_tlc_database,
)
from generaldataagent.datasets.xlsx_preflight import (
    XlsxPreflightError,
    XlsxPreflightReport,
    preflight_xlsx,
)
from generaldataagent.datasets.xlsx_preview import (
    XlsxPreviewData,
    XlsxPreviewError,
    XlsxPreviewResult,
    preview_xlsx,
)

__all__ = [
    "TlcDatasetManifest",
    "XlsxPreflightError",
    "XlsxPreflightReport",
    "XlsxPreviewData",
    "XlsxPreviewError",
    "XlsxPreviewResult",
    "build_tlc_metric_catalog",
    "download_tlc_sources",
    "preflight_xlsx",
    "preview_xlsx",
    "prepare_tlc_database",
]
