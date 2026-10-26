"""Import contrôlé de mesures énergétiques (CSV / Excel).

Colonnes attendues (noms insensibles à la casse) :
    asset_code, timestamp, value, unit   (unit est optionnelle, défaut kWh)

Contrôles par ligne : asset identifiable dans l'entreprise, date valide et non
future, valeur numérique finie >= 0 et plausible, unité connue, doublon dans le
fichier, doublon en base, ordre temporel (par asset). Les lignes invalides sont
rejetées et listées avec leur numéro de ligne ; les lignes valides sont importées.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.db import transaction
from django.utils import timezone

from apps.energy.models import EnergyAsset, EnergyReading, ImportBatch
from apps.energy.validation import MIN_TIMESTAMP, unit_error

REQUIRED_COLUMNS = {"asset_code", "timestamp", "value"}
VALID_UNITS = {u.value for u in EnergyReading.Unit}
MAX_VALUE = Decimal("1000000000")  # au-delà : valeur jugée impossible
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 200_000


class ImportFileError(Exception):
    """Fichier inutilisable dans son ensemble (format, colonnes manquantes...)."""


@dataclass
class RowError:
    line: int  # numéro de ligne dans le fichier (en-tête = ligne 1)
    message: str


@dataclass
class ImportResult:
    total_rows: int = 0
    imported_rows: int = 0
    errors: list[RowError] = field(default_factory=list)
    batch: ImportBatch | None = None

    @property
    def rejected_rows(self):
        return len({e.line for e in self.errors})


def _read_dataframe(uploaded_file) -> pd.DataFrame:
    name = (getattr(uploaded_file, "name", "") or "").lower()
    size = getattr(uploaded_file, "size", None)
    if size is not None and size > MAX_FILE_BYTES:
        raise ImportFileError("Fichier trop volumineux (maximum 10 Mo).")
    try:
        if name.endswith(".csv"):
            raw = uploaded_file.read()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            df = pd.read_csv(io.StringIO(text), dtype=str, sep=None, engine="python", skipinitialspace=True)
        elif name.endswith((".xlsx", ".xlsm")):
            df = pd.read_excel(uploaded_file, dtype=str, engine="openpyxl")
        else:
            raise ImportFileError("Format non supporté : utilisez .csv ou .xlsx.")
    except ImportFileError:
        raise
    except Exception as exc:  # fichier corrompu, encodage invalide...
        raise ImportFileError(f"Impossible de lire le fichier : {exc}") from exc

    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ImportFileError("Colonnes manquantes : " + ", ".join(sorted(missing)))
    if len(df) > MAX_ROWS:
        raise ImportFileError(f"Trop de lignes (maximum {MAX_ROWS}).")
    return df


def import_readings(uploaded_file, company, user=None) -> ImportResult:
    """Valide et importe le fichier pour `company`. Jamais d'asset d'une autre entreprise."""
    df = _read_dataframe(uploaded_file)
    result = ImportResult(total_rows=len(df))
    assets = {a.code: a for a in EnergyAsset.objects.filter(company=company)}

    candidates = []  # (line, asset, ts, value, unit)
    seen = set()
    last_ts: dict[int, object] = {}

    for idx, row in df.iterrows():
        line = idx + 2
        code = str(row.get("asset_code") or "").strip()
        asset = assets.get(code)
        if not code or asset is None:
            result.errors.append(RowError(line, f"Asset inconnu : « {code[:40]} »."))
            continue
        if not asset.is_active:
            result.errors.append(RowError(line, f"Asset inactif : « {code} »."))
            continue

        ts = pd.to_datetime(row.get("timestamp"), errors="coerce")
        if pd.isna(ts):
            result.errors.append(RowError(line, "Date/heure invalide."))
            continue
        ts = ts.to_pydatetime()
        if timezone.is_naive(ts):
            ts = timezone.make_aware(ts, timezone.get_current_timezone())
        if ts > timezone.now():
            result.errors.append(RowError(line, "Date dans le futur."))
            continue
        if ts < MIN_TIMESTAMP:
            result.errors.append(RowError(line, "Date antérieure à 2000."))
            continue

        raw = str(row.get("value") if row.get("value") is not None else "").strip().replace(",", ".")
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError):
            result.errors.append(RowError(line, f"Valeur non numérique : « {raw[:30]} »."))
            continue
        if not value.is_finite() or (isinstance(value, Decimal) and math.isnan(float(value))):
            result.errors.append(RowError(line, "Valeur non finie."))
            continue
        if value < 0:
            result.errors.append(RowError(line, "Consommation négative impossible."))
            continue
        if value > MAX_VALUE:
            result.errors.append(RowError(line, "Valeur impossible (trop élevée)."))
            continue

        unit_raw = row.get("unit")
        unit = str(unit_raw).strip() if unit_raw is not None and not pd.isna(unit_raw) else ""
        unit = unit or EnergyReading.Unit.KWH
        if unit not in VALID_UNITS:
            result.errors.append(RowError(line, f"Unité inconnue : « {unit[:20]} »."))
            continue

        msg = unit_error(asset.energy_type, unit)
        if msg:
            result.errors.append(RowError(line, msg))
            continue

        if value.as_tuple().exponent < -3:
            value = value.quantize(Decimal("0.001"))  # la base garde 3 décimales

        key = (asset.pk, ts)
        if key in seen:
            result.errors.append(RowError(line, "Doublon dans le fichier."))
            continue
        if asset.pk in last_ts and ts < last_ts[asset.pk]:
            result.errors.append(RowError(line, "Ordre temporel non respecté pour cet asset."))
            continue

        seen.add(key)
        last_ts[asset.pk] = ts
        candidates.append((line, asset, ts, value, unit))

    # Doublons déjà présents en base
    if candidates:
        existing = set(
            EnergyReading.objects.for_company(company)
            .filter(asset__in={c[1] for c in candidates}, timestamp__gte=min(c[2] for c in candidates),
                    timestamp__lte=max(c[2] for c in candidates))
            .values_list("asset_id", "timestamp")
        )
        fresh = []
        for line, asset, ts, value, unit in candidates:
            if (asset.pk, ts) in existing:
                result.errors.append(RowError(line, "Mesure déjà existante en base."))
            else:
                fresh.append(EnergyReading(asset=asset, timestamp=ts, value=value, unit=unit))
        candidates = fresh

    result.errors.sort(key=lambda e: e.line)
    with transaction.atomic():
        EnergyReading.objects.bulk_create(candidates, batch_size=1000)
        result.imported_rows = len(candidates)
        result.batch = ImportBatch.objects.create(
            company=company,
            imported_by=user,
            filename=getattr(uploaded_file, "name", "import")[:255],
            total_rows=result.total_rows,
            imported_rows=result.imported_rows,
            rejected_rows=result.rejected_rows,
            errors=[{"line": e.line, "message": e.message} for e in result.errors[:500]],
        )
    return result
