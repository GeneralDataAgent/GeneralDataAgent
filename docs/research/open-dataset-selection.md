# Open dataset selection for real-system validation

## Decision summary

Use the January 2025 NYC Taxi and Limousine Commission green-taxi trip records for the first real
validation dataset. Keep UCI Online Retail II as the next commerce-oriented candidate. Do not use
Chinook as evidence that the system has been tested on real transactions.

## Comparison

| Dataset | Reality and provenance | License/access | Engineering fit | Decision |
|---|---|---|---|---|
| [NYC TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page) | Vendor-reported operational taxi trips published by the city, with official dictionaries and monthly files | Publicly downloadable official data | Native Parquet; timestamps, geography, fares, distances, passenger and payment fields; January green data is small enough for local CI-style acceptance | Selected |
| [UCI Online Retail II](https://archive.ics.uci.edu/dataset/502/online+retail+ii) | Two years of real UK online-retail transactions | CC BY 4.0 | Strong for revenue, retention, cancellation, segmentation and forecasting; source is an Excel workbook and requires heavier normalization | Phase-two candidate |
| [Chinook](https://github.com/lerocha/chinook-database) | Media metadata partly reflects an iTunes library, but customers are fictitious and sales are randomly generated | MIT | Excellent relational demo database with many SQL dialects | Rejected as the primary “real data” proof |

## Why TLC is first

- It exercises a genuine raw-to-curated data-engineering step rather than loading a prebuilt demo
  database.
- Its official zone lookup allows semantic enrichment and grouped business metrics.
- A monthly snapshot makes input provenance, hashing and reruns manageable.
- The schema supports later data-quality, anomaly, forecasting and trip-duration modeling work.

## Source limitations

The TLC states that the records are supplied by authorized technology providers and that it cannot
guarantee their accuracy or completeness. The current pipeline therefore treats them as reported
operational records, not ground truth. The selected metric excludes rows whose pickup timestamp is
outside January 2025 even when those rows occur in the January source file.

## Reproducibility contract

The preparation command records:

- exact source URLs;
- SHA-256 for the Parquet and zone lookup;
- source period and curated row count;
- preparation timestamp.

Downloaded and generated data remain outside Git. Code, metric definitions, tests and this source
ledger remain versioned.
