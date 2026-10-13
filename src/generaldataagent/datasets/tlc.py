"""Prepare a curated SQLite database from official NYC TLC open data."""

import csv
import hashlib
import json
import sqlite3
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import duckdb
from pydantic import BaseModel, ConfigDict

from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition

GREEN_TRIPS_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data/green_tripdata_2025-01.parquet"
ZONE_LOOKUP_URL = "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv"


class TlcDatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "NYC Taxi and Limousine Commission"
    period: str = "2025-01"
    trip_url: str
    zone_url: str
    trip_sha256: str
    zone_sha256: str
    row_count: int
    prepared_at: datetime


def build_tlc_metric_catalog() -> InMemoryMetricCatalog:
    """Return the versioned semantic definitions for the curated TLC snapshot."""

    return InMemoryMetricCatalog(
        [
            MetricDefinition(
                name="trip_count",
                version="v1",
                description="Number of reported NYC TLC green-taxi trips",
                aliases=("trip count", "number of trips", "trips", "行程数", "订单量"),
                data_domain="mobility.tlc",
                relation="green_trips",
                expression="COUNT(*)",
                time_column="pickup_at",
                allowed_dimensions=("pickup_borough",),
                owner="nyc-tlc",
            ),
            MetricDefinition(
                name="total_fare_amount",
                version="v1",
                description="Sum of reported fare amount before extras and taxes",
                aliases=("total fare", "fare", "fare amount", "总车费", "车费"),
                data_domain="mobility.tlc",
                relation="green_trips",
                expression="ROUND(SUM(fare_amount), 2)",
                time_column="pickup_at",
                allowed_dimensions=("pickup_borough",),
                unit="USD",
                owner="nyc-tlc",
            ),
            MetricDefinition(
                name="average_fare_amount",
                version="v1",
                description="Average reported fare amount before extras and taxes",
                aliases=("average fare", "fare", "avg fare", "平均车费", "车费"),
                data_domain="mobility.tlc",
                relation="green_trips",
                expression="ROUND(AVG(fare_amount), 2)",
                time_column="pickup_at",
                allowed_dimensions=("pickup_borough",),
                unit="USD",
                owner="nyc-tlc",
            ),
            MetricDefinition(
                name="total_charged_amount",
                version="v1",
                description="Sum of reported total charged amount including extras and taxes",
                aliases=("total charged", "charged amount", "total amount", "总金额", "总费用"),
                data_domain="mobility.tlc",
                relation="green_trips",
                expression="ROUND(SUM(total_amount), 2)",
                time_column="pickup_at",
                allowed_dimensions=("pickup_borough",),
                unit="USD",
                owner="nyc-tlc",
            ),
            MetricDefinition(
                name="average_charged_amount",
                version="v1",
                description="Average reported total charged amount including extras and taxes",
                aliases=(
                    "average charged",
                    "charged amount",
                    "average total amount",
                    "平均总金额",
                    "总费用",
                ),
                data_domain="mobility.tlc",
                relation="green_trips",
                expression="ROUND(AVG(total_amount), 2)",
                time_column="pickup_at",
                allowed_dimensions=("pickup_borough",),
                unit="USD",
                owner="nyc-tlc",
            ),
            MetricDefinition(
                name="total_trip_distance",
                version="v1",
                description="Sum of reported trip distance",
                aliases=("total distance", "trip distance", "总里程", "行程里程"),
                data_domain="mobility.tlc",
                relation="green_trips",
                expression="ROUND(SUM(trip_distance), 2)",
                time_column="pickup_at",
                allowed_dimensions=("pickup_borough",),
                unit="mile",
                owner="nyc-tlc",
            ),
            MetricDefinition(
                name="average_trip_distance",
                version="v1",
                description="Average reported trip distance",
                aliases=(
                    "average distance",
                    "trip distance",
                    "avg distance",
                    "平均里程",
                    "行程里程",
                ),
                data_domain="mobility.tlc",
                relation="green_trips",
                expression="ROUND(AVG(trip_distance), 2)",
                time_column="pickup_at",
                allowed_dimensions=("pickup_borough",),
                unit="mile",
                owner="nyc-tlc",
            ),
            MetricDefinition(
                name="average_passenger_count",
                version="v1",
                description="Average reported passenger count for records that report passengers",
                aliases=("average passengers", "passenger count", "平均乘客数", "乘客数"),
                data_domain="mobility.tlc",
                relation="green_trips",
                expression="ROUND(AVG(passenger_count), 2)",
                time_column="pickup_at",
                allowed_dimensions=("pickup_borough",),
                unit="passenger",
                owner="nyc-tlc",
            ),
        ]
    )


def download_tlc_sources(target_dir: Path) -> tuple[Path, Path]:
    """Download the pinned monthly source files if they are not already present."""

    target_dir.mkdir(parents=True, exist_ok=True)
    trips = target_dir / "green_tripdata_2025-01.parquet"
    zones = target_dir / "taxi_zone_lookup.csv"
    _download_if_missing(GREEN_TRIPS_URL, trips)
    _download_if_missing(ZONE_LOOKUP_URL, zones)
    return trips, zones


def prepare_tlc_database(
    trip_parquet: Path,
    zone_csv: Path,
    database_path: Path,
    *,
    manifest_path: Path | None = None,
) -> TlcDatasetManifest:
    """Create an analysis-ready SQLite snapshot with provenance and hashes."""

    trip_parquet = trip_parquet.resolve(strict=True)
    zone_csv = zone_csv.resolve(strict=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database_path.unlink(missing_ok=True)

    zones = _read_zones(zone_csv)
    source = duckdb.connect(":memory:")
    try:
        cursor = source.execute(
            """
            SELECT
                lpep_pickup_datetime,
                lpep_dropoff_datetime,
                PULocationID,
                DOLocationID,
                passenger_count,
                trip_distance,
                fare_amount,
                total_amount,
                payment_type
            FROM read_parquet(?)
            """,
            [str(trip_parquet)],
        )
        with sqlite3.connect(database_path) as target:
            target.execute(
                """
                CREATE TABLE green_trips (
                    pickup_at TEXT NOT NULL,
                    dropoff_at TEXT NOT NULL,
                    pickup_location_id INTEGER NOT NULL,
                    dropoff_location_id INTEGER NOT NULL,
                    pickup_borough TEXT,
                    pickup_zone TEXT,
                    passenger_count REAL,
                    trip_distance REAL NOT NULL,
                    fare_amount REAL NOT NULL,
                    total_amount REAL NOT NULL,
                    payment_type INTEGER
                )
                """
            )
            row_count = 0
            while batch := cursor.fetchmany(5_000):
                rows = []
                for row in batch:
                    pickup = zones.get(int(row[2]), (None, None))
                    rows.append(
                        (
                            row[0].isoformat(),
                            row[1].isoformat(),
                            int(row[2]),
                            int(row[3]),
                            pickup[0],
                            pickup[1],
                            _optional_float(row[4]),
                            float(row[5]),
                            float(row[6]),
                            float(row[7]),
                            int(row[8]) if row[8] is not None else None,
                        )
                    )
                target.executemany(
                    "INSERT INTO green_trips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
                )
                row_count += len(rows)
            target.execute("CREATE INDEX idx_green_trips_pickup ON green_trips(pickup_at)")
            target.execute("CREATE INDEX idx_green_trips_borough ON green_trips(pickup_borough)")
    finally:
        source.close()

    manifest = TlcDatasetManifest(
        trip_url=GREEN_TRIPS_URL,
        zone_url=ZONE_LOOKUP_URL,
        trip_sha256=_sha256(trip_parquet),
        zone_sha256=_sha256(zone_csv),
        row_count=row_count,
        prepared_at=datetime.now(UTC),
    )
    output_manifest = manifest_path or database_path.with_suffix(".manifest.json")
    output_manifest.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _read_zones(path: Path) -> dict[int, tuple[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            int(row["LocationID"]): (row["Borough"], row["Zone"]) for row in csv.DictReader(handle)
        }


def _download_if_missing(url: str, destination: Path) -> None:
    if destination.exists():
        return
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)
