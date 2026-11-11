"""Los campos que el agente documental extrajo de los documentos de la solicitud.

Entran como una fuente más (ADR 0001 y diseño de módulos): el motor no distingue
entre datos de una API y datos de un PDF. Sin documentos cargados → sin antecedentes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from relevo.consultor import Disponible, ResultadoFuente, SinAntecedentes
from relevo.fuentes import Variable


class Documentos:
    nombre = "documentos"
    variables = {
        "ventas_anuales": Variable("pesos", "Ventas anuales según el balance"),
        "categoria_monotributo": Variable("texto", "Categoría de monotributo según la constancia"),
        "antiguedad_actividad_anios": Variable("número", "Años desde el inicio de actividades según la constancia"),
        "cuit_coincide": Variable("booleano", "El CUIT de los documentos coincide con el de la solicitud"),
        "confianza_minima": Variable("proporción", "Menor confianza entre los campos extraídos que usa el flujo"),
    }

    def __init__(self, campos: Mapping[str, Any] | None):
        self.campos = dict(campos) if campos else None

    def consultar(self, identificacion: str, timeout: float) -> dict[str, Any] | None:
        return self.campos

    def traducir(self, crudo: dict[str, Any] | None) -> ResultadoFuente:
        return Disponible(crudo) if crudo else SinAntecedentes()
