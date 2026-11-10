"""Tipos del dominio. Los nombres siguen GLOSSARY.md."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from relevo.consultor import Snapshot


class Veredicto(StrEnum):
    APROBADO = "APROBADO"
    DERIVAR = "DERIVAR"
    RECHAZADO = "RECHAZADO"


@dataclass(frozen=True)
class Solicitud:
    """El pedido de crédito de un solicitante."""

    cuit: str
    segmento: str
    monto: float
    plazo_meses: int


@dataclass(frozen=True)
class Oferta:
    """Condiciones propuestas por el motor. La tasa es nominal anual, en %."""

    monto: float
    plazo_meses: int
    tasa_nominal_anual: float
    cuota: float


@dataclass(frozen=True)
class Paso:
    """Un paso de la traza: qué se evaluó, con qué valores y qué dio."""

    etapa: str
    regla: str
    resultado: str
    detalle: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Evaluacion:
    """Una corrida inmutable del motor sobre una solicitud."""

    solicitud: Solicitud
    version_reglas: str
    veredicto: Veredicto
    motivos: tuple[str, ...]
    traza: tuple[Paso, ...]
    snapshots: tuple[Snapshot, ...]
    fuentes_no_consultadas: tuple[str, ...]
    puntos: float | None = None
    pd: float | None = None
    rating: str | None = None
    oferta: Oferta | None = None

    def pasos(self, etapa: str) -> list[Paso]:
        return [paso for paso in self.traza if paso.etapa == etapa]
