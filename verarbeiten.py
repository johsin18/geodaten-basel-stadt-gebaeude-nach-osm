#!/usr/bin/env python3
"""
BB_BoFlaeche.shp → OSM-Datei konvertieren

Liest die Amtliche-Vermessung-Flächen (MOpublic), konvertiert LV95 → WGS84
und schreibt eine OSM-XML-Datei.

Verwendung:
    python verarbeiten.py
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import xml.dom.minidom
from pathlib import Path
import shapefile
from pyproj import CRS, Transformer

_DEFAULT_DIR = Path(__file__).parent / "data"

# Mapping AV-Typ → OSM-Tags
TYP_TAGS: dict[str, dict[str, str]] = {
    "Gebaeude":           {"building": "yes"},
    "Gartenanlage":       {"leisure": "garden"},
    # "Strasse_Weg", "Trottoir", "Verkehrsinsel", "uebrige_befestigte" werden ausgelassen
}


_LV95 = CRS.from_wkt(
    """PROJCS["CH1903+ / LV95",GEOGCS["CH1903+",DATUM["CH1903+",SPHEROID["Bessel 1841",6377397.155,299.1528128],TOWGS84[674.374,15.056,405.346,0,0,0,0]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4150"]],PROJECTION["Hotine_Oblique_Mercator_Azimuth_Center"],PARAMETER["latitude_of_center",46.9524055555556],PARAMETER["longitude_of_center",7.43958333333333],PARAMETER["azimuth",90],PARAMETER["rectified_grid_angle",90],PARAMETER["scale_factor",1],PARAMETER["false_easting",2600000],PARAMETER["false_northing",1200000],UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Easting",EAST],AXIS["Northing",NORTH],AUTHORITY["EPSG","2056"]]"""
)
_TRANSFORMER = Transformer.from_crs(_LV95, "EPSG:4326", always_xy=True)


def lv95_to_wgs84(E: float, N: float) -> tuple[float, float]:
    """Konvertiert LV95 (E, N) nach WGS84 (lat, lon) via pyproj."""
    lon, lat = _TRANSFORMER.transform(E, N)
    return round(lat, 8), round(lon, 8)


def konvertiere(auftrag_dir: Path = None) -> None:
    if auftrag_dir is None:
        auftrag_dir = _DEFAULT_DIR

    data_dir   = auftrag_dir / "MO_MOpublic"
    input_shps = [
        data_dir / "BB_BoFlaeche.shp",
        data_dir / "BB_ProjBoFlaeche.shp",
    ]
    output_osm = auftrag_dir / "Gebaeude.osm"

    osm = ET.Element("osm", version="0.6", generator="verarbeiten.py")

    node_id = -1
    way_id  = -1

    for input_shp in input_shps:
      if not input_shp.exists():
          print(f"{input_shp.name} nicht vorhanden, wird übersprungen.")
          continue
      sf = shapefile.Reader(str(input_shp))
      print(f"Lese {len(sf)} Features aus {input_shp.name} …")
      for shape_rec in sf.iterShapeRecords():
          rec   = shape_rec.record.as_dict()
          shape = shape_rec.shape
          typ   = rec.get("Typ", "")
          tags  = TYP_TAGS.get(typ)

          if tags is None:
              continue

          # Shapefile-Parts = Ringe (erster = äusserer, weitere = Löcher)
          parts = list(shape.parts) + [len(shape.points)]
          ringe = [shape.points[parts[i]:parts[i+1]] for i in range(len(parts) - 1)]

          if len(ringe) == 1:
              # Einfaches Polygon → Way
              ring_node_ids = []
              for (E, N) in ringe[0][:-1]:  # letzter Punkt = erster → weglassen
                  lat, lon = lv95_to_wgs84(E, N)
                  ET.SubElement(osm, "node",
                                id=str(node_id), lat=str(lat), lon=str(lon))
                  ring_node_ids.append(node_id)
                  node_id -= 1

              way = ET.SubElement(osm, "way", id=str(way_id))
              way_id -= 1
              for nid in ring_node_ids:
                  ET.SubElement(way, "nd", ref=str(nid))
              ET.SubElement(way, "nd", ref=str(ring_node_ids[0]))  # Ring schliessen

          else:
              # Mehrere Ringe → Multipolygon-Relation
              rel = ET.SubElement(osm, "relation", id=str(way_id))
              way_id -= 1

              for i, ring in enumerate(ringe):
                  ring_node_ids = []
                  for (E, N) in ring[:-1]:
                      lat, lon = lv95_to_wgs84(E, N)
                      ET.SubElement(osm, "node",
                                    id=str(node_id), lat=str(lat), lon=str(lon))
                      ring_node_ids.append(node_id)
                      node_id -= 1

                  w = ET.SubElement(osm, "way", id=str(way_id))
                  way_id -= 1
                  for nid in ring_node_ids:
                      ET.SubElement(w, "nd", ref=str(nid))
                  ET.SubElement(w, "nd", ref=str(ring_node_ids[0]))

                  rolle = "outer" if i == 0 else "inner"
                  ET.SubElement(rel, "member",
                                type="way", ref=str(int(w.get("id"))), role=rolle)

              ET.SubElement(rel, "tag", k="type", v="multipolygon")
              tags = {**tags}  # Kopie für Relation

          # Tags anhängen (an Way oder Relation – letztes hinzugefügtes Element)
          ziel = osm[-1]
          for k, v in tags.items():
              ET.SubElement(ziel, "tag", k=k, v=v)

    # ── Gebäudeeingänge (Punkte) ──────────────────────────────────────────────
    sf_ein = shapefile.Reader(str(data_dir / "GEB_Gebaeudeeingang.shp"), encoding="cp1252")
    print(f"Lese {len(sf_ein)} Features aus GEB_Gebaeudeeingang.shp …")
    for shape_rec in sf_ein.iterShapeRecords():
        rec   = shape_rec.record.as_dict()
        E, N  = shape_rec.shape.points[0]
        lat, lon = lv95_to_wgs84(E, N)
        nd = ET.SubElement(osm, "node",
                           id=str(node_id), lat=str(lat), lon=str(lon))
        node_id -= 1
        ET.SubElement(nd, "tag", k="entrance",        v="yes")
        ET.SubElement(nd, "tag", k="addr:street",     v=rec.get("Lokalisati", ""))
        ET.SubElement(nd, "tag", k="addr:housenumber",v=rec.get("Hausnummer", ""))

    raw = ET.tostring(osm, encoding="unicode")
    pretty = xml.dom.minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")
    output_osm.write_bytes(pretty)

    n_nodes = sum(1 for el in osm if el.tag == "node")
    n_ways  = sum(1 for el in osm if el.tag == "way")
    n_rels  = sum(1 for el in osm if el.tag == "relation")
    print(f"Geschrieben: {output_osm}")
    print(f"  {n_nodes} Nodes, {n_ways} Ways, {n_rels} Relationen")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Shapefiles → Häuser.osm konvertieren")
    parser.add_argument("verzeichnis", nargs="?", type=Path,
                        help="Auftragsordner mit MO_MOpublic/ (Standard: neuester in data/)")
    args = parser.parse_args()

    if args.verzeichnis:
        konvertiere(args.verzeichnis)
    else:
        # Neuesten Unterordner in data/ verwenden
        unterordner = sorted(
            (p for p in _DEFAULT_DIR.iterdir() if p.is_dir()),
            key=lambda p: p.name, reverse=True
        )
        if not unterordner:
            raise SystemExit("Kein Auftragsordner in data/ gefunden.")
        konvertiere(unterordner[0])
