"""NASA FIRMS active fire / thermal anomaly detections.

The bounding box is always computed from the municipality geometry; no
coordinates are hard-coded. Multiple satellite products are ingested so the
observation-gap view can compare what each one saw.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from shapely.geometry import Point

from firewatch.config import settings
from firewatch.sources.base import (
    DatasetManifest,
    DataStatus,
    IngestContext,
    NormalizedFeature,
    NormalizedFeatures,
    RawDataset,
    SourceAdapter,
    content_hash,
)
from firewatch.sources.http import SourceUnavailable, get_text

AREA_CSV = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
MAX_DAY_RANGE = 10


class NasaFirmsAdapter(SourceAdapter):
    adapter_id = "nasa_firms"
    accumulate = True
    aging_after = timedelta(hours=12)
    stale_after = timedelta(days=3)

    def discover(self, ctx: IngestContext) -> DatasetManifest:
        manifest = DatasetManifest(
            source_id=self.source_id,
            title="NASA FIRMS thermal anomalies",
            source_url="https://firms.modaps.eosdis.nasa.gov/api/area/",
            licence=self.params.get("licence"),
            licence_url=self.params.get("licence_url"),
            attribution=self.params.get("attribution", "NASA FIRMS (LANCE / MODAPS)"),
            temporal_resolution="Several satellite overpasses per day",
            spatial_resolution="375 m (VIIRS) / 1 km (MODIS) nominal footprint",
            caveats=[
                "A FIRMS detection is a thermal anomaly, not a confirmed wildfire. "
                "Industrial heat, flares and agricultural burning all register.",
                "Detection depends on satellite overpass timing and cloud cover. A "
                "fire can burn for hours between usable observations.",
            ],
        )
        if not settings.firms_map_key:
            manifest.available = False
            manifest.message = (
                "No FIRMS_MAP_KEY is configured, so FIRMS detections are not being "
                "retrieved. This is reported as unavailable rather than as zero "
                "detections, because those mean very different things. A free key is "
                "available at https://firms.modaps.eosdis.nasa.gov/api/map_key/"
            )
        return manifest

    def _products(self) -> list[str]:
        products = self.params.get("products") or ["VIIRS_SNPP_NRT"]
        return [str(p) for p in products]

    def fetch(self, ctx: IngestContext) -> RawDataset:
        key = settings.firms_map_key
        if not key:
            raise SourceUnavailable("FIRMS_MAP_KEY is not configured.")

        west, south, east, north = ctx.bounds
        bbox = f"{west:.4f},{south:.4f},{east:.4f},{north:.4f}"
        day_range = min(int(self.params.get("day_range", 7)), MAX_DAY_RANGE)

        rows: list[dict] = []
        notes: list[str] = []
        urls: list[str] = []
        failures: list[str] = []

        for product in self._products():
            url = f"{AREA_CSV}/{key}/{product}/{bbox}/{day_range}"
            if ctx.as_of:
                # The area API accepts a start date for archive queries.
                url = f"{url}/{ctx.as_of.strftime('%Y-%m-%d')}"
            # Never let the key reach a log or a provenance record.
            urls.append(url.replace(key, "<MAP_KEY>"))
            try:
                text = get_text(url, retries=1)
            except Exception as exc:
                failures.append(f"{product}: {str(exc).replace(key, '<MAP_KEY>')}")
                continue

            if text.lstrip().lower().startswith(("<", "invalid", "error")):
                failures.append(f"{product}: unexpected response {text[:120].strip()}")
                continue

            product_rows = list(csv.DictReader(io.StringIO(text)))
            for row in product_rows:
                row["_product"] = product
            rows.extend(product_rows)
            notes.append(f"{product}: {len(product_rows)} detections.")

        if failures and not rows:
            raise SourceUnavailable("; ".join(failures))
        notes.extend(f"Failed: {f}" for f in failures)

        return RawDataset(
            payload={"rows": rows},
            request_url=" ".join(urls),
            content_hash=content_hash(rows),
            notes=notes,
        )

    def normalize(self, raw: RawDataset, ctx: IngestContext) -> NormalizedFeatures:
        kind = self.feature_kind or "satellite_hotspot"
        out: list[NormalizedFeature] = []
        bad = 0

        for row in raw.payload.get("rows", []):
            try:
                lat = float(row["latitude"])
                lon = float(row["longitude"])
            except (KeyError, TypeError, ValueError):
                bad += 1
                continue

            observed = _parse_acq(row.get("acq_date"), row.get("acq_time"))
            product = row.get("_product", "FIRMS")
            satellite = row.get("satellite", "")
            record_id = (
                f"{product}:{row.get('acq_date')}:{row.get('acq_time')}:"
                f"{lat:.5f}:{lon:.5f}:{satellite}"
            )

            props = {k: v for k, v in row.items() if not k.startswith("_")}
            props["product"] = product
            props["source"] = "NASA FIRMS"
            # FIRMS 'confidence' is the algorithm's own detection confidence,
            # not a statement about whether a wildfire exists.
            props["confidence_meaning"] = (
                "FIRMS detection confidence for the thermal anomaly algorithm; "
                "not a probability that a wildfire is present."
            )

            out.append(
                NormalizedFeature(
                    source_record_id=record_id,
                    feature_kind=kind,
                    geometry=Point(lon, lat),
                    properties=props,
                    observed_at=observed,
                )
            )

        notes = [f"{bad} rows lacked usable coordinates."] if bad else []
        return NormalizedFeatures(features=out, notes=notes)

    def status_for(self, latest_observed_at, raw, report):
        if report.accepted == 0:
            # Zero detections is the normal, good case. It must not read as a
            # failure, but it also must not imply nothing is burning.
            return DataStatus.CURRENT, (
                "No thermal anomalies were detected in the area during the query "
                "window. This does not establish that no fire is present; small, "
                "cool or cloud-obscured fires are routinely missed."
            )
        return super().status_for(latest_observed_at, raw, report)


def _parse_acq(acq_date: str | None, acq_time: str | None) -> datetime | None:
    if not acq_date:
        return None
    try:
        base = datetime.strptime(acq_date.strip(), "%Y-%m-%d")
    except ValueError:
        return None
    minutes = 0
    if acq_time:
        digits = "".join(ch for ch in str(acq_time) if ch.isdigit()).zfill(4)
        try:
            minutes = int(digits[:2]) * 60 + int(digits[2:4])
        except ValueError:
            minutes = 0
    return (base + timedelta(minutes=minutes)).replace(tzinfo=timezone.utc)
