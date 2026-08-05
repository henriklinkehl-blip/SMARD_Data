#!/usr/bin/env python3
"""Aktualisiert SMARD-Zeitreihen und erzeugt eine ZIP-Datei.

Quelle: Bundesnetzagentur | SMARD.de
Lizenz der abgerufenen Marktdaten: CC BY 4.0

Zusätzlich zu den bisherigen Prognose- und Preisreihen werden die realisierte
Netzlast sowie die realisierte Wind-/PV-Erzeugung geladen. Aus Prognose und Ist
wird eine gemeinsame Fehlerdatei für probabilistische Modelltests erzeugt.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

ENDPOINT = "https://www.smard.de/nip-download-manager/nip/download/market-data"
TIMEZONE = ZoneInfo("Europe/Berlin")
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "Data"
DOWNLOAD_DIR = ROOT / "download"
START_DATE = date.fromisoformat(os.getenv("SMARD_START_DATE", "2022-01-01"))
LOOKBACK_DAYS = max(1, int(os.getenv("SMARD_LOOKBACK_DAYS", "7")))
FULL_REFRESH = os.getenv("FULL_REFRESH", "false").lower() in {"1", "true", "yes", "ja"}
BUILD_ZIP_ONLY = "--build-zip-only" in sys.argv
ERROR_FILENAME = "Prognosefehler_Last_Wind_PV_aktuell.csv"


@dataclass(frozen=True)
class Dataset:
    filename: str
    label: str
    module_ids: tuple[int, ...]
    region: str = "DE"
    future_days: int = 0


DATASETS = (
    Dataset(
        "DayAhead_Preise_aktuell.csv",
        "Day-Ahead-Preise",
        (8004169,),
        future_days=1,
    ),
    Dataset(
        "Prognostizierter_Stromverbrauch_aktuell.csv",
        "prognostizierter Stromverbrauch",
        (6000411,),
        future_days=1,
    ),
    Dataset(
        "Prognostizierte_Erzeugung_Wind_PV_aktuell.csv",
        "Wind-, Offshore- und Photovoltaik-Prognosen",
        (2000123, 2003791, 2000125),
        future_days=1,
    ),
    Dataset(
        "Realisierter_Stromverbrauch_Netzlast_aktuell.csv",
        "realisierter Stromverbrauch / Netzlast",
        (5000410,),
    ),
    Dataset(
        "Realisierte_Erzeugung_Wind_PV_aktuell.csv",
        "realisierte Wind-, Offshore- und Photovoltaik-Erzeugung",
        (1001225, 1004067, 1004068),
    ),
)


def berlin_ms(day: date, end_of_day: bool = False) -> int:
    local_time = dt_time(23, 59, 59, 999000) if end_of_day else dt_time(0, 0)
    value = datetime.combine(day, local_time, tzinfo=TIMEZONE)
    return int(value.timestamp() * 1000)


def split_periods(start: date, end: date) -> Iterable[tuple[date, date]]:
    current = start
    while current <= end:
        try:
            candidate = current.replace(year=current.year + 2) - timedelta(days=1)
        except ValueError:  # 29. Februar
            candidate = current.replace(year=current.year + 2, day=28) - timedelta(days=1)
        candidate = min(candidate, end)
        yield current, candidate
        current = candidate + timedelta(days=1)


def decode_response(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Die SMARD-Antwort konnte nicht als Text gelesen werden.")


def validate_csv(text: str, label: str) -> None:
    preview = text.lstrip()[:200].lower()
    if not text.strip() or preview.startswith("{") or preview.startswith("<"):
        raise ValueError(f"SMARD lieferte für {label} keine CSV-Datei.")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    if len(rows) < 2 or len(rows[0]) < 3:
        raise ValueError(f"Die CSV für {label} enthält keine verwertbaren Daten.")
    if "datum" not in rows[0][0].lower():
        raise ValueError(f"Unerwarteter CSV-Kopf für {label}: {rows[0][0]!r}")


def request_csv(dataset: Dataset, start: date, end: date) -> str:
    payload = {
        "request_form": [
            {
                "format": "CSV",
                "moduleIds": list(dataset.module_ids),
                "region": dataset.region,
                "timestamp_from": berlin_ms(start),
                "timestamp_to": berlin_ms(end, end_of_day=True),
                "type": "discrete",
                "language": "de",
            }
        ]
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Accept": "text/csv,application/octet-stream,*/*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0 SMARD-GitHub-Actions-Downloader/1.1",
        },
        method="POST",
    )

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                text = decode_response(response.read())
            validate_csv(text, dataset.label)
            return text
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = exc
            if attempt < 3:
                wait_seconds = attempt * 5
                print(f"  Versuch {attempt} fehlgeschlagen: {exc}. Neuer Versuch in {wait_seconds}s …")
                time.sleep(wait_seconds)
    raise RuntimeError(f"Download für {dataset.label} fehlgeschlagen: {last_error}")


def parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%d.%m.%Y %H:%M")


def read_csv_file(path: Path) -> tuple[list[str], dict[tuple[str, str], list[str]]]:
    if not path.exists() or path.stat().st_size < 50:
        return [], {}
    text = decode_response(path.read_bytes())
    reader = csv.reader(io.StringIO(text), delimiter=";")
    rows = list(reader)
    if not rows:
        return [], {}
    header = rows[0]
    data: dict[tuple[str, str], list[str]] = {}
    for row in rows[1:]:
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            data[(row[0].strip(), row[1].strip())] = row
    return header, data


def read_csv_text(text: str) -> tuple[list[str], dict[tuple[str, str], list[str]]]:
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    header = rows[0]
    data: dict[tuple[str, str], list[str]] = {}
    for row in rows[1:]:
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            data[(row[0].strip(), row[1].strip())] = row
    return header, data


def has_payload(row: list[str]) -> bool:
    values = [cell.strip() for cell in row[2:]]
    return any(value not in {"", "-", "–", "—", "NaN", "nan"} for value in values)


def latest_date(rows: dict[tuple[str, str], list[str]]) -> date | None:
    dates: list[date] = []
    for start, _ in rows:
        try:
            dates.append(parse_timestamp(start).date())
        except ValueError:
            continue
    return max(dates) if dates else None


def choose_update_start(existing: dict[tuple[str, str], list[str]], today: date) -> date:
    if FULL_REFRESH or not existing:
        return START_DATE
    latest = latest_date(existing)
    if latest is None:
        return START_DATE
    anchor = min(latest, today)
    return max(START_DATE, anchor - timedelta(days=LOOKBACK_DAYS))


def write_merged(path: Path, header: list[str], rows: dict[tuple[str, str], list[str]]) -> None:
    def sort_key(item: tuple[tuple[str, str], list[str]]) -> datetime:
        try:
            return parse_timestamp(item[0][0])
        except ValueError:
            return datetime.max

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", lineterminator="\n")
        writer.writerow(header)
        for _, row in sorted(rows.items(), key=sort_key):
            writer.writerow(row)


def update_dataset(dataset: Dataset, today: date) -> None:
    path = DATA_DIR / dataset.filename
    old_header, merged = read_csv_file(path)
    update_start = choose_update_start(merged, today)
    update_end = today + timedelta(days=dataset.future_days)

    print(f"\n{dataset.label}: {update_start.isoformat()} bis {update_end.isoformat()}")
    new_header: list[str] = []
    new_rows_count = 0

    for part_start, part_end in split_periods(update_start, update_end):
        print(f"  Lade {part_start.isoformat()} bis {part_end.isoformat()} …")
        text = request_csv(dataset, part_start, part_end)
        part_header, part_rows = read_csv_text(text)
        new_header = part_header or new_header
        for key, row in part_rows.items():
            # Leere Zukunftswerte überschreiben keine bereits vorhandenen Messwerte.
            if has_payload(row) or key not in merged:
                merged[key] = row
                new_rows_count += 1

    header = new_header or old_header
    if not header:
        raise RuntimeError(f"Für {dataset.label} konnte kein CSV-Kopf bestimmt werden.")
    write_merged(path, header, merged)
    print(
        f"  Gespeichert: {path.relative_to(ROOT)} "
        f"({len(merged):,} Zeitintervalle, {new_rows_count:,} aktualisiert)"
    )


def normalize_header(value: str) -> str:
    value = value.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def find_column(header: list[str], *terms: str) -> int:
    normalized_terms = tuple(normalize_header(term) for term in terms)
    for index, column in enumerate(header):
        normalized = normalize_header(column)
        if all(term in normalized for term in normalized_terms):
            return index
    raise ValueError(f"Spalte mit Begriffen {terms!r} nicht gefunden. Kopf: {header!r}")


def parse_german_number(value: str) -> float | None:
    text = value.strip().replace("\u00a0", "").replace(" ", "")
    if text in {"", "-", "–", "—", "NaN", "nan"}:
        return None
    # SMARD verwendet deutsches Zahlenformat: 12.345,67
    text = text.replace(".", "").replace(",", ".")
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def format_german_number(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.6f}".rstrip("0").rstrip(".").replace(".", ",")


def build_forecast_error_file() -> None:
    """Verknüpft Prognose und Ist über identische Viertelstundenintervalle.

    Fehlerdefinition: Istwert minus Prognose. Positive Werte bedeuten, dass der
    tatsächliche Wert höher war als die Day-Ahead-Prognose.
    """

    forecast_load_header, forecast_load = read_csv_file(
        DATA_DIR / "Prognostizierter_Stromverbrauch_aktuell.csv"
    )
    actual_load_header, actual_load = read_csv_file(
        DATA_DIR / "Realisierter_Stromverbrauch_Netzlast_aktuell.csv"
    )
    forecast_gen_header, forecast_gen = read_csv_file(
        DATA_DIR / "Prognostizierte_Erzeugung_Wind_PV_aktuell.csv"
    )
    actual_gen_header, actual_gen = read_csv_file(
        DATA_DIR / "Realisierte_Erzeugung_Wind_PV_aktuell.csv"
    )

    required = (
        forecast_load_header,
        actual_load_header,
        forecast_gen_header,
        actual_gen_header,
    )
    if not all(required):
        print("\nPrognosefehler-Datei übersprungen: Mindestens eine Quelldatei fehlt.")
        return

    fl_net = find_column(forecast_load_header, "Netzlast")
    al_net = find_column(actual_load_header, "Netzlast")
    fg_off = find_column(forecast_gen_header, "Wind Offshore")
    fg_on = find_column(forecast_gen_header, "Wind Onshore")
    fg_pv = find_column(forecast_gen_header, "Photovoltaik")
    ag_off = find_column(actual_gen_header, "Wind Offshore")
    ag_on = find_column(actual_gen_header, "Wind Onshore")
    ag_pv = find_column(actual_gen_header, "Photovoltaik")

    common_keys = set(forecast_load) & set(actual_load) & set(forecast_gen) & set(actual_gen)
    output_rows: list[list[str]] = []

    def sort_key(key: tuple[str, str]) -> datetime:
        try:
            return parse_timestamp(key[0])
        except ValueError:
            return datetime.max

    for key in sorted(common_keys, key=sort_key):
        forecast_load_value = parse_german_number(forecast_load[key][fl_net])
        actual_load_value = parse_german_number(actual_load[key][al_net])
        forecast_off = parse_german_number(forecast_gen[key][fg_off])
        actual_off = parse_german_number(actual_gen[key][ag_off])
        forecast_on = parse_german_number(forecast_gen[key][fg_on])
        actual_on = parse_german_number(actual_gen[key][ag_on])
        forecast_pv = parse_german_number(forecast_gen[key][fg_pv])
        actual_pv = parse_german_number(actual_gen[key][ag_pv])

        values = (
            forecast_load_value,
            actual_load_value,
            forecast_off,
            actual_off,
            forecast_on,
            actual_on,
            forecast_pv,
            actual_pv,
        )
        if any(value is None for value in values):
            continue

        assert forecast_load_value is not None and actual_load_value is not None
        assert forecast_off is not None and actual_off is not None
        assert forecast_on is not None and actual_on is not None
        assert forecast_pv is not None and actual_pv is not None

        forecast_wind_total = forecast_off + forecast_on
        actual_wind_total = actual_off + actual_on
        forecast_residual_load = forecast_load_value - forecast_wind_total - forecast_pv
        actual_residual_load = actual_load_value - actual_wind_total - actual_pv

        output_rows.append(
            [
                key[0],
                key[1],
                format_german_number(forecast_load_value),
                format_german_number(actual_load_value),
                format_german_number(actual_load_value - forecast_load_value),
                format_german_number(forecast_off),
                format_german_number(actual_off),
                format_german_number(actual_off - forecast_off),
                format_german_number(forecast_on),
                format_german_number(actual_on),
                format_german_number(actual_on - forecast_on),
                format_german_number(forecast_pv),
                format_german_number(actual_pv),
                format_german_number(actual_pv - forecast_pv),
                format_german_number(forecast_wind_total),
                format_german_number(actual_wind_total),
                format_german_number(actual_wind_total - forecast_wind_total),
                format_german_number(forecast_residual_load),
                format_german_number(actual_residual_load),
                format_german_number(actual_residual_load - forecast_residual_load),
            ]
        )

    output_path = DATA_DIR / ERROR_FILENAME
    header = [
        "Datum von",
        "Datum bis",
        "Prognose Netzlast [MWh]",
        "Ist Netzlast [MWh]",
        "Fehler Netzlast Ist-minus-Prognose [MWh]",
        "Prognose Wind Offshore [MWh]",
        "Ist Wind Offshore [MWh]",
        "Fehler Wind Offshore Ist-minus-Prognose [MWh]",
        "Prognose Wind Onshore [MWh]",
        "Ist Wind Onshore [MWh]",
        "Fehler Wind Onshore Ist-minus-Prognose [MWh]",
        "Prognose Photovoltaik [MWh]",
        "Ist Photovoltaik [MWh]",
        "Fehler Photovoltaik Ist-minus-Prognose [MWh]",
        "Prognose Wind gesamt [MWh]",
        "Ist Wind gesamt [MWh]",
        "Fehler Wind gesamt Ist-minus-Prognose [MWh]",
        "Prognose Residuallast [MWh]",
        "Ist Residuallast [MWh]",
        "Fehler Residuallast Ist-minus-Prognose [MWh]",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", lineterminator="\n")
        writer.writerow(header)
        writer.writerows(output_rows)

    if output_rows:
        first_date = output_rows[0][0]
        last_date = output_rows[-1][0]
        print(
            f"\nPrognosefehler-Datei erstellt: {output_path.relative_to(ROOT)} "
            f"({len(output_rows):,} vollständige Intervalle, {first_date} bis {last_date})"
        )
    else:
        print(f"\nWarnung: {ERROR_FILENAME} wurde erstellt, enthält aber noch keine vollständigen Intervalle.")


def build_zip(today: date) -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    notes = DOWNLOAD_DIR / "QUELLE_UND_HINWEISE.txt"
    notes.write_text(
        "Quelle: Bundesnetzagentur | SMARD.de\n"
        "Lizenz: CC BY 4.0\n"
        f"Automatisch aktualisiert: {datetime.now(TIMEZONE):%Y-%m-%d %H:%M:%S %Z}\n"
        f"Basiszeitraum ab: {START_DATE.isoformat()}\n\n"
        "Enthaltene Reihen:\n"
        "- Day-Ahead-Preise\n"
        "- prognostizierter Stromverbrauch / Netzlast\n"
        "- prognostizierte Erzeugung Wind Onshore, Wind Offshore und Photovoltaik\n"
        "- realisierter Stromverbrauch / Netzlast\n"
        "- realisierte Erzeugung Wind Onshore, Wind Offshore und Photovoltaik\n"
        "- Prognosefehler und Residuallastfehler (Ist minus Prognose)\n",
        encoding="utf-8",
    )

    zip_path = DOWNLOAD_DIR / "SMARD_Daten_aktuell.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for dataset in DATASETS:
            source = DATA_DIR / dataset.filename
            if not source.exists():
                raise FileNotFoundError(f"Fehlende Datei: {source}")
            archive.write(source, arcname=source.name)
        error_source = DATA_DIR / ERROR_FILENAME
        if error_source.exists():
            archive.write(error_source, arcname=error_source.name)
        archive.write(notes, arcname=notes.name)

    status = DOWNLOAD_DIR / "Letzte_Aktualisierung.txt"
    status.write_text(
        f"Letzte Aktualisierung: {datetime.now(TIMEZONE):%d.%m.%Y %H:%M:%S %Z}\n"
        f"ZIP-Datei: {zip_path.name}\n",
        encoding="utf-8",
    )
    print(f"\nZIP erstellt: {zip_path.relative_to(ROOT)}")


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(TIMEZONE).date()
    if not BUILD_ZIP_ONLY:
        for dataset in DATASETS:
            update_dataset(dataset, today)
        build_forecast_error_file()
    build_zip(today)


if __name__ == "__main__":
    main()
