"""Chat with your data: preguntas sobre evaluaciones, cartera y políticas (ADR 0008).

Cada pregunta pasa por tres pasos, y el agente nunca hace cuentas (ADR 0001):

1. **Interpretar**: el agente traduce la pregunta a un pedido de un catálogo cerrado de
   análisis (``Plan``), en JSON. Si no entra en el catálogo, lo dice.
2. **Ejecutar**: el código valida el pedido y corre el análisis, que es determinístico
   (``monitoreo``, ``simulador``, resumen de evaluaciones).
3. **Explicar**: el agente recibe el resultado y lo explica. La pantalla dibuja las tablas
   desde el resultado, no desde el texto del agente.

Para simular, el agente puede proponer cambios a una copia de las reglas vigentes (con las
operaciones de ``simulador.aplicar_cambios``). Nunca las publica: lo más que puede pasar es
que el analista guarde esos cambios como borrador y los publique después desde Backoffice.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from relevo.cartera import CarteraInvalida
from relevo.clientes_claude import ClienteClaude
from relevo.dominio import Veredicto
from relevo.monitoreo import Grupo, Informe
from relevo.reglas import ReglasInvalidas
from relevo.repositorio import EvaluacionGuardada
from relevo.simulador import CambioInvalido, Simulacion

ANALISIS = ("monitoreo", "simulacion", "evaluaciones", "ninguno")
ORIGENES = ("cambios", "borrador", "version")


class DatosDelChat(Protocol):
    """Lo que el chat puede leer y calcular. Lo implementa la capa que compone la app."""

    def flujos(self) -> Mapping[str, str]: ...  # id -> nombre

    def monitoreo(self) -> Informe: ...

    def simular(self, flujo: str, origen: str, operaciones: Sequence[Mapping], version: int | None) -> Simulacion: ...

    def evaluaciones(self) -> list[EvaluacionGuardada]: ...

    def contexto_reglas(self, flujo: str) -> str: ...  # reglas vigentes y catálogo de variables


@dataclass(frozen=True)
class Plan:
    analisis: str  # monitoreo | simulacion | evaluaciones | ninguno
    flujo: str | None = None
    origen: str | None = None  # simulación: de dónde salen las reglas candidatas (cambios, borrador, version)
    version: int | None = None
    cambios: tuple[dict[str, Any], ...] = ()
    motivo: str | None = None  # por qué no hay análisis

    def a_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, (), [])}


@dataclass(frozen=True)
class Turno:
    rol: str  # analista | agente
    texto: str
    plan: dict[str, Any] | None = None  # el pedido que resolvió esa respuesta


@dataclass(frozen=True)
class Respuesta:
    texto: str
    plan: Plan
    resultado: Informe | Simulacion | dict[str, Any] | None = None
    error: str | None = None
    datos: dict[str, Any] = field(default_factory=dict)  # lo que vio el agente


# --- 1. interpretar ----------------------------------------------------------

SISTEMA_PLAN = """\
Sos el planificador de un chat sobre los datos de un motor de decisión de riesgo crediticio \
para PyMEs y monotributistas argentinos. No respondés la pregunta: la traducís a un pedido de \
análisis, en JSON, y nada más que el JSON (sin texto antes ni después, sin markdown)."""

PROMPT_PLAN = """\
Análisis disponibles (elegí uno):

- "monitoreo": cómo se comporta la cartera otorgada contra la PD del scorecard: default \
esperado contra observado (O/E) por rating, cohorte de desembolso, segmento y versión de \
reglas; mora temprana; alertas; embudo otorgadas → convertidas → desembolsadas.
  {{"analisis": "monitoreo"}}

- "evaluaciones": qué decidió el motor: solicitudes por veredicto, segmento y rating, motivos \
de rechazo y derivación más frecuentes, montos pedidos y ofrecidos.
  {{"analisis": "evaluaciones"}}

- "simulacion": qué habría pasado con otras reglas en un flujo. Vuelve a correr el motor sobre \
las evaluaciones guardadas (sin consultar fuentes) y compara con las reglas vigentes. Las \
reglas candidatas salen de:
  - cambios a una copia de las vigentes: {{"analisis": "simulacion", "flujo": "pyme", \
"origen": "cambios", "cambios": [...]}}
  - el borrador del editor de reglas: {{"analisis": "simulacion", "flujo": "pyme", "origen": "borrador"}}
  - una versión publicada: {{"analisis": "simulacion", "flujo": "pyme", "origen": "version", "version": 2}}

  Operaciones para "cambios" (sobre las reglas vigentes que están abajo):
  - {{"op": "poner", "ruta": "politica.rechazar_ratings", "valor": ["D", "E"]}} (cualquier \
clave de politica o scorecard, con la ruta separada por puntos)
  - {{"op": "cambiar_regla", "seccion": "killers", "id": "<id>", "campos": {{"condicion": "..."}}}}
  - {{"op": "agregar_regla", "seccion": "killers" | "derivaciones", "regla": {{"id": "...", \
"descripcion": "...", "condicion": "...", "si_no_disponible": "continuar"}}, "despues_de": "<id opcional>"}}
  - {{"op": "quitar_regla", "seccion": "killers" | "derivaciones", "id": "<id>"}}
  Las condiciones usan solo las variables del catálogo del flujo, con comparaciones, and/or/not, \
+ - * / y listas de constantes. Si el analista pide algo sobre lo que ya simuló ("y si además…"), \
partí de los cambios del pedido anterior y sumá los nuevos.

- "ninguno": la pregunta no necesita datos (un saludo, una aclaración sobre la respuesta \
anterior) o no entra en ningún análisis. {{"analisis": "ninguno", "motivo": "..."}}

Flujos: {flujos}

{reglas}

Conversación previa (con el pedido que resolvió cada respuesta):
{historial}

Pregunta del analista:
{pregunta}
{error}
Devolvé solo el JSON del pedido."""


def planificar(
    pregunta: str,
    historial: Sequence[Turno],
    datos: DatosDelChat,
    cliente: ClienteClaude,
    error: str | None = None,
) -> Plan:
    flujos = datos.flujos()
    prompt = PROMPT_PLAN.format(
        flujos=", ".join(f"{id} ({nombre})" for id, nombre in flujos.items()),
        reglas="\n\n".join(datos.contexto_reglas(f) for f in flujos),
        historial=_historial(historial, con_plan=True),
        pregunta=pregunta,
        error=f"\nEl pedido anterior falló: {error}. Corregilo.\n" if error else "",
    )
    return leer_plan(cliente.completar(prompt, [], SISTEMA_PLAN), flujos)


def leer_plan(texto: str, flujos: Mapping[str, str]) -> Plan:
    """El JSON del agente como ``Plan``, validado. Si no se puede leer, es un plan "ninguno"."""
    inicio, fin = texto.find("{"), texto.rfind("}")
    try:
        datos = json.loads(texto[inicio : fin + 1]) if inicio >= 0 else None
    except json.JSONDecodeError:
        datos = None
    if not isinstance(datos, dict) or datos.get("analisis") not in ANALISIS:
        return Plan("ninguno", motivo="no pude interpretar la pregunta como un análisis")
    analisis = datos["analisis"]
    if analisis != "simulacion":
        return Plan(analisis, motivo=str(datos["motivo"]) if datos.get("motivo") else None)
    flujo, origen = datos.get("flujo"), datos.get("origen") or "cambios"
    if flujo not in flujos:
        return Plan("ninguno", motivo=f"no hay un flujo {flujo!r}")
    if origen not in ORIGENES:
        return Plan("ninguno", motivo=f"las reglas candidatas no pueden salir de {origen!r}")
    cambios = datos.get("cambios") or []
    if not isinstance(cambios, list) or not all(isinstance(c, dict) for c in cambios):
        return Plan("ninguno", motivo="los cambios tienen que ser una lista de operaciones")
    version = datos.get("version")
    return Plan(
        "simulacion",
        flujo=flujo,
        origen=origen,
        version=int(version) if isinstance(version, int | str) and str(version).isdigit() else None,
        cambios=tuple(cambios),
    )


# --- 2. ejecutar ---------------------------------------------------------------


class SinCambios(ValueError):
    pass


def ejecutar(plan: Plan, datos: DatosDelChat) -> Informe | Simulacion | dict[str, Any] | None:
    """Corre el análisis. Lanza ``CambioInvalido``, ``ReglasInvalidas`` o ``CarteraInvalida``."""
    match plan.analisis:
        case "monitoreo":
            return datos.monitoreo()
        case "evaluaciones":
            return resumen_evaluaciones(datos.evaluaciones())
        case "simulacion":
            assert plan.flujo is not None
            if plan.origen == "cambios" and not plan.cambios:
                raise SinCambios("no hay cambios para simular")
            return datos.simular(plan.flujo, plan.origen, plan.cambios, plan.version)
    return None


def resumen_evaluaciones(evaluaciones: Sequence[EvaluacionGuardada]) -> dict[str, Any]:
    """Lo que decidió el motor, por segmento: la última evaluación de cada solicitud."""
    resumen: dict[str, Any] = {}
    for segmento in sorted({g.evaluacion.solicitud.segmento for g in evaluaciones}):
        del_segmento = [g.evaluacion for g in evaluaciones if g.evaluacion.solicitud.segmento == segmento]
        aprobadas = [e for e in del_segmento if e.veredicto is Veredicto.APROBADO]
        motivos = Counter(
            m.split(":", 1)[0] for e in del_segmento if e.veredicto is not Veredicto.APROBADO for m in e.motivos
        )
        resumen[segmento] = {
            "solicitudes": len(del_segmento),
            "por_veredicto": dict(Counter(e.veredicto.value for e in del_segmento)),
            "tasa_aprobacion": round(len(aprobadas) / len(del_segmento), 4),
            "aprobadas_por_rating": dict(sorted(Counter(e.rating for e in aprobadas if e.rating).items())),
            "motivos_mas_frecuentes": dict(motivos.most_common(10)),
            "monto_pedido_promedio": round(sum(e.solicitud.monto for e in del_segmento) / len(del_segmento)),
            "monto_ofrecido_promedio": round(sum(e.oferta.monto for e in aprobadas if e.oferta) / len(aprobadas))
            if aprobadas
            else None,
            "versiones_de_reglas": dict(Counter(e.version_reglas for e in del_segmento)),
        }
    return resumen


# --- 3. explicar -----------------------------------------------------------------

SISTEMA_EXPLICAR = """\
Sos el agente de "Chat with your data" de un motor de decisión de riesgo crediticio para \
PyMEs y monotributistas argentinos. Le contestás a un analista de riesgo con el resultado de \
un análisis que ya se calculó.

Reglas:
- No hacés cuentas nuevas: citás las cifras del resultado. Si te piden una cifra que no está, \
decí que no la tenés y qué análisis habría que correr.
- No decidís ni publicás reglas: podés sugerir qué revisar o qué simular, pero la decisión es \
del analista. Una simulación nunca cambia las reglas vigentes; si al analista le sirve, puede \
guardarla como borrador y publicarla desde Backoffice.
- Monitoreo: la PD del scorecard es a 12 meses y casi ningún crédito tiene 12 meses de \
historia, así que cada crédito aporta su PD llevada a los meses observados (hazard constante). \
O/E mayor que 1 es que el scorecard subestima el riesgo, y es señal solo si el piso del \
intervalo supera 1. La mora temprana (30+ días en 3 meses) anticipa el deterioro de las \
cohortes nuevas.
- Simulación: el motor se volvió a correr sobre los datos ya consultados, sin consultar \
fuentes. Las solicitudes "no evaluables" necesitaban una fuente que no se consultó en su \
momento y quedaron fuera de la comparación: mencionalas si son muchas. De "los que salen" hay \
default real observado si tuvieron crédito. "Los que entran" nunca tuvieron crédito: solo hay \
PD estimada, y decilo. La PD corregida multiplica la del scorecard por el O/E de su rating en \
el monitoreo. Los cambios de una simulación son acumulativos: si al sumar un cambio el \
resultado es igual al de la simulación anterior, ese cambio no afecta a ninguna solicitud de \
la base; decilo así.
- Escribí en español rioplatense, claro y concreto, en texto plano (sin markdown), en pocas \
frases o viñetas con "- "."""

PROMPT_EXPLICAR = """\
Análisis: {analisis}
Resultado:
{resultado}

Conversación previa:
{historial}

Pregunta del analista:
{pregunta}

Respondé la pregunta con el resultado."""


def explicar(
    pregunta: str, historial: Sequence[Turno], plan: Plan, datos: dict[str, Any] | None, cliente: ClienteClaude
) -> str:
    prompt = PROMPT_EXPLICAR.format(
        analisis=json.dumps(plan.a_dict(), ensure_ascii=False),
        resultado=json.dumps(datos, ensure_ascii=False, separators=(",", ":")) if datos else "(no hay resultado)",
        historial=_historial(historial, con_plan=False),
        pregunta=pregunta,
    )
    return cliente.completar(prompt, [], SISTEMA_EXPLICAR).strip()


# --- los tres pasos ----------------------------------------------------------------


def responder(pregunta: str, historial: Sequence[Turno], datos: DatosDelChat, cliente: ClienteClaude) -> Respuesta:
    """Interpreta, ejecuta y explica. Si el pedido falla (un cambio que no se puede aplicar,
    reglas inválidas), le pide al agente que lo corrija una vez."""
    plan = planificar(pregunta, historial, datos, cliente)
    resultado, error = None, None
    for intento in range(2):
        try:
            resultado = ejecutar(plan, datos)
            error = None
            break
        except (CambioInvalido, ReglasInvalidas, SinCambios) as e:
            error = str(e)
            if intento == 0:
                plan = planificar(pregunta, historial, datos, cliente, error=error)
        except CarteraInvalida as e:
            error = f"no se pudo leer la cartera: {e}"
            break
    contexto = a_datos(resultado) if resultado is not None else ({"error": error} if error else None)
    if plan.analisis == "ninguno" and plan.motivo:
        contexto = {"sin_analisis": plan.motivo}
    texto = explicar(pregunta, historial, plan, contexto, cliente)
    return Respuesta(texto, plan, resultado, error, contexto or {})


def a_datos(resultado: Informe | Simulacion | dict[str, Any]) -> dict[str, Any]:
    """El resultado como JSON para el agente, con los cocientes ya calculados y redondeados."""
    if isinstance(resultado, Informe):
        return _informe(resultado)
    if isinstance(resultado, Simulacion):
        return _simulacion(resultado)
    return resultado


def _grupo(g: Grupo) -> dict[str, Any]:
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


def _informe(informe: Informe) -> dict[str, Any]:
    return {
        "corte_de_la_cartera": informe.corte.isoformat() if informe.corte else None,
        "embudo": asdict(informe.embudo),
        "total": _grupo(informe.total),
        "por_rating": [_grupo(g) for g in informe.por_rating],
        "por_cohorte_de_desembolso": [_grupo(g) for g in informe.por_cohorte],
        "por_segmento": [_grupo(g) for g in informe.por_segmento],
        "por_version_de_reglas": [_grupo(g) for g in informe.por_version],
        "por_rating_y_cohorte": {r: [_grupo(g) for g in gs] for r, gs in informe.por_rating_cohorte.items()},
        "alertas": [asdict(a) for a in informe.alertas],
    }


def _simulacion(s: Simulacion) -> dict[str, Any]:
    def redondear(valor: Any) -> Any:
        if isinstance(valor, float):
            return round(valor, 4)
        if isinstance(valor, dict):
            return {k: redondear(v) for k, v in valor.items()}
        return valor

    def libro(libro) -> dict[str, Any]:
        datos = redondear(asdict(libro))
        datos["tasa_aprobacion"] = round(libro.tasa_aprobacion, 4) if libro.tasa_aprobacion is not None else None
        return datos

    def movimiento(m) -> dict[str, Any]:
        return {
            "solicitudes": m.solicitudes,
            "pd_promedio": redondear(m.pd_promedio),
            "pd_corregida_promedio": redondear(m.pd_corregida_promedio),
            "con_credito_real": _grupo(m.con_credito) if m.con_credito else None,
        }

    return {
        "flujo": s.flujo,
        "cambios_respecto_de_la_vigente": [asdict(c) for c in s.cambios],
        "vigente": libro(s.vigente),
        "candidata": libro(s.candidata),
        "veredictos_vigente_a_candidata": [
            {"vigente": a, "candidata": b, "solicitudes": n} for (a, b), n in s.matriz.items()
        ],
        "salen": movimiento(s.salen),
        "entran": movimiento(s.entran),
        "no_evaluables_por_fuente_no_consultada": s.no_evaluables,
        "factores_de_correccion_o_sobre_e": redondear(s.factores),
    }


def _historial(historial: Sequence[Turno], con_plan: bool) -> str:
    lineas = []
    for t in historial:
        lineas.append(f"{t.rol}: {t.texto}")
        if con_plan and t.plan:
            lineas.append(f"(pedido: {json.dumps(t.plan, ensure_ascii=False)})")
    return "\n".join(lineas) or "(ninguna)"
