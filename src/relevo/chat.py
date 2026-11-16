"""Chat con tus datos: el agente de informe y el de cartera (producto aparte, ADR 0008).

``redactar_informe`` explica una evaluación y ``responder`` contesta preguntas del analista
sobre ella. ``responder_cartera`` contesta preguntas sobre el monitoreo de la cartera, con
los números que ya calculó ``monitoreo`` (el agente no hace cuentas).

Lee del motor y nunca escribe en él: puede explicar, no decidir (ADR 0001). Nada del motor
ni de la lectura de documentos importa este módulo ni ``monitoreo``; un test lo controla.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from relevo.clientes_claude import AgenteNoDisponible, ClienteClaude
from relevo.consultor import Disponible, NoDisponible, SinAntecedentes
from relevo.dominio import Evaluacion
from relevo.monitoreo import Grupo, Informe

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


# --- agente de cartera (monitoreo) -------------------------------------------

SISTEMA_CARTERA = """\
Sos el agente de monitoreo de cartera de un motor de decisión de riesgo crediticio para \
PyMEs y monotributistas argentinos. Le explicás a un analista de riesgo cómo se comporta la \
cartera otorgada contra lo que esperaba el scorecard.

Reglas:
- Los números ya están calculados y vos no hacés cuentas nuevas: citás los del monitoreo. \
Si te piden un corte o una cifra que no está, decí que no la tenés y qué habría que calcular.
- No cambiás reglas ni decidís: podés sugerir qué revisar (recalibrar un rating, endurecer \
un corte, mirar una cohorte), pero la decisión es del analista.
- Cómo leer el monitoreo: la PD del scorecard es a 12 meses y casi ningún crédito tiene 12 \
meses de historia. Por eso cada crédito aporta su PD llevada a los meses que lleva \
observados, suponiendo hazard constante: los "esperados" (E) son esa suma y los \
"observados" (O) son los créditos con 90+ días de mora en sus primeros 12 meses. O/E mayor \
que 1 quiere decir que el scorecard subestima el riesgo. Solo es una señal si el piso del \
intervalo de confianza supera 1. Los créditos con menos de 3 meses no entran porque todavía \
no pueden llegar a 90 días.
- La mora temprana (30+ días en los primeros 3 meses) anticipa el deterioro de las cohortes \
nuevas, que todavía no maduraron.
- El supuesto de hazard constante es una aproximación: si el default se concentra en los \
primeros meses, el O/E de las cohortes jóvenes sale inflado. Mencionalo si es relevante.
- Escribí en español rioplatense, claro y concreto, en texto plano (sin markdown)."""

PROMPT_CARTERA = """\
Monitoreo de la cartera:
{contexto}

Conversación previa con el analista:
{historial}

Pregunta del analista:
{pregunta}

Respondé en pocas frases, con los números del monitoreo."""


def responder_cartera(informe: Informe, historial: Sequence[Turno], pregunta: str, cliente: ClienteClaude) -> str:
    previa = "\n".join(f"{t.rol}: {t.texto}" for t in historial) or "(ninguna)"
    prompt = PROMPT_CARTERA.format(contexto=contexto_cartera(informe), historial=previa, pregunta=pregunta)
    return cliente.completar(prompt, [], SISTEMA_CARTERA).strip()


def contexto_cartera(informe: Informe) -> str:
    """El monitoreo como JSON para el agente, con los cocientes ya calculados y redondeados."""

    def grupo(g: Grupo) -> dict[str, Any]:
        return {
            "corte": g.clave,
            "creditos": g.creditos,
            "meses_observados_promedio": round(g.meses_promedio, 1),
            "pd_12m_promedio": round(g.pd_12m, 4),
            "default_esperado": round(g.esperados, 1),
            "default_observado": g.observados,
            "o_sobre_e": round(g.ratio, 2) if g.ratio is not None else None,
            "ic95_o_sobre_e": [round(x, 2) for x in g.ic] if g.ic else None,
            "mora_temprana": round(g.tasa_mora_temprana, 4) if g.tasa_mora_temprana is not None else None,
            "mora_temprana_creditos": g.mora_temprana_base,
            "estado": g.estado,
        }

    datos = {
        "corte_de_la_cartera": informe.corte.isoformat() if informe.corte else None,
        "embudo": asdict(informe.embudo),
        "total": grupo(informe.total),
        "por_rating": [grupo(g) for g in informe.por_rating],
        "por_cohorte_de_desembolso": [grupo(g) for g in informe.por_cohorte],
        "por_segmento": [grupo(g) for g in informe.por_segmento],
        "por_version_de_reglas": [grupo(g) for g in informe.por_version],
        "por_rating_y_cohorte": {r: [grupo(g) for g in gs] for r, gs in informe.por_rating_cohorte.items()},
        "alertas": [asdict(a) for a in informe.alertas],
    }
    return json.dumps(datos, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "AgenteNoDisponible",
    "Turno",
    "contexto_cartera",
    "contexto_evaluacion",
    "redactar_informe",
    "responder",
    "responder_cartera",
]
