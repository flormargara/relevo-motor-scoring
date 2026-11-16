"""Lectura de documentos: el agente documental (producto aparte, ADR 0008).

``extraer_documento`` lee una constancia de ARCA o un balance y devuelve sus campos, cada
uno con un nivel de confianza. ``documento_manual`` arma lo mismo con lo que carga el
analista a mano.

Este módulo no sabe nada del motor: entra un archivo y sale un ``DocumentoExtraido``. Solo
importa el seam ``ClienteClaude``; un test lo controla. La traducción a variables del motor
vive del lado del motor, en ``relevo.fuentes.documentos``.

Lo que el agente devuelve se valida en código: un valor con formato inválido queda como
*sin dato* con confianza 0. El agente asiste, nunca decide (ADR 0001).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from relevo.clientes_claude import AgenteNoDisponible, ClienteClaude

TIPOS_DOCUMENTO = ("constancia_arca", "balance", "otro")
CAMPOS_POR_TIPO = {
    "constancia_arca": ("cuit", "denominacion", "categoria_monotributo", "fecha_inicio_actividades"),
    "balance": ("cuit", "denominacion", "fecha_cierre_ejercicio", "ventas_anuales"),
    "otro": ("cuit", "denominacion"),
}
CATEGORIAS_MONOTRIBUTO = "ABCDEFGHIJK"


class RespuestaInvalida(AgenteNoDisponible):
    """El agente respondió algo que no se puede interpretar."""


@dataclass(frozen=True)
class CampoExtraido:
    valor: Any
    confianza: float


@dataclass(frozen=True)
class DocumentoExtraido:
    archivo: str
    tipo: str
    campos: dict[str, CampoExtraido]
    observaciones: str = ""

    def a_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def desde_dict(cls, datos: Mapping[str, Any]) -> DocumentoExtraido:
        campos = {k: CampoExtraido(**v) for k, v in datos["campos"].items()}
        return cls(datos["archivo"], datos["tipo"], campos, datos.get("observaciones", ""))


SISTEMA_DOCUMENTAL = """\
Sos el agente documental de un motor de riesgo crediticio argentino. Extraés campos de \
documentos de solicitantes de crédito (constancias de inscripción de ARCA/AFIP y balances \
o estados contables). No evaluás el riesgo ni opinás sobre el crédito.

Reglas:
- Extraé solo lo que está escrito en el documento. Si un campo no aparece o no se lee, \
valor null y confianza 0. Nunca lo inventes ni lo deduzcas de otra fuente.
- La confianza (0 a 1) mide qué tan seguro estás de haber leído bien ese valor: 0.95 o más \
si está impreso y claro; menos si está borroso, es ambiguo, hay varios candidatos o tuviste \
que interpretar.
- Las observaciones son sobre el contenido del documento. No compares el CUIT con el nombre \
del archivo ni con otros datos: el sistema lo controla contra la solicitud. Un mismo CUIT \
puede aparecer con o sin guiones.
- Respondé únicamente con un objeto JSON, sin texto antes ni después."""

PROMPT_DOCUMENTAL = """\
Extraé los campos del documento adjunto ({archivo}).

Primero identificá el tipo: "constancia_arca", "balance" u "otro". Campos por tipo:
- constancia_arca: cuit (11 dígitos, sin guiones), denominacion (nombre o razón social), \
categoria_monotributo (una letra de la A a la K; null si no está adherido al monotributo), \
fecha_inicio_actividades (AAAA-MM-DD).
- balance: cuit, denominacion, fecha_cierre_ejercicio (AAAA-MM-DD, del último ejercicio), \
ventas_anuales (ventas netas o ingresos por ventas del último ejercicio, en PESOS como \
número: si el balance está expresado en miles o millones de pesos, multiplicá).
- otro: cuit, denominacion.

Formato de respuesta:
{{"tipo": "...", "campos": {{"<campo>": {{"valor": ..., "confianza": 0.0}}}}, \
"observaciones": "una o dos frases: unidad de medida, ajuste por inflación, \
inconsistencias o lo que el analista deba saber"}}"""


def extraer_documento(archivo: Path, cliente: ClienteClaude) -> DocumentoExtraido:
    respuesta = cliente.completar(PROMPT_DOCUMENTAL.format(archivo=archivo.name), [archivo], SISTEMA_DOCUMENTAL)
    datos = _json_de(respuesta)
    tipo = datos.get("tipo") if datos.get("tipo") in TIPOS_DOCUMENTO else "otro"
    crudos = datos.get("campos") if isinstance(datos.get("campos"), dict) else {}
    campos = {nombre: _validar_campo(nombre, crudos.get(nombre)) for nombre in CAMPOS_POR_TIPO[tipo]}
    observaciones = datos.get("observaciones")
    return DocumentoExtraido(archivo.name, tipo, campos, observaciones if isinstance(observaciones, str) else "")


def documento_manual(campos: Mapping[str, Any]) -> DocumentoExtraido | None:
    """Los campos que el analista carga a mano: confianza 1, y pisan a los extraídos."""
    validos = {k: _validar_campo(k, {"valor": v, "confianza": 1.0}) for k, v in campos.items() if v not in (None, "")}
    validos = {k: c for k, c in validos.items() if c.valor is not None}
    return DocumentoExtraido("carga manual", "manual", validos) if validos else None


def _validar_campo(nombre: str, crudo: Any) -> CampoExtraido:
    if not isinstance(crudo, dict):
        return CampoExtraido(None, 0.0)
    try:
        confianza = min(max(float(crudo.get("confianza", 0)), 0.0), 1.0)
    except (TypeError, ValueError):
        confianza = 0.0
    valor = _normalizar(nombre, crudo.get("valor"))
    return CampoExtraido(valor, confianza if valor is not None else 0.0)


def _normalizar(nombre: str, valor: Any) -> Any:
    if valor is None or valor == "":
        return None
    try:
        match nombre:
            case "cuit":
                digitos = re.sub(r"\D", "", str(valor))
                return digitos if len(digitos) == 11 else None
            case "categoria_monotributo":
                letra = str(valor).strip().upper()
                return letra if len(letra) == 1 and letra in CATEGORIAS_MONOTRIBUTO else None
            case "fecha_inicio_actividades" | "fecha_cierre_ejercicio":
                return date.fromisoformat(str(valor).strip()).isoformat()
            case "ventas_anuales" | "antiguedad_actividad_anios":
                numero = float(valor) if not isinstance(valor, str) else _numero_argentino(valor)
                return numero if numero >= 0 else None
            case _:
                return str(valor).strip() or None
    except (TypeError, ValueError):
        return None


def _numero_argentino(texto: str) -> float:
    """``"1.234.567,89"`` o ``"1234567.89"`` → float."""
    texto = re.sub(r"[^\d,.\-]", "", texto)
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif texto.count(".") > 1:
        texto = texto.replace(".", "")
    return float(texto)


def _json_de(texto: str) -> dict[str, Any]:
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fin < inicio:
        raise RespuestaInvalida("el agente documental no devolvió JSON")
    try:
        datos = json.loads(texto[inicio : fin + 1])
    except json.JSONDecodeError as error:
        raise RespuestaInvalida(f"el agente documental devolvió JSON inválido: {error}") from None
    if not isinstance(datos, dict):
        raise RespuestaInvalida("el agente documental no devolvió un objeto JSON")
    return datos


__all__ = [
    "AgenteNoDisponible",
    "CampoExtraido",
    "DocumentoExtraido",
    "RespuestaInvalida",
    "documento_manual",
    "extraer_documento",
]
