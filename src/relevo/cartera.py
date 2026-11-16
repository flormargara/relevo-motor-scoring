"""Seam ``FuenteDeCartera``: lo que pasó después de la evaluación, leído de la entidad (ADR 0008).

La conversión, el desembolso, los pagos y la mora son de la entidad: Relevo no los
almacena, no los modela ni los valida. Los lee en su origen, a través de este seam, y
los enlaza con sus evaluaciones por el id de solicitud.

Es de solo lectura. Hay un adapter por tipo de origen. Hoy está ``CarteraEnArchivos``
(tres CSV, que es también lo que escribe el generador sintético) y ``CarteraEnMemoria``
(para tests). Más adelante vendrán los de base SQL y API.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

DIAS_DEFAULT = 90  # mora grave: la misma definición que el default a 12 meses del scorecard
HORIZONTE_MESES = 12

CONVERSIONES = "conversiones.csv"
DESEMBOLSOS = "desembolsos.csv"
ESTADOS = "estados.csv"


@dataclass(frozen=True)
class Desembolso:
    fecha: date
    monto: float
    plazo_meses: int
    tasa_nominal_anual: float  # en %, como la oferta


@dataclass(frozen=True)
class EstadoMensual:
    """La foto a fin de mes de un crédito desembolsado."""

    periodo: date  # primer día del mes
    saldo: float
    pagado: float  # lo que pagó en el mes
    dias_mora: int


@dataclass(frozen=True)
class RegistroCartera:
    """Lo que la entidad informa de una solicitud después de la evaluación."""

    solicitud_id: str
    convertida: bool  # el solicitante tomó la oferta
    desembolso: Desembolso | None = None
    estados: tuple[EstadoMensual, ...] = ()  # ordenados por período

    def meses_observados(self) -> int:
        return len(self.estados)

    def peor_mora(self, meses: int | None = None) -> int:
        """Los peores días de mora en los primeros ``meses`` desde el desembolso (todos si es ``None``)."""
        estados = self.estados if meses is None else self.estados[:meses]
        return max((e.dias_mora for e in estados), default=0)

    def en_default(self, meses: int = HORIZONTE_MESES, dias: int = DIAS_DEFAULT) -> bool:
        return self.peor_mora(meses) >= dias


class CarteraInvalida(ValueError):
    """Los datos de la entidad no cumplen el contrato del seam."""


class FuenteDeCartera(Protocol):
    def registros(self, solicitud_ids: Iterable[str] | None = None) -> list[RegistroCartera]:
        """Los registros de esas solicitudes (todas si es ``None``). Una solicitud de la que la
        entidad no informó nada no aparece: no es lo mismo que una oferta no convertida."""
        ...


class CarteraEnMemoria:
    def __init__(self, registros: Iterable[RegistroCartera]):
        self._registros = {r.solicitud_id: r for r in registros}

    def registros(self, solicitud_ids: Iterable[str] | None = None) -> list[RegistroCartera]:
        return _filtrar(self._registros, solicitud_ids)


class CarteraEnArchivos:
    """Tres CSV en un directorio, enlazados por la columna del id de solicitud:

    - ``conversiones.csv``: ``solicitud_id, convertida`` (``1``/``0``)
    - ``desembolsos.csv``: ``solicitud_id, fecha, monto, plazo_meses, tasa_nominal_anual``
    - ``estados.csv``: ``solicitud_id, periodo, saldo, pagado, dias_mora``

    La entidad declara cómo se llama en sus archivos la columna del id de solicitud
    (``columna_solicitud``). Los archivos se leen en cada consulta: son de la entidad y
    pueden cambiar.
    """

    def __init__(self, directorio: Path | str, columna_solicitud: str = "solicitud_id"):
        self.directorio = Path(directorio)
        self.columna = columna_solicitud

    def registros(self, solicitud_ids: Iterable[str] | None = None) -> list[RegistroCartera]:
        return _filtrar(self._leer(), solicitud_ids)

    def _leer(self) -> dict[str, RegistroCartera]:
        convertidas = {fila[self.columna]: _bool(fila["convertida"]) for fila in self._filas(CONVERSIONES)}
        desembolsos = {
            fila[self.columna]: Desembolso(
                fecha=date.fromisoformat(fila["fecha"]),
                monto=float(fila["monto"]),
                plazo_meses=int(fila["plazo_meses"]),
                tasa_nominal_anual=float(fila["tasa_nominal_anual"]),
            )
            for fila in self._filas(DESEMBOLSOS)
        }
        estados: dict[str, list[EstadoMensual]] = {}
        for fila in self._filas(ESTADOS):
            estados.setdefault(fila[self.columna], []).append(
                EstadoMensual(
                    periodo=date.fromisoformat(fila["periodo"]),
                    saldo=float(fila["saldo"]),
                    pagado=float(fila["pagado"]),
                    dias_mora=int(fila["dias_mora"]),
                )
            )

        if sobrantes := (desembolsos.keys() - {i for i, c in convertidas.items() if c}):
            raise CarteraInvalida(f"desembolsos sin conversión: {sorted(sobrantes)[:5]}")
        if sobrantes := (estados.keys() - desembolsos.keys()):
            raise CarteraInvalida(f"estados sin desembolso: {sorted(sobrantes)[:5]}")
        return {
            id: RegistroCartera(
                solicitud_id=id,
                convertida=convertida,
                desembolso=desembolsos.get(id),
                estados=tuple(sorted(estados.get(id, []), key=lambda e: e.periodo)),
            )
            for id, convertida in convertidas.items()
        }

    def _filas(self, nombre: str) -> list[dict[str, str]]:
        archivo = self.directorio / nombre
        if not archivo.exists():
            raise CarteraInvalida(f"falta {archivo}")
        with archivo.open(newline="", encoding="utf-8") as f:
            lector = csv.DictReader(f)
            if self.columna not in (lector.fieldnames or ()):
                raise CarteraInvalida(f"{nombre} no tiene la columna {self.columna!r}")
            return list(lector)


def escribir_en_archivos(registros: Iterable[RegistroCartera], directorio: Path | str) -> Path:
    """Escribe los registros en el formato que lee ``CarteraEnArchivos``."""
    directorio = Path(directorio)
    directorio.mkdir(parents=True, exist_ok=True)
    registros = list(registros)
    _escribir(
        directorio / CONVERSIONES,
        ["solicitud_id", "convertida"],
        ([r.solicitud_id, int(r.convertida)] for r in registros),
    )
    _escribir(
        directorio / DESEMBOLSOS,
        ["solicitud_id", "fecha", "monto", "plazo_meses", "tasa_nominal_anual"],
        (
            [r.solicitud_id, d.fecha.isoformat(), d.monto, d.plazo_meses, d.tasa_nominal_anual]
            for r in registros
            if (d := r.desembolso)
        ),
    )
    _escribir(
        directorio / ESTADOS,
        ["solicitud_id", "periodo", "saldo", "pagado", "dias_mora"],
        ([r.solicitud_id, e.periodo.isoformat(), e.saldo, e.pagado, e.dias_mora] for r in registros for e in r.estados),
    )
    return directorio


def _escribir(archivo: Path, encabezado: list[str], filas: Iterable[list[object]]) -> None:
    with archivo.open("w", newline="", encoding="utf-8") as f:
        escritor = csv.writer(f)
        escritor.writerow(encabezado)
        escritor.writerows(filas)


def _bool(valor: str) -> bool:
    if valor.strip().lower() in {"1", "true", "si", "sí"}:
        return True
    if valor.strip().lower() in {"0", "false", "no"}:
        return False
    raise CarteraInvalida(f"convertida debe ser 1 o 0, no {valor!r}")


def _filtrar(registros: Mapping[str, RegistroCartera], ids: Iterable[str] | None) -> list[RegistroCartera]:
    if ids is None:
        return list(registros.values())
    return [registros[i] for i in dict.fromkeys(ids) if i in registros]
