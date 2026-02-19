# geodaten-basel-stadt-gebaeude-nach-osm

Automatische Bestellung und Verarbeitung von Gebäudedaten aus dem [Geodaten-Shop Basel-Stadt](https://shop.geo.bs.ch).

## Konfiguration

Einstellungen in `config.yaml`:

```yaml
email: ihre@email.ch

imap:          # optional – ohne IMAP endet das Skript nach der Bestellung
  host: imap.example.com
  username: login@example.com   # optional, Standard: email
  password: geheim
```

## Installation

```bash
uv sync
uv run playwright install chromium
```

## Verwendung

### Bestellung aufgeben

```bash
uv run python bestellen.py "https://www.openstreetmap.org/#map=20/47.5714605/7.5962882"
```

Die Daten werden automatisch bestellt, heruntergeladen, entpackt und als `Häuser.osm` in einem datierten Unterordner unter `data/` gespeichert.

**Optionen:**

| Option | Beschreibung | Standard |
|---|---|---|
| `--radius METER` | Halbmesser des Ausschnitts | 100 m |
| `--ebenen EBENE …` | Datenschichten | Bodenbedeckung Gebäudeadressen |
| `--sichtbar` | Browser sichtbar anzeigen | headless |

### Daten nachträglich verarbeiten

```bash
uv run python verarbeiten.py data/2026-02-20_47.5714605_7.5962882
```

Ohne Pfadangabe wird der neueste Unterordner in `data/` verwendet.
