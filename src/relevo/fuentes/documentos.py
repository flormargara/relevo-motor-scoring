"""Los campos que el agente documental extrajo de los documentos de la solicitud.

Entran como una fuente más (ADR 0001 y diseño de módulos): el motor no distingue
entre datos de una API y datos de un PDF. Sin documentos cargados → sin antecedentes.

``variables_documentos`` es la traducción del lado del motor: toma lo que devuelve la
lectura de documentos (``DocumentoExtraido``, ADR 0008) y arma las variables que usan las
reglas de cada segmento. La lectura de documentos no conoce segmentos ni reglas.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from relevo import identificacion
from relevo.consultor import Disponible, ResultadoFuente, SinAntecedentes
from relevo.fuentes import Variable
from relevo.lectura_documentos import CampoExtraido, DocumentoExtraido

# variables de la fuente `documentos` que usan las reglas de cada segmento
VARIABLES_POR_SEGMENTO = {
    "pyme": ("ventas_anuales",),
    "monotributista": ("categoria_monotributo", "antiguedad_actividad_anios"),
}


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


def variables_documentos(
    documentos: Iterable[DocumentoExtraido], segmento: str, cuit: str, hoy: date
) -> dict[str, Any] | None:
    """Las variables de la fuente ``documentos`` para el motor.

    De cada variable se toma el valor con mayor confianza entre los documentos.
    ``confianza_minima`` es la menor confianza entre las variables que usa el segmento
    (0 si falta alguna). Si un documento trae un CUIT distinto al de la solicitud,
    ``cuit_coincide`` es falso y la confianza mínima pasa a 0, así el caso se deriva."""
    documentos = list(documentos)
    if not documentos:
        return None
    candidatos: dict[str, list[CampoExtraido]] = {}
    for doc in documentos:
        for nombre, campo in doc.campos.items():
            if campo.valor is None:
                continue
            if nombre == "fecha_inicio_actividades":
                nombre, campo = "antiguedad_actividad_anios", CampoExtraido(_anios(campo.valor, hoy), campo.confianza)
            candidatos.setdefault(nombre, []).append(campo)
    variables: dict[str, Any] = {}
    confianzas = []
    for nombre in VARIABLES_POR_SEGMENTO[segmento]:
        mejor = max(candidatos.get(nombre, []), key=lambda c: c.confianza, default=None)
        variables[nombre] = mejor.valor if mejor else None
        confianzas.append(mejor.confianza if mejor else 0.0)
    cuits = {c.valor for c in candidatos.get("cuit", [])}
    variables["cuit_coincide"] = all(c == identificacion.normalizar(cuit) for c in cuits) if cuits else None
    variables["confianza_minima"] = 0.0 if variables["cuit_coincide"] is False else min(confianzas)
    return variables


def _anios(fecha_iso: str, hoy: date) -> float:
    return round((hoy - date.fromisoformat(fecha_iso)).days / 365.25, 1)
