import csv
import sqlite3

import duckdb

from generaldataagent.datasets import build_tlc_metric_catalog, prepare_tlc_database


def test_prepares_curated_sqlite_snapshot(tmp_path) -> None:
    parquet = tmp_path / "trips.parquet"
    zones = tmp_path / "zones.csv"
    database = tmp_path / "tlc.sqlite"

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE trips AS SELECT
                TIMESTAMP '2025-01-02 10:00:00' AS lpep_pickup_datetime,
                TIMESTAMP '2025-01-02 10:15:00' AS lpep_dropoff_datetime,
                7 AS PULocationID,
                8 AS DOLocationID,
                1.0 AS passenger_count,
                2.5 AS trip_distance,
                12.0 AS fare_amount,
                15.5 AS total_amount,
                1 AS payment_type
            """
        )
        connection.execute("COPY trips TO ? (FORMAT PARQUET)", [str(parquet)])
    finally:
        connection.close()

    with zones.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["LocationID", "Borough", "Zone", "service_zone"])
        writer.writerow([7, "Queens", "Astoria", "Boro Zone"])
        writer.writerow([8, "Queens", "Astoria Park", "Boro Zone"])

    manifest = prepare_tlc_database(parquet, zones, database)
    assert manifest.row_count == 1
    assert len(manifest.trip_sha256) == 64

    with sqlite3.connect(database) as target:
        row = target.execute(
            "SELECT pickup_borough, pickup_zone, trip_distance FROM green_trips"
        ).fetchone()
    assert row == ("Queens", "Astoria", 2.5)


def test_tlc_catalog_exposes_eight_certified_metrics() -> None:
    catalog = build_tlc_metric_catalog()
    assert {metric.name for metric in catalog.list()} == {
        "average_charged_amount",
        "average_fare_amount",
        "average_passenger_count",
        "average_trip_distance",
        "total_charged_amount",
        "total_fare_amount",
        "total_trip_distance",
        "trip_count",
    }
