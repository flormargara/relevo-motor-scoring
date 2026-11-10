"""Seam ``Consultor``: el motor pide variables por fuente y nunca ve HTTP."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class Disponible:
    """La fuente respondió y estas son sus variables, ya traducidas."""

    variables: Mapping[str, Any]


@dataclass(frozen=True)
class SinAntecedentes:
    """La fuente respondió que no tiene datos del solicitante. Es un dato, no una falla."""


@dataclass(frozen=True)
class NoDisponible:
    """La fuente no pudo responder, después de los reintentos."""

    motivo: str


ResultadoFuente = Disponible | SinAntecedentes | NoDisponible


@dataclass(frozen=True)
class Snapshot:
    """La respuesta cruda de una fuente, con su fecha y hora de consulta."""

    fuente: str
    consultado_en: datetime
    crudo: Any
    resultado: ResultadoFuente
    origen: str = "consulta"  # consulta | reutilizado | ultimo_guardado
    intentos: int = 1
    nota: str | None = None


class Consultor(Protocol):
    def variables(self, fuente: str) -> ResultadoFuente: ...

    def snapshots(self) -> list[Snapshot]: ...


MOMENTO_FIJO = datetime(2026, 11, 9, 12, 0, 0)


@dataclass
class ConsultorFalso:
    """Devuelve resultados fijos por fuente. Una fuente sin respuesta configurada
    está *no disponible*. Registra en orden qué fuentes se pidieron."""

    respuestas: Mapping[str, ResultadoFuente]
    consultado_en: datetime = MOMENTO_FIJO
    pedidas: list[str] = field(default_factory=list)

    def variables(self, fuente: str) -> ResultadoFuente:
        self.pedidas.append(fuente)
        return self.respuestas.get(fuente, NoDisponible("sin respuesta configurada"))

    def snapshots(self) -> list[Snapshot]:
        vistas = dict.fromkeys(self.pedidas)
        return [
            Snapshot(fuente, self.consultado_en, self._crudo(fuente), self.variables_sin_registrar(fuente))
            for fuente in vistas
        ]

    def variables_sin_registrar(self, fuente: str) -> ResultadoFuente:
        return self.respuestas.get(fuente, NoDisponible("sin respuesta configurada"))

    def _crudo(self, fuente: str) -> Any:
        resultado = self.variables_sin_registrar(fuente)
        return dict(resultado.variables) if isinstance(resultado, Disponible) else None
