"""Agentes de IA: asisten al analista, nunca deciden (ADR 0001).

- **Agente documental**: ``extraer_documento`` lee una constancia de ARCA o un balance y
  devuelve sus campos, cada uno con un nivel de confianza. ``variables_documentos``
  convierte lo extraído en las variables de la fuente ``documentos`` que usan las reglas.
- **Agente de informe**: ``redactar_informe`` explica una evaluación y ``responder``
  contesta preguntas del analista sobre ella.

Lo que el agente devuelve se valida en código: un valor con formato inválido queda como
*sin dato* con confianza 0. La confianza baja no la resuelve el agente sino las reglas
(``documentos.confianza_minima``), que derivan el caso.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from relevo import identificacion
from relevo.clientes_claude import AgenteNoDisponible, ClienteClaude
from relevo.consultor import Disponible, NoDisponible, SinAntecedentes
from relevo.dominio import Evaluacion

TIPOS_DOCUMENTO = ("constancia_arca", "balance", "otro")
CAMPOS_POR_TIPO = {
    "constancia_arca": ("cuit", "denominacion", "categoria_monotributo", "fecha_inicio_actividades"),
    "balance": ("cuit", "denominacion", "fecha_cierre_ejercicio", "ventas_anuales"),
    "otro": ("cuit", "denominacion"),
}
# variables de la fuente `documentos` que usan las reglas de cada segmento
VARIABLES_POR_SEGMENTO = {
    "pyme": ("ventas_anuales",),
    "monotributista": ("categoria_monotributo", "antiguedad_actividad_anios"),
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


# --- agente documental -------------------------------------------------------

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


def _anios(fecha_iso: str, hoy: date) -> float:
    return round((hoy - date.fromisoformat(fecha_iso)).days / 365.25, 1)


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


# --- agente de informe -------------------------------------------------------

SISTEMA_INFORME = """\
Sos el agente de informe de un motor de decisión de riesgo crediticio para PyMEs y \
monotributistas argentinos. Le explicás a un analista de riesgo cómo llegó el motor a su \
veredicto.

Reglas:
- El veredicto lo calculó un motor determinístico y vos no lo cambiás ni lo ponés en duda: \
lo explicás. Si el analista no está de acuerdo, puede registrar una resolución aparte.
- Usá solo los datos de la evaluación que te pasan. No inventes cifras, fuentes ni \
antecedentes. Si un dato falta, decilo.
- Una fuente "sin antecedentes" es un dato (no tiene historia), no una falla. Una fuente \
"no disponible" sí es una falla.
- Escribí en español rioplatense, claro y concreto, en texto plano (sin markdown)."""

PROMPT_INFORME = """\
Redactá el informe de riesgo de esta evaluación, con estas cuatro secciones (títulos en \
mayúsculas, una línea en blanco entre secciones, viñetas con "- "):

RESUMEN: veredicto, rating, PD y oferta en dos o tres frases.
FUNDAMENTOS: qué reglas y variables explican el veredicto, con sus valores.
ALERTAS: señales de riesgo, datos faltantes, fuentes no disponibles o desactualizadas, \
baja confianza documental.
QUÉ VERIFICAR: lo que el analista debería revisar o pedir (si el caso está derivado, qué \
tiene que decidir).

Evaluación:
{contexto}"""

PROMPT_PREGUNTA = """\
Evaluación:
{contexto}

Conversación previa con el analista:
{historial}

Pregunta del analista:
{pregunta}

Respondé la pregunta en pocas frases, con los datos de la evaluación."""


def redactar_informe(evaluacion: Evaluacion, cliente: ClienteClaude) -> str:
    return cliente.completar(
        PROMPT_INFORME.format(contexto=contexto_evaluacion(evaluacion)), [], SISTEMA_INFORME
    ).strip()


@dataclass(frozen=True)
class Turno:
    rol: str  # analista | agente
    texto: str


def responder(evaluacion: Evaluacion, historial: Sequence[Turno], pregunta: str, cliente: ClienteClaude) -> str:
    previa = "\n".join(f"{t.rol}: {t.texto}" for t in historial) or "(ninguna)"
    prompt = PROMPT_PREGUNTA.format(contexto=contexto_evaluacion(evaluacion), historial=previa, pregunta=pregunta)
    return cliente.completar(prompt, [], SISTEMA_INFORME).strip()


def contexto_evaluacion(evaluacion: Evaluacion) -> str:
    """La evaluación como JSON para el agente: lo que ve el analista, sin las respuestas crudas."""
    fuentes = {}
    for snapshot in evaluacion.snapshots:
        match snapshot.resultado:
            case Disponible(variables=variables):
                resultado: dict[str, Any] = {"resultado": "disponible", "variables": dict(variables)}
            case NoDisponible(motivo=motivo):
                resultado = {"resultado": "no disponible", "motivo": motivo}
            case SinAntecedentes():
                resultado = {"resultado": "sin antecedentes"}
        if snapshot.nota:
            resultado["nota"] = snapshot.nota
        resultado["origen"] = snapshot.origen
        fuentes[snapshot.fuente] = resultado
    datos = {
        "solicitud": asdict(evaluacion.solicitud),
        "version_reglas": evaluacion.version_reglas,
        "veredicto": evaluacion.veredicto.value,
        "motivos": list(evaluacion.motivos),
        "puntos": evaluacion.puntos,
        "pd": evaluacion.pd,
        "rating": evaluacion.rating,
        "oferta": asdict(evaluacion.oferta) if evaluacion.oferta else None,
        "traza": [asdict(paso) for paso in evaluacion.traza],
        "fuentes": fuentes,
        "fuentes_no_consultadas": list(evaluacion.fuentes_no_consultadas),
    }
    return json.dumps(datos, ensure_ascii=False, indent=1, default=str)


__all__ = [
    "AgenteNoDisponible",
    "CampoExtraido",
    "DocumentoExtraido",
    "RespuestaInvalida",
    "Turno",
    "contexto_evaluacion",
    "documento_manual",
    "extraer_documento",
    "redactar_informe",
    "responder",
    "variables_documentos",
]
