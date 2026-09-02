"""Disk cache for municipality cell GeoJSON.

Building ~8k hex polygons in PostGIS on every map load is too heavy for a
1 GB VPS. The cache is written after scoring and read on subsequent requests.
"""

from __future__ import annotations

import json
from pathlib import Path

from firewatch.config import settings


def cells_cache_path(
    municipality_id: str,
    value: str,
    as_of_date: str,
    *,
    within_boundary: bool = True,
    min_overall_priority: float | None = None,
) -> Path:
    key = (
        f"{municipality_id}_{value}_{as_of_date}"
        f"_{'inside' if within_boundary else 'all'}"
    )
    if min_overall_priority is not None:
        key += f"_minop{min_overall_priority:g}"
    return settings.firewatch_cache_dir / f"cells_{key}.json"


def read_cells_cache(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_cells_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")))


def invalidate_municipality_caches(municipality_id: str) -> None:
    pattern = f"cells_{municipality_id}_*.json"
    for path in settings.firewatch_cache_dir.glob(pattern):
        try:
            path.unlink()
        except OSError:
            pass
