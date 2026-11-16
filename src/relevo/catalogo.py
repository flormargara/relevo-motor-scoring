"""Catálogo de variables de cada flujo (ADR 0007).

El catálogo de un flujo son las variables de la solicitud más las que declara cada
fuente que el flujo consulta (``config/flujos.yaml``), más ``sin_antecedentes`` de
cada fuente. De ``documentos`` entran solo las que el agente documental extrae para
ese segmento. Las reglas de un flujo solo pueden usar variables de su catálogo.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

from relevo.condiciones import CondicionInvalida, Expresion
from relevo.fuentes import Variable
from relevo.fuentes.arca import Arca
from relevo.fuentes.badlar import Badlar
from relevo.fuentes.cendeu import Cendeu, Cheques
from relevo.fuentes.documentos import VARIABLES_POR_SEGMENTO, Documentos
from relevo.fuentes.identidad import IdentidadDeclarada
from relevo.fuentes.igj import Igj
from relevo.fuentes.juguete import Bureau, Propios
from relevo.fuentes.modelo_ml import ModeloML

ARCHIVO_FLUJOS = Path(__file__).resolve().parents[2] / "config" / "flujos.yaml"

_FUENTES = {
    f.nombre: f for f in (Cendeu, Cheques, Badlar, Igj, Arca, Bureau, Propios, IdentidadDeclarada, Documentos, ModeloML)
}

VARIABLES_SOLICITUD = {
    "cuit": Variable("texto", "CUIT/CUIL del solicitante"),
    "cuit_valido": Variable("booleano", "El CUIT/CUIL tiene formato y dígito verificador válidos"),
    "tipo_persona": Variable("texto", "'juridica' o 'humana', según el prefijo del CUIT"),
    "segmento": Variable("texto", "Segmento del solicitante"),
    "monto": Variable("pesos", "Monto solicitado"),
    "plazo_meses": Variable("entero", "Plazo solicitado, en meses"),
}
_DOCUMENTOS_SIEMPRE = ("cuit_coincide", "confianza_minima")


@dataclass(frozen=True)
class VariableCatalogo:
    nombre: str  # fuente.variable
    tipo: str
    descripcion: str

    @property
    def fuente(self) -> str:
        return self.nombre.split(".", 1)[0]


@dataclass(frozen=True)
class Flujo:
    id: str
    nombre: str
    segmento: str
    tipo: str  # originacion (a futuro, p. ej., comportamiento)
    fuentes: tuple[str, ...]


@cache
def flujos(archivo: Path = ARCHIVO_FLUJOS) -> dict[str, Flujo]:
    datos = yaml.safe_load(archivo.read_text(encoding="utf-8"))
    return {id: Flujo(id, d["nombre"], d["segmento"], d["tipo"], tuple(d["fuentes"])) for id, d in datos.items()}


def flujo_de_originacion(segmento: str) -> Flujo:
    """El flujo por el que entra una solicitud nueva de ese segmento."""
    for flujo in flujos().values():
        if flujo.segmento == segmento and flujo.tipo == "originacion":
            return flujo
    raise LookupError(f"no hay flujo de originación para el segmento {segmento!r} en config/flujos.yaml")


@cache
def catalogo(flujo: str) -> tuple[VariableCatalogo, ...]:
    definicion = flujos()[flujo]
    variables = [VariableCatalogo(f"solicitud.{n}", v.tipo, v.descripcion) for n, v in VARIABLES_SOLICITUD.items()]
    for fuente in definicion.fuentes:
        declaradas = _FUENTES[fuente].variables
        if fuente == "documentos":
            usadas = (*VARIABLES_POR_SEGMENTO[definicion.segmento], *_DOCUMENTOS_SIEMPRE)
            declaradas = {n: v for n, v in declaradas.items() if n in usadas}
        variables += [VariableCatalogo(f"{fuente}.{n}", v.tipo, v.descripcion) for n, v in declaradas.items()]
        sin_datos = f"La fuente {fuente} no tiene datos del solicitante"
        variables.append(VariableCatalogo(f"{fuente}.sin_antecedentes", "booleano", sin_datos))
    return tuple(variables)


@dataclass(frozen=True)
class Validacion:
    ok: bool
    error: str | None = None
    usa: tuple[str, ...] = ()


def validar_condicion(flujo: str, texto: str) -> Validacion:
    """Valida una condición contra el lenguaje y el catálogo del flujo, con un mensaje para quien edita."""
    if not texto.strip():
        return Validacion(False, "La condición está vacía.")
    try:
        expresion = Expresion(texto)
    except CondicionInvalida as error:
        return Validacion(False, error.motivo[:1].upper() + error.motivo[1:] + ".")
    nombres = [v.nombre for v in catalogo(flujo)]
    usa = tuple(f"{fuente}.{variable}" for fuente, variable in expresion.referencias)
    for nombre in usa:
        if nombre not in nombres:
            parecidas = difflib.get_close_matches(nombre, nombres, n=1, cutoff=0.6)
            sugerencia = f" ¿Quisiste decir {parecidas[0]}?" if parecidas else ""
            return Validacion(False, f"{nombre} no está en el catálogo de {flujos()[flujo].nombre}.{sugerencia}", usa)
    return Validacion(True, usa=usa)
