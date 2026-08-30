"""Canadian FBP fuel types, and what we are willing to infer from them.

The CWFIS 100 m national fuel grid is the authoritative public classification
for fire behaviour in Canada. A cell of that grid is a fuel *type*, not a fuel
inventory: it does not carry stand age, crown closure, surface load or ladder
fuels. Those remain unknown.

Two quantities are derived from the type, and both are typical-for-class rather
than measured:

* a spread factor, ranking how readily that type carries fire relative to the
  other FBP types;
* a canopy screening height, used by the sightline engine so a forested draw is
  not treated as open air.

Both are disclosed as inferences. Substituting measured canopy (LiDAR) would
change the observation-gap numbers; substituting a local fuel inventory would
change the spread numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyproj import Transformer

import numpy as np

NODATA = -9999


@dataclass(frozen=True)
class FuelType:
    code: int
    fbp_class: str
    label: str
    #: Relative fire-behaviour potential in 0..1. Not a rate of spread.
    spread_factor: float
    #: Typical stand height in metres. Grass and non-fuel are ~0.
    canopy_height_m: float
    is_fuel: bool


#: Codes and labels from the CWFIS SLD for ``cffdrs_fbp_fuel_types_100m``
#: (2024 national grid). Mixedwood codes encode percent conifer in the last
#: two digits (415 = M-1 at 15% conifer).
FBP_TYPES: dict[int, FuelType] = {
    1: FuelType(1, "C-1", "C-1 Spruce-Lichen Woodland", 0.45, 12.0, True),
    2: FuelType(2, "C-2", "C-2 Boreal Spruce", 0.85, 18.0, True),
    3: FuelType(3, "C-3", "C-3 Mature Jack or Lodgepole Pine", 0.80, 18.0, True),
    4: FuelType(4, "C-4", "C-4 Immature Jack or Lodgepole Pine", 0.90, 10.0, True),
    5: FuelType(5, "C-5", "C-5 Red and White Pine", 0.40, 22.0, True),
    6: FuelType(6, "C-6", "C-6 Conifer Plantation", 0.70, 14.0, True),
    7: FuelType(7, "C-7", "C-7 Ponderosa Pine / Douglas Fir", 0.75, 25.0, True),
    11: FuelType(11, "D-1", "D-1 Leafless Aspen", 0.35, 12.0, True),
    12: FuelType(12, "D-2", "D-2 Green Aspen", 0.25, 12.0, True),
    13: FuelType(13, "D-1/D-2", "D-1/D-2 Aspen", 0.30, 12.0, True),
    31: FuelType(31, "O-1a", "O-1a Matted Grass", 0.55, 0.5, True),
    32: FuelType(32, "O-1b", "O-1b Standing Grass", 0.65, 0.7, True),
    101: FuelType(101, "NF", "Non-fuel", 0.00, 0.0, False),
    102: FuelType(102, "WA", "Water", 0.00, 0.0, False),
    105: FuelType(105, "VNF", "Vegetated Non-Fuel", 0.05, 1.0, False),
    106: FuelType(106, "URB", "Urban or built-up", 0.00, 0.0, False),
    415: FuelType(415, "M-1", "M-1 Boreal Mixedwood — leafless (15% conifer)", 0.40, 13.0, True),
    625: FuelType(625, "M-1/M-2", "M-1/M-2 Boreal Mixedwood (25% conifer)", 0.50, 14.0, True),
    650: FuelType(650, "M-1/M-2", "M-1/M-2 Boreal Mixedwood (50% conifer)", 0.60, 16.0, True),
    675: FuelType(675, "M-1/M-2", "M-1/M-2 Boreal Mixedwood (75% conifer)", 0.70, 18.0, True),
}


def classify(code: int | None) -> FuelType | None:
    if code is None:
        return None
    value = int(code)
    if value == NODATA:
        return None
    known = FBP_TYPES.get(value)
    if known:
        return known
    # Mixedwood variants share the last-two-digits convention (percent conifer).
    if 400 <= value <= 699:
        pct = value % 100
        return FuelType(
            value,
            "M",
            f"Boreal mixedwood (~{pct}% conifer)",
            min(0.85, 0.30 + pct / 100.0 * 0.55),
            12.0 + pct / 100.0 * 8.0,
            True,
        )
    return None


class FuelModel:
    """A municipal extract of the national FBP grid, sampled in WGS84."""

    def __init__(
        self,
        codes: np.ndarray,
        west_m: float,
        north_m: float,
        pixel_m: float,
        native_crs: str,
    ) -> None:
        self.codes = codes.astype(np.int32)
        self.west_m = west_m
        self.north_m = north_m
        self.pixel_m = pixel_m
        self.native_crs = native_crs
        self._rows, self._cols = self.codes.shape
        self._to_native = Transformer.from_crs(
            "EPSG:4326", native_crs, always_xy=True
        )

        canopy = np.zeros(self.codes.shape, dtype=np.float32)
        spread = np.zeros(self.codes.shape, dtype=np.float32)
        for code, spec in FBP_TYPES.items():
            mask = self.codes == code
            if mask.any():
                canopy[mask] = spec.canopy_height_m
                spread[mask] = spec.spread_factor
        # Unlisted mixedwood codes.
        unknown = (self.codes != NODATA) & (canopy == 0) & (self.codes >= 400)
        if unknown.any():
            for code in np.unique(self.codes[unknown]):
                spec = classify(int(code))
                if spec:
                    hit = self.codes == code
                    canopy[hit] = spec.canopy_height_m
                    spread[hit] = spec.spread_factor
        self.canopy = canopy
        self.spread = spread

    def _index(self, lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        east, north = self._to_native.transform(lon, lat)
        col = np.rint((np.asarray(east) - self.west_m) / self.pixel_m).astype(np.int64)
        row = np.rint((self.north_m - np.asarray(north)) / self.pixel_m).astype(np.int64)
        np.clip(col, 0, self._cols - 1, out=col)
        np.clip(row, 0, self._rows - 1, out=row)
        return row, col

    def code_at(self, lat: float, lon: float) -> int | None:
        row, col = self._index(np.array([lat]), np.array([lon]))
        value = int(self.codes[row[0], col[0]])
        return None if value == NODATA else value

    def sample(self, lat: float, lon: float) -> tuple[FuelType | None, float, float]:
        """Majority fuel type, mean spread factor and mean canopy in a 3x3 window.

        An H3-10 cell is about 150 m across and the fuel grid is 100 m, so a
        single pixel would under-represent a mixed cell.
        """
        row, col = self._index(np.array([lat]), np.array([lon]))
        r0 = max(0, int(row[0]) - 1)
        r1 = min(self._rows, int(row[0]) + 2)
        c0 = max(0, int(col[0]) - 1)
        c1 = min(self._cols, int(col[0]) + 2)
        window = self.codes[r0:r1, c0:c1]
        valid = window[window != NODATA]
        if valid.size == 0:
            return None, 0.0, 0.0
        values, counts = np.unique(valid, return_counts=True)
        majority = int(values[int(np.argmax(counts))])
        return (
            classify(majority),
            float(self.spread[r0:r1, c0:c1].mean()),
            float(self.canopy[r0:r1, c0:c1].mean()),
        )

    def canopy_at(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        row, col = self._index(lat, lon)
        return self.canopy[row, col]
