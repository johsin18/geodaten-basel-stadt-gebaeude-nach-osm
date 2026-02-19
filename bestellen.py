#!/usr/bin/env python3
"""
Geodaten-Shop Basel-Stadt – automatische Bestellung
Nimmt eine OpenStreetMap-URL und bestellt Geodaten für den entsprechenden Ausschnitt.

Verwendung:
    python bestellen.py "https://www.openstreetmap.org/#map=20/47.5712341/7.5960305"

Optionen:
    --radius   Halbmesser des Ausschnitts in Metern (Standard: 250 → 0.25 km²)
    --format   shp (Standard), dxf, dwg, itf
    --ebenen   Bodenbedeckung Gebäudeadressen Liegenschaften ... (Leerzeichen-getrennt)
    --sichtbar Browser sichtbar anzeigen (nützlich zum Debuggen)
"""

from __future__ import annotations

import re
import time
import imaplib
import email as email_lib
import argparse
import urllib.request
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from verarbeiten import konvertiere

SHOP_URL = "https://shop.geo.bs.ch/"

_config    = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8"))
EMAIL_ZIEL = _config["email"]
IMAP_HOST  = (_config.get("imap") or {}).get("host")
IMAP_USER  = (_config.get("imap") or {}).get("username") or EMAIL_ZIEL
IMAP_PASS  = (_config.get("imap") or {}).get("password")

ALLE_EBENEN = [
    "Bodenbedeckung",
    "Einzelobjekte",
    "Fixpunkte",
    "Gebäudeadressen",
    "Gebietsgrenzen",
    "Höhen",
    "Liegenschaften",
    "Nomenklatur",
    "Rohrleitungen",
]


def parse_osm_url(url: str) -> tuple[float, float]:
    """Extrahiert lat/lon aus einer OSM-URL wie #map=ZOOM/LAT/LON"""
    match = re.search(r"#map=\d+/([-\d.]+)/([-\d.]+)", url)
    if not match:
        raise ValueError(f"Keine Koordinaten in URL gefunden: {url!r}\n"
                         f"Erwartet: https://www.openstreetmap.org/#map=ZOOM/LAT/LON")
    return float(match.group(1)), float(match.group(2))


def wgs84_to_lv95(lat: float, lon: float) -> tuple[int, int]:
    """
    Konvertiert WGS84-Koordinaten (Grad) nach LV95 (Meter).
    Formel: swisstopo Merkblatt "Umrechnung von Koordinaten"
    Rückgabe: (E, N) gerundet auf ganze Meter.
    """
    phi = (lat * 3600 - 169028.66) / 10000
    lam = (lon * 3600 - 26782.5)  / 10000

    E = (2_600_072.37
         + 211_455.93 * lam
         -  10_938.51 * lam * phi
         -       0.36 * lam * phi**2
         -      44.54 * lam**3)

    N = (1_200_147.07
         + 308_807.95 * phi
         +   3_745.25 * lam**2
         +      76.63 * phi**2
         -     194.56 * lam**2 * phi
         +     119.79 * phi**3)

    return round(E), round(N)


def _body_aus_msg(msg) -> str:
    """Extrahiert den HTML-Body (fallback: plain text) aus einer E-Mail."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return part.get_payload(decode=True).decode(errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode(errors="replace")
    return msg.get_payload(decode=True).decode(errors="replace")


def warte_auf_antwort(
    seit: datetime,
    betreff_filter: str = None,
    timeout_s: int = 600,
    poll_s: int = 5,
) -> str:
    """
    Pollt den IMAP-Posteingang und gibt den Body der ersten neuen E-Mail zurück,
    die nach `seit` eingetroffen ist und deren Betreff `betreff_filter` enthält.
    """
    deadline = time.monotonic() + timeout_s
    seit_utc = seit.astimezone(timezone.utc)
    datum_imap = seit.strftime("%d-%b-%Y")

    while time.monotonic() < deadline:
        try:
            with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
                imap.login(IMAP_USER, IMAP_PASS)
                imap.select("INBOX")

                _, ids = imap.search(None, f'SINCE "{datum_imap}"')
                for mid in reversed(ids[0].split()):
                    _, data = imap.fetch(mid, "(RFC822)")
                    msg = email_lib.message_from_bytes(data[0][1])

                    try:
                        msg_zeit = parsedate_to_datetime(msg.get("Date", ""))
                    except Exception:
                        continue

                    if msg_zeit.astimezone(timezone.utc) < seit_utc:
                        continue

                    betreff = str(email_lib.header.make_header(
                        email_lib.header.decode_header(msg.get("Subject", ""))))
                    if betreff_filter and betreff_filter not in betreff:
                        continue

                    print(f"E-Mail erhalten (Betreff: {betreff})")
                    return _body_aus_msg(msg)

        except Exception as e:
            print(f"IMAP-Fehler: {e}")

        verbleibend = int(deadline - time.monotonic())
        print(f"  Noch keine E-Mail – warte {poll_s}s (noch {verbleibend}s) …")
        time.sleep(poll_s)

    raise TimeoutError(f"Keine Antwort-E-Mail innerhalb von {timeout_s}s erhalten.")


def extrahiere_download_links(html: str) -> list[str]:
    """Findet Download-URLs der Form https://shop.geo.bs.ch/php/download.php?... (dedupliziert)."""
    gefunden = re.findall(r'https://shop\.geo\.bs\.ch/php/download\.php[^\s"\'<>]*', html)
    return list(dict.fromkeys(gefunden))  # Reihenfolge erhalten, Duplikate entfernen


def bestellen(
    osm_url:  str,
    radius_m: int        = 100,
    ebenen:   list[str]  = None,
    headless: bool       = True,
) -> None:
    """
    Öffnet den Geodaten-Shop BS, konfiguriert und sendet eine Bestellung für
    "Amtliche Vermessung MOpublic" ab.

    Args:
        osm_url:  OpenStreetMap-URL mit Koordinaten im Fragment (#map=…)
        radius_m: Halbmesser des quadratischen Ausschnitts in Metern
        ebenen:   Gewünschte Datenschichten (None = Bodenbedeckung + Gebäudeadressen)
        headless: False = Browser sichtbar (für Debugging)
    """
    if ebenen is None:
        ebenen = ["Bodenbedeckung", "Gebäudeadressen"]

    # Unbekannte Ebenen abfangen
    unbekannt = [e for e in ebenen if e not in ALLE_EBENEN]
    if unbekannt:
        raise ValueError(f"Unbekannte Ebene(n): {unbekannt}\nVerfügbar: {ALLE_EBENEN}")

    # Koordinaten umrechnen
    lat, lon = parse_osm_url(osm_url)
    E, N = wgs84_to_lv95(lat, lon)

    oben   = N + radius_m   # N_max
    unten  = N - radius_m   # N_min
    links  = E - radius_m   # E_min
    rechts = E + radius_m   # E_max

    flaeche_km2 = (2 * radius_m / 1000) ** 2

    print(f"OSM:       lat={lat}, lon={lon}")
    print(f"LV95:      E={E}, N={N}")
    print(f"Ausschnitt Oben={oben} Unten={unten} Links={links} Rechts={rechts}")
    print(f"Fläche:    {flaeche_km2:.4f} km²")
    print(f"Ebenen:    {', '.join(ebenen)}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        # ── Shop laden ────────────────────────────────────────────────────────
        print("Öffne Shop …")
        page.goto(SHOP_URL, wait_until="networkidle")

        # ── Kategorie "Amtliche Vermessung" aufklappen ────────────────────────
        page.get_by_text("Amtliche Vermessung", exact=True).click()

        # ── Produkt "Amtliche Vermessung MOpublic" öffnen ─────────────────────
        page.get_by_role("link", name="Amtliche Vermessung MOpublic Bestellung öffnen").click()
        page.wait_for_selector("text=Seite 1 / 2")

        # ══ Seite 1: Format & Ebenen ══════════════════════════════════════════

        # Format: immer Esri Shapefile
        page.get_by_role("radio", name="Esri Shapefile").click()

        # Alle Ebenen abwählen (Toggle-Link ruft checkEinAus() auf)
        # Wir klicken ihn genau einmal → "Alle abwählen"
        seite1_ebenen = [cb.is_checked()
                         for cb in page.get_by_role("checkbox").all()]
        if any(seite1_ebenen):
            page.get_by_role("link", name="Alle aus-/abwählen").click()

        # Gewünschte Ebenen einzeln anwählen
        for ebene in ebenen:
            page.get_by_role("checkbox", name=ebene).check()

        # Weiter →
        page.locator("#weiter").click()
        page.wait_for_selector("text=Seite 2 / 2")

        # ══ Seite 2: Bestellausschnitt ════════════════════════════════════════

        page.get_by_role("radio", name="Koordinaten").click()

        # IDs gemäss Shop-Formular: maxx=Oben, minx=Unten, miny=Links, maxy=Rechts
        page.locator("#maxx").fill(str(oben))
        page.locator("#minx").fill(str(unten))
        page.locator("#miny").fill(str(links))
        page.locator("#maxy").fill(str(rechts))

        # In den Warenkorb
        print("Lege in Warenkorb …")
        page.locator("#Warenkorb").click()
        page.wait_for_selector("text=Bestellung abschliessen")

        # ══ Checkout: Bestellung abschliessen ════════════════════════════════
        page.get_by_role("link", name="Bestellung abschliessen").click()
        page.wait_for_selector("#mail")
        time.sleep(1)

        page.locator("#mail").fill(EMAIL_ZIEL)
        page.locator("#mail2").fill(EMAIL_ZIEL)
        page.locator("#bestMail").check()
        page.locator("#chkAGB").check()

        # Bestellung absenden
        bestellzeit = datetime.now(tz=timezone.utc)
        print(f"Sende Bestellung an {EMAIL_ZIEL} …")
        time.sleep(1)
        page.evaluate("document.getElementById('cmdAbschluss').click()")
        print(f"Bestellung abgeschickt! Bestätigung wird an {EMAIL_ZIEL} gesendet.")

        page.wait_for_selector("#txtAusgabe", timeout=15_000)
        time.sleep(5)
        browser.close()

    base_dir = Path(__file__).parent / "data"
    base_dir.mkdir(exist_ok=True)
    datum = bestellzeit.strftime("%Y-%m-%d")
    auftrag_dir = base_dir / f"{datum}_{lat}_{lon}"
    auftrag_dir.mkdir(exist_ok=True)
    print(f"Auftragsordner: {auftrag_dir}")

    if not IMAP_HOST or not IMAP_PASS:
        print("Kein IMAP konfiguriert – Skript endet nach der Bestellung.")
        return

    # Zweites E-Mail: Lieferung mit Download-Link
    print("Warte auf Lieferungs-E-Mail …")
    lieferung = warte_auf_antwort(seit=bestellzeit, betreff_filter="Lieferung", timeout_s=1800)

    links = extrahiere_download_links(lieferung)
    if not links:
        raise RuntimeError("Kein Download-Link in der Lieferungs-E-Mail gefunden.")

    for link in links:
        zip_pfad = auftrag_dir / "Gebäude.zip"

        print(f"Lade herunter: {link}")
        urllib.request.urlretrieve(link, zip_pfad)
        print(f"Gespeichert:   {zip_pfad}")

        if zipfile.is_zipfile(zip_pfad):
            print(f"Entpacke …")
            with zipfile.ZipFile(zip_pfad) as zf:
                zf.extractall(auftrag_dir)
            print(f"Entpackt nach: {auftrag_dir}")

    konvertiere(auftrag_dir)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Amtliche Vermessung MOpublic automatisch im Geodaten-Shop BS bestellen",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Verfügbare Ebenen: {', '.join(ALLE_EBENEN)}",
    )
    parser.add_argument(
        "url",
        help="OpenStreetMap-URL, z. B. 'https://www.openstreetmap.org/#map=20/47.57/7.59'",
    )
    parser.add_argument(
        "--radius", type=int, default=100, metavar="METER",
        help="Halbmesser des Ausschnitts in Metern (Standard: 100 → 0.04 km²)",
    )
    parser.add_argument(
        "--ebenen", nargs="+", default=["Bodenbedeckung", "Gebäudeadressen"],
        metavar="EBENE",
        help="Datenschichten (Standard: Bodenbedeckung Gebäudeadressen)",
    )
    parser.add_argument(
        "--sichtbar", action="store_true",
        help="Browser sichtbar anzeigen (Standard: headless)",
    )
    args = parser.parse_args()

    bestellen(
        osm_url  = args.url,
        radius_m = args.radius,
        ebenen   = args.ebenen,
        headless = not args.sichtbar,
    )
