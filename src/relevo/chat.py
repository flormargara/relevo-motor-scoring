"""Chat con tus datos: el agente de informe (producto aparte, ADR 0008).

``redactar_informe`` explica una evaluación y ``responder`` contesta preguntas del analista
sobre ella. Lee del motor (``Evaluacion``) y nunca escribe en él: puede explicar, no
decidir (ADR 0001). Nada del motor ni de la lectura de documentos importa este módulo; un
test lo controla.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from relevo.clientes_claude import AgenteNoDisponible, ClienteClaude
from relevo.consultor import Disponible, NoDisponible, SinAntecedentes
from relevo.dominio import Evaluacion

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
- Cómo leer la traza: un killer o una derivación "no evaluable" aplica la acción de la regla \
(continuar, derivar o rechazar). En el scorecard, una característica "sin dato" o "no \
evaluable" suma igual sus puntos de sin dato: esos puntos están incluidos en el total, que \
por eso puede ser más optimista o más pesimista que con el dato real.
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


__all__ = ["AgenteNoDisponible", "Turno", "contexto_evaluacion", "redactar_informe", "responder"]
