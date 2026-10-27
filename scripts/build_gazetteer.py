"""Build the bundled offline gazetteer from the GeoNames ``DE.txt`` dump.

Usage::

    curl -O https://download.geonames.org/export/dump/DE.zip && unzip DE.zip
    python scripts/build_gazetteer.py DE.txt src/jll_harvester/data/gazetteer_de.tsv.gz

Data: GeoNames (https://www.geonames.org), licensed CC BY 4.0.

The output keeps three kinds of entries:

* ``place``    — municipalities / towns (population >= 1000, or admin seats)
* ``district`` — named city sections (GeoNames ``PPLX``); only matched in explicit district context
* ``locality`` — small populated places (< 1000 inhabitants); only used for proximity-checked
  district look-ups such as "Bonn-Auerberg", never matched free-standing in text
* ``region``   — states, Regierungsbezirke and Landkreise; never used as a point location
"""

from __future__ import annotations

import csv
import gzip
import math
import sys
from pathlib import Path

PLACE_CODES = {"PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLC", "PPLF", "PPLS"}
# GeoNames uses English exonyms for a handful of German cities; press releases use the endonym.
ENDONYMS = {
    "Munich": "München", "Nuremberg": "Nürnberg", "Cologne": "Köln", "Hanover": "Hannover",
    "Brunswick": "Braunschweig", "Frankfurt": "Frankfurt am Main",
}  # fmt: skip
REGION_PREFIXES = ("Regierungsbezirk ", "Landkreis ", "Kreis ", "Stadtkreis ", "Region ")


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return 6371.0 * math.hypot(dlat, dlon)


def build(src: Path, dst: Path) -> int:
    places: list[dict[str, str]] = []
    admin4: list[dict[str, str]] = []
    out: list[dict[str, str]] = []

    with src.open(encoding="utf-8") as fh:
        for row in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
            gid, name, ascii_name = row[0], row[1], row[2]
            lat, lon, fclass, fcode, admin1 = row[4], row[5], row[6], row[7], row[10]
            pop = int(row[14] or 0)
            if fclass in {"P", "A"}:
                # "München, Landeshauptstadt" → "München"; "Munich" → "München"
                name = ENDONYMS.get(name, name.split(",")[0].strip())
                ascii_name = ascii_name.split(",")[0].strip()
            rec = {
                "geonameid": gid,
                "name": name,
                "ascii": ascii_name,
                "lat": lat,
                "lon": lon,
                "fcode": fcode,
                "admin1": admin1,
                "population": str(pop),
            }
            if fclass == "P" and fcode in PLACE_CODES and (pop >= 1000 or fcode.startswith("PPLA")):
                places.append({**rec, "kind": "place"})
            elif fclass == "P" and fcode == "PPLX":
                out.append({**rec, "kind": "district"})
            elif fclass == "P" and fcode in PLACE_CODES:
                out.append({**rec, "kind": "locality"})
            elif fclass == "A" and fcode == "ADM4" and pop >= 1000:
                admin4.append({**rec, "kind": "place"})
            elif fclass == "A" and fcode in {"ADM1", "ADM2", "ADM3"}:
                for prefix in REGION_PREFIXES:
                    if name.startswith(prefix):
                        rec["name"] = name.removeprefix(prefix)
                        rec["ascii"] = ascii_name.removeprefix(prefix)
                out.append({**rec, "kind": "region"})

    # Municipalities without a same-named populated place (e.g. "Sassenburg") come from ADM4.
    by_name: dict[str, list[dict[str, str]]] = {}
    for p in places:
        by_name.setdefault(p["name"], []).append(p)
    for a in admin4:
        twins = [
            t
            for t in by_name.get(a["name"], [])
            if _km(float(a["lat"]), float(a["lon"]), float(t["lat"]), float(t["lon"])) < 15
        ]
        for t in twins:  # seats like "Hoppegarten" carry population 0; the municipality has the real figure
            t["population"] = str(max(int(t["population"]), int(a["population"])))
        if not twins:
            places.append(a)

    out.extend(places)
    out.sort(key=lambda r: (r["kind"], r["name"], -int(r["population"])))
    dst.parent.mkdir(parents=True, exist_ok=True)
    fields = ["geonameid", "kind", "name", "ascii", "lat", "lon", "fcode", "admin1", "population"]
    with gzip.open(dst, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(out)
    return len(out)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    n = build(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"wrote {n} gazetteer entries to {sys.argv[2]}")
