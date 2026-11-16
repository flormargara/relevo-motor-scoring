"""Cómo se muestra una evaluación en la pantalla del caso.

Funciones puras sobre lo que ya está guardado: la ficha del solicitante, el informe del
agente partido en secciones y la traza agrupada en etapas para el diagrama de flujo. No
deciden nada: solo ordenan y dan formato.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from relevo import identificacion
from relevo.consultor import Disponible, NoDisponible, ResultadoFuente, SinAntecedentes
from relevo.dominio import Evaluacion, Paso
from relevo.reglas import Reglas

# --- formato ----------------------------------------------------------------


def _miles(numero: float, decimales: int = 0) -> str:
    texto = f"{numero:,.{decimales}f}"
    return texto.replace(",", "§").replace(".", ",").replace("§", ".")


def pesos(valor: Any, decimales: int = 0) -> str:
    return "—" if valor is None else f"$ {_miles(float(valor), decimales)}"


def porcentaje(valor: Any, decimales: int = 2) -> str:
    return "—" if valor is None else f"{_miles(float(valor) * 100, decimales)} %"


def decimal(valor: Any, decimales: int = 2) -> str:
    return "—" if valor is None else _miles(float(valor), decimales)


def numero(valor: Any) -> str:
    if isinstance(valor, bool) or valor is None:
        return {True: "sí", False: "no", None: "—"}[valor]
    if isinstance(valor, float):
        return _miles(valor, 0 if valor.is_integer() else (2 if abs(valor) >= 1 else 4))
    if isinstance(valor, int):
        return _miles(valor)
    return str(valor)


def fecha(valor: Any) -> str:
    return valor.strftime("%d/%m/%Y %H:%M") if valor else "—"


def tipo_resultado(resultado: Any) -> str:
    return {Disponible: "disponible", SinAntecedentes: "sin antecedentes", NoDisponible: "no disponible"}[
        type(resultado)
    ]


def a_json(valor: Any) -> str:
    return json.dumps(valor, ensure_ascii=False, indent=2, default=str)


def cuit_con_guiones(cuit: str) -> str:
    return f"{cuit[:2]}-{cuit[2:10]}-{cuit[10:]}" if len(cuit) == 11 and cuit.isdigit() else cuit


# --- ficha del solicitante --------------------------------------------------


@dataclass(frozen=True)
class Dato:
    etiqueta: str
    valor: str
    nota: str = ""  # de dónde salió, o por qué falta


@dataclass(frozen=True)
class Ficha:
    nombre: str | None
    datos: tuple[Dato, ...]


def ficha_solicitante(
    cuit: str,
    segmento: str,
    monto: float,
    plazo_meses: int,
    evaluacion: Evaluacion | None,
    extraidos: Iterable[Mapping[str, Any]],
) -> Ficha:
    """Los datos del solicitante, de las fuentes de la evaluación y de los documentos extraídos."""
    fuentes = {s.fuente: s.resultado for s in evaluacion.snapshots} if evaluacion else {}
    campos_doc: dict[str, Any] = {}
    for extraido in extraidos:
        for nombre, campo in extraido.get("campos", {}).items():
            if campo.get("valor") is not None and nombre not in campos_doc:
                campos_doc[nombre] = campo["valor"]

    nombre, origen = None, ""
    for fuente, variable, etiqueta in (
        ("cendeu", "denominacion", "BCRA"),
        ("igj", "razon_social", "IGJ"),
    ):
        valor = _variable(fuentes.get(fuente), variable)
        if valor:
            nombre, origen = str(valor), etiqueta
            break
    if not nombre and campos_doc.get("denominacion"):
        nombre, origen = str(campos_doc["denominacion"]), "documentos"

    tipo = identificacion.tipo_persona(cuit)
    datos = [
        Dato("Razón social" if tipo == "juridica" else "Nombre", nombre or "—", origen),
        Dato("CUIT" if tipo == "juridica" else "CUIL", cuit_con_guiones(cuit)),
        Dato(
            "Tipo",
            {"juridica": "Persona jurídica", "humana": "Persona humana"}.get(tipo or "", "—")
            + (" · PyME" if segmento == "pyme" else " · Monotributista"),
        ),
        Dato("Solicita", f"{pesos(monto)} a {plazo_meses} meses"),
    ]
    if segmento == "pyme":
        datos.append(_desde_documentos("Ventas anuales", campos_doc, "ventas_anuales", pesos))
        if campos_doc.get("fecha_cierre_ejercicio"):
            datos.append(Dato("Cierre del ejercicio", _fecha_iso(campos_doc["fecha_cierre_ejercicio"]), "balance"))
        datos.append(_desde_fuente("Antigüedad de la sociedad", fuentes, "igj", "antiguedad_anios", _anios))
    else:
        # Usar categoría de ARCA si está disponible, sino de documentos
        dato_categoria = _desde_fuente("Categoría de monotributo", fuentes, "arca", "categoria_monotributo", str)
        if dato_categoria.valor == "—":
            dato_categoria = _desde_documentos("Categoría de monotributo", campos_doc, "categoria_monotributo", str)
        datos.append(dato_categoria)
        if campos_doc.get("fecha_inicio_actividades"):
            datos.append(
                Dato("Inicio de actividades", _fecha_iso(campos_doc["fecha_inicio_actividades"]), "documentos")
            )
        edad = _desde_fuente("Edad", fuentes, "identidad", "edad", lambda v: f"{numero(v)} años")
        origen = _variable(fuentes.get("identidad"), "origen")
        if origen and edad.valor != "—":
            validada = _variable(fuentes.get("identidad"), "validada")
            edad = Dato(edad.etiqueta, edad.valor, f"identidad: {origen}" + ("" if validada else " (sin verificar)"))
        datos.append(edad)

    # Datos de ARCA para ambos segmentos
    datos += [
        _desde_fuente("Condición ante Ganancias", fuentes, "arca", "ganancias", str),
        _desde_fuente("Condición ante IVA", fuentes, "arca", "iva", str),
        _desde_fuente("Fecha del padrón ARCA", fuentes, "arca", "fecha_padron", str),
    ]
    datos += [
        _desde_fuente("Peor situación BCRA (12 meses)", fuentes, "cendeu", "peor_situacion_12m", numero),
        _desde_fuente("Deuda en el sistema", fuentes, "cendeu", "deuda_total", pesos),
        _desde_fuente("Entidades con deuda", fuentes, "cendeu", "entidades_con_deuda", numero),
        _desde_fuente("Score de bureau", fuentes, "bureau", "score", numero),
    ]
    return Ficha(nombre, tuple(datos))


def _variable(resultado: ResultadoFuente | None, variable: str) -> Any:
    return resultado.variables.get(variable) if isinstance(resultado, Disponible) else None


def _desde_fuente(etiqueta: str, fuentes: Mapping[str, ResultadoFuente], fuente: str, variable: str, formato) -> Dato:
    resultado = fuentes.get(fuente)
    match resultado:
        case None:
            return Dato(etiqueta, "—", "no consultada")
        case NoDisponible():
            return Dato(etiqueta, "—", f"{fuente}: no disponible")
        case SinAntecedentes():
            return Dato(etiqueta, "—", f"{fuente}: sin antecedentes")
    valor = _variable(resultado, variable)
    return Dato(etiqueta, formato(valor) if valor is not None else "—", fuente)


def _desde_documentos(etiqueta: str, campos: Mapping[str, Any], nombre: str, formato) -> Dato:
    valor = campos.get(nombre)
    return Dato(etiqueta, formato(valor) if valor is not None else "—", "documentos" if valor is not None else "")


def _anios(valor: Any) -> str:
    return f"{numero(round(float(valor), 1))} años"


def _fecha_iso(valor: str) -> str:
    partes = str(valor).split("-")
    return "/".join(reversed(partes)) if len(partes) == 3 else str(valor)


# --- informe ----------------------------------------------------------------


@dataclass
class Seccion:
    titulo: str | None
    bloques: list[tuple[str, Any]] = field(default_factory=list)  # ("p", texto) | ("ul", [items])


_TITULO = re.compile(r"^(?:#+\s*)?\**([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ ]{2,40})\**\s*:?\s*(.*)$")
_VINETA = re.compile(r"^\s*(?:[-•*]|\d+[.)])\s+(.*)$")


def secciones_informe(texto: str) -> list[Seccion]:
    """Parte el informe en secciones: un título es una línea en mayúsculas (``RESUMEN``,
    ``QUÉ VERIFICAR:``), las viñetas empiezan con ``-``. Tolera markdown simple."""
    secciones = [Seccion(None)]
    for linea in texto.splitlines():
        linea = linea.rstrip()
        if not linea.strip():
            continue
        titulo = _TITULO.match(linea.strip())
        if titulo and titulo.group(1).strip() == titulo.group(1).strip().upper():
            secciones.append(Seccion(titulo.group(1).strip().capitalize()))
            if titulo.group(2):
                secciones[-1].bloques.append(("p", _limpiar(titulo.group(2))))
            continue
        vineta = _VINETA.match(linea)
        bloques = secciones[-1].bloques
        if vineta:
            if not bloques or bloques[-1][0] != "ul":
                bloques.append(("ul", []))
            bloques[-1][1].append(_limpiar(vineta.group(1)))
        else:
            bloques.append(("p", _limpiar(linea.strip())))
    return [s for s in secciones if s.titulo or s.bloques]


def _limpiar(texto: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", texto).strip()


# --- traza como flujo -------------------------------------------------------

ETAPAS = (
    ("fuente", "Fuentes"),
    ("killer", "Killers"),
    ("derivacion", "Derivaciones"),
    ("scorecard", "Scorecard"),
    ("rating", "Rating"),
    ("politica", "Política"),
    ("veredicto", "Veredicto"),
)
_PROBLEMA = {"no evaluable", "no disponible", "disponible (desactualizada)"}


@dataclass(frozen=True)
class Item:
    regla: str
    descripcion: str
    resultado: str
    paso: Paso


@dataclass(frozen=True)
class Etapa:
    clave: str
    nombre: str
    estado: str  # ok | alerta | corta | no-alcanzada
    resumen: str
    items: tuple[Item, ...]


def flujo(evaluacion: Evaluacion, reglas: Reglas | None = None) -> list[Etapa]:
    """La traza agrupada por etapa, en el orden del pipeline, con el estado de cada una."""
    descripciones: dict[str, str] = {}
    if reglas is not None:
        descripciones = {r.id: r.descripcion for r in (*reglas.killers, *reglas.derivaciones)}
    etapas = []
    for clave, nombre in ETAPAS:
        pasos = [p for p in evaluacion.traza if p.etapa == clave]
        items = tuple(Item(p.regla, descripciones.get(p.regla, ""), p.resultado, p) for p in pasos)
        if not pasos:
            etapas.append(Etapa(clave, nombre, "no-alcanzada", "no se llegó: ya estaba decidida", items))
            continue
        estado, resumen = _estado(clave, pasos, evaluacion)
        etapas.append(Etapa(clave, nombre, estado, resumen, items))
    return etapas


def _estado(clave: str, pasos: list[Paso], evaluacion: Evaluacion) -> tuple[str, str]:
    resultados = [p.resultado for p in pasos]
    problemas = sum(r in _PROBLEMA for r in resultados)
    sin_dato = resultados.count("sin dato")
    match clave:
        case "fuente":
            consultadas = f"{len(pasos)} consultadas"
            return ("alerta", f"{consultadas} · {problemas} con problemas") if problemas else ("ok", consultadas)
        case "killer":
            if "salta" in resultados:
                return "corta", f"rechaza: {pasos[resultados.index('salta')].regla}"
            if evaluacion.veredicto.value == "RECHAZADO" and not _hay_etapa_despues(evaluacion, "killer"):
                return "corta", "rechaza: regla no evaluable"
            if problemas:
                return "alerta", f"{len(pasos)} evaluados · {problemas} no evaluables"
            return "ok", f"{len(pasos)} evaluados · ninguno salta"
        case "derivacion":
            saltan = resultados.count("salta")
            if saltan or problemas:
                return "alerta", f"deriva ({saltan + problemas})"
            return "ok", "ninguna salta"
        case "scorecard" if any("@" in p.regla for p in pasos):  # modelo de ML externo (ADR 0010)
            paso = pasos[-1]
            if paso.resultado != "ok":
                return "corta", f"{paso.regla}: {paso.resultado}"
            return "ok", f"modelo de ML {paso.regla} · PD {porcentaje(paso.detalle.get('valor'))}"
        case "scorecard":
            total = sum(float(p.detalle.get("puntos") or 0) for p in pasos)
            faltan = problemas + sin_dato
            resumen = f"{numero(total)} puntos" + (f" · {faltan} sin dato" if faltan else "")
            return ("alerta" if faltan else "ok"), resumen
        case "rating":
            paso = pasos[-1]
            resumen = f"{paso.resultado} · PD {porcentaje(paso.detalle.get('pd'))}"
            politica = {p.resultado for p in evaluacion.traza if p.etapa == "politica" and p.regla == "rating"}
            if "rechaza" in politica:
                return "corta", resumen
            # A–C sin observaciones; D o peor (o una derivación de la política) en alerta
            return ("alerta" if "deriva" in politica or paso.resultado >= "D" else "ok"), resumen
        case "politica":
            ultimo = pasos[-1]
            if ultimo.resultado == "ofrece":
                return ("alerta" if "deriva" in resultados else "ok"), f"ofrece {pesos(ultimo.detalle.get('monto'))}"
            if ultimo.resultado in ("rechaza", "debajo del mínimo"):
                return "corta", ultimo.resultado
            return "alerta", ultimo.resultado
        case _:
            veredicto = evaluacion.veredicto.value
            return {"APROBADO": "ok", "DERIVAR": "alerta"}.get(veredicto, "corta"), veredicto


def _hay_etapa_despues(evaluacion: Evaluacion, etapa: str) -> bool:
    orden = [clave for clave, _ in ETAPAS]
    posteriores = set(orden[orden.index(etapa) + 1 : -1])
    return any(p.etapa in posteriores for p in evaluacion.traza)
