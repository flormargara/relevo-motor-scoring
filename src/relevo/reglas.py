"""Reglas versionadas por segmento (ADR 0002).

``cargar_reglas`` lee ``rules/<segmento>.v<N>.yaml`` y el modelo del scorecard
que referencia (``modelos/<archivo>.json``), y valida todo al cargar: un YAML
mal formado falla acá y nunca en medio de una evaluación.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from relevo.condiciones import CondicionInvalida, Expresion

RAIZ = Path(__file__).resolve().parents[2]
DIRECTORIO_REGLAS = RAIZ / "rules"
DIRECTORIO_MODELOS = RAIZ / "modelos"

ACCIONES_NO_DISPONIBLE = ("continuar", "derivar", "rechazar")
VARIABLES_SOLICITUD = ("cuit", "cuit_valido", "tipo_persona", "segmento", "monto", "plazo_meses")
TIPOS_CAPACIDAD_PAGO = ("porcentaje_ventas", "cuota_ingreso")


class ReglasInvalidas(ValueError):
    """Las reglas o el modelo del scorecard no respetan el formato."""


@dataclass(frozen=True)
class Regla:
    """Un killer o una derivación."""

    id: str
    descripcion: str
    condicion: Expresion
    si_no_disponible: str = "continuar"


@dataclass(frozen=True)
class Tramo:
    puntos: float
    hasta: float | None = None  # el valor cae en el tramo si es <= hasta
    valor: Any = None  # o si es igual a valor
    por_valor: bool = False

    def contiene(self, x: Any) -> bool:
        if self.por_valor:
            return x == self.valor and type(x) is type(self.valor)
        return self.hasta is None or x <= self.hasta


@dataclass(frozen=True)
class Caracteristica:
    nombre: str
    expresion: Expresion
    tramos: tuple[Tramo, ...]
    sin_dato: float

    def puntos(self, valor: Any) -> float:
        if valor is None:
            return self.sin_dato
        for tramo in self.tramos:
            if tramo.contiene(valor):
                return tramo.puntos
        return self.sin_dato


@dataclass(frozen=True)
class Scorecard:
    modelo: str
    provisional: bool
    puntos_base: float
    odds_base: float
    pdo: float
    caracteristicas: tuple[Caracteristica, ...]
    si_no_disponible: str = "continuar"

    def pd(self, puntos: float) -> float:
        """Escala estándar: odds (buenos/malos) se duplican cada `pdo` puntos."""
        odds = self.odds_base * 2 ** ((puntos - self.puntos_base) / self.pdo)
        return 1 / (1 + odds)


@dataclass(frozen=True)
class Rating:
    nombre: str
    pd_hasta: float


@dataclass(frozen=True)
class CapacidadPago:
    tipo: str
    porcentaje_cuota: float
    variable: Expresion  # ventas anuales (PyME) o categoría de monotributo
    ingreso_anual_por_categoria: dict[str, float] | None = None


@dataclass(frozen=True)
class Politica:
    rechazar_ratings: tuple[str, ...]
    derivar_ratings: tuple[str, ...]
    tasa_base: Expresion
    spread_por_rating: dict[str, float]
    capacidad_pago: CapacidadPago
    monto_minimo: float
    redondeo_monto: float
    plazo_minimo: int
    plazo_maximo: int
    si_no_disponible: str = "derivar"


@dataclass(frozen=True)
class Reglas:
    segmento: str
    version: int
    killers: tuple[Regla, ...]
    derivaciones: tuple[Regla, ...]
    scorecard: Scorecard
    ratings: tuple[Rating, ...]
    politica: Politica

    @property
    def id(self) -> str:
        return f"{self.segmento}.v{self.version}"

    @property
    def _expresiones(self) -> list[Expresion]:
        return [
            *(r.condicion for r in self.killers),
            *(r.condicion for r in self.derivaciones),
            *(c.expresion for c in self.scorecard.caracteristicas),
            self.politica.tasa_base,
            self.politica.capacidad_pago.variable,
        ]

    @property
    def fuentes(self) -> tuple[str, ...]:
        """Las fuentes que estas reglas pueden llegar a consultar, en orden de uso."""
        fuentes = (f for e in self._expresiones for f in e.fuentes if f != "solicitud")
        return tuple(dict.fromkeys(fuentes))

    @property
    def variables(self) -> tuple[str, ...]:
        """Todas las variables (``fuente.variable``) que usan estas reglas, sin repetir."""
        return tuple(dict.fromkeys(f"{f}.{v}" for e in self._expresiones for f, v in e.referencias))

    def rating_para(self, pd: float) -> str:
        return next(r.nombre for r in self.ratings if pd <= r.pd_hasta)

    def con_killer(self, id: str, **cambios: Any) -> Reglas:
        """Copia con un killer modificado (útil para probar variantes de política)."""
        killers = tuple(replace(k, **cambios) if k.id == id else k for k in self.killers)
        return replace(self, killers=killers)

    def con_politica(self, **cambios: Any) -> Reglas:
        return replace(self, politica=replace(self.politica, **cambios))


# --- carga ----------------------------------------------------------------


def cargar_reglas(
    segmento: str,
    version: int | None = None,
    directorio_reglas: Path = DIRECTORIO_REGLAS,
    directorio_modelos: Path = DIRECTORIO_MODELOS,
) -> Reglas:
    """Carga las reglas de un segmento: la versión pedida o, si no se pide, la última."""
    versiones = {}
    for archivo in directorio_reglas.glob(f"{segmento}.v*.yaml"):
        coincidencia = re.fullmatch(rf"{re.escape(segmento)}\.v(\d+)\.yaml", archivo.name)
        if coincidencia:
            versiones[int(coincidencia.group(1))] = archivo
    elegida = version if version is not None else max(versiones, default=None)
    if elegida not in versiones:
        detalle = f" v{version}" if version is not None else ""
        raise ReglasInvalidas(f"no hay reglas para el segmento {segmento!r}{detalle} en {directorio_reglas}")

    archivo = versiones[elegida]
    try:
        datos = yaml.safe_load(archivo.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ReglasInvalidas(f"{archivo.name}: YAML inválido: {error}") from None
    reglas = reglas_desde_dict(datos, directorio_modelos)
    if reglas.segmento != segmento or reglas.version != elegida:
        raise ReglasInvalidas(f"{archivo.name} declara la versión {reglas.version} del segmento {reglas.segmento!r}")
    return reglas


def reglas_desde_dict(datos: Any, directorio_modelos: Path = DIRECTORIO_MODELOS) -> Reglas:
    """Construye y valida las reglas a partir del contenido de un YAML."""
    d = _Lector(datos, "reglas")
    if "ratings" in d.datos:
        raise ReglasInvalidas("ratings: los define el modelo del scorecard, no las reglas")
    scorecard, ratings = _scorecard(d.dict("scorecard"), directorio_modelos)
    nombres = tuple(r.nombre for r in ratings)
    return Reglas(
        segmento=d.texto("segmento"),
        version=d.entero("version"),
        killers=_reglas(d.lista("killers"), "killers"),
        derivaciones=_reglas(d.lista("derivaciones", []), "derivaciones"),
        scorecard=scorecard,
        ratings=ratings,
        politica=_politica(d.dict("politica"), nombres),
    )


_FALTA = object()


class _Lector:
    """Acceso a un dict del YAML con mensajes de error que dicen dónde está el problema."""

    def __init__(self, datos: Any, donde: str):
        if not isinstance(datos, dict):
            raise ReglasInvalidas(f"{donde}: se esperaba un mapa")
        self.datos = datos
        self.donde = donde

    def _valor(self, clave: str, default: Any, tipos: type | tuple[type, ...], nombre_tipo: str) -> Any:
        if clave not in self.datos:
            if default is _FALTA:
                raise ReglasInvalidas(f"{self.donde}: falta {clave!r}")
            return default
        valor = self.datos[clave]
        if not isinstance(valor, tipos) or (isinstance(valor, bool) and bool not in _como_tupla(tipos)):
            raise ReglasInvalidas(f"{self.donde}.{clave}: se esperaba {nombre_tipo}, vino {valor!r}")
        return valor

    def texto(self, clave: str, default: Any = _FALTA) -> str:
        return self._valor(clave, default, str, "texto")

    def entero(self, clave: str) -> int:
        return self._valor(clave, _FALTA, int, "un entero")

    def numero(self, clave: str) -> float:
        return float(self._valor(clave, _FALTA, (int, float), "un número"))

    def lista(self, clave: str, default: Any = _FALTA) -> list:
        return self._valor(clave, default, list, "una lista")

    def dict(self, clave: str) -> dict:
        return self._valor(clave, _FALTA, dict, "un mapa")

    def sub(self, clave: str) -> _Lector:
        return _Lector(self.dict(clave), f"{self.donde}.{clave}")

    def expresion(self, clave: str) -> Expresion:
        texto = self.texto(clave)
        try:
            expresion = Expresion(texto)
        except CondicionInvalida as error:
            raise ReglasInvalidas(f"{self.donde}.{clave}: {error}") from None
        for fuente, variable in expresion.referencias:
            if fuente == "solicitud" and variable not in VARIABLES_SOLICITUD:
                raise ReglasInvalidas(f"{self.donde}.{clave}: variable desconocida: solicitud.{variable}")
        return expresion

    def accion(self, clave: str, default: str) -> str:
        accion = self.texto(clave, default)
        if accion not in ACCIONES_NO_DISPONIBLE:
            raise ReglasInvalidas(
                f"{self.donde}.{clave}: {accion!r} no es válido (opciones: {', '.join(ACCIONES_NO_DISPONIBLE)})"
            )
        return accion


def _como_tupla(tipos: type | tuple[type, ...]) -> tuple[type, ...]:
    return tipos if isinstance(tipos, tuple) else (tipos,)


def _reglas(items: list, seccion: str) -> tuple[Regla, ...]:
    reglas: list[Regla] = []
    for posicion, item in enumerate(items):
        d = _Lector(item, f"{seccion}[{posicion}]")
        regla = Regla(
            id=d.texto("id"),
            descripcion=d.texto("descripcion"),
            condicion=d.expresion("condicion"),
            si_no_disponible=d.accion("si_no_disponible", "continuar"),
        )
        if any(r.id == regla.id for r in reglas):
            raise ReglasInvalidas(f"{seccion}: id repetido {regla.id!r}")
        reglas.append(regla)
    return tuple(reglas)


def _ratings(items: list, donde: str) -> tuple[Rating, ...]:
    ratings = tuple(
        Rating(nombre=d.texto("rating"), pd_hasta=d.numero("pd_hasta"))
        for d in (_Lector(item, f"{donde}: ratings[{i}]") for i, item in enumerate(items))
    )
    if not ratings:
        raise ReglasInvalidas(f"{donde}: ratings no puede estar vacío")
    cortes = [r.pd_hasta for r in ratings]
    if cortes != sorted(cortes) or len(set(cortes)) != len(cortes):
        raise ReglasInvalidas(f"{donde}: los pd_hasta de los ratings deben ir en orden creciente")
    if cortes[-1] != 1:
        raise ReglasInvalidas(f"{donde}: el último pd_hasta de los ratings debe ser 1")
    return ratings


def _scorecard(datos: dict, directorio_modelos: Path) -> tuple[Scorecard, tuple[Rating, ...]]:
    """El scorecard y los ratings del modelo: los ratings son del modelo, no de las reglas (ADR 0007)."""
    d = _Lector(datos, "scorecard")
    nombre_modelo = d.texto("modelo")
    archivo = directorio_modelos / nombre_modelo
    if not archivo.is_file():
        raise ReglasInvalidas(f"scorecard: no existe el modelo {archivo}")
    try:
        modelo = _Lector(json.loads(archivo.read_text(encoding="utf-8")), f"modelo {nombre_modelo}")
        escala = modelo.sub("escala")
        caracteristicas = tuple(
            _caracteristica(item, f"modelo {nombre_modelo}: caracteristicas[{i}]")
            for i, item in enumerate(modelo.lista("caracteristicas"))
        )
        if not caracteristicas:
            raise ReglasInvalidas(f"modelo {nombre_modelo}: no tiene características")
        ratings = _ratings(modelo.lista("ratings"), f"modelo {nombre_modelo}")
        scorecard = Scorecard(
            modelo=nombre_modelo,
            provisional=bool(modelo.datos.get("provisional", False)),
            puntos_base=escala.numero("puntos_base"),
            odds_base=escala.numero("odds_base"),
            pdo=escala.numero("pdo"),
            caracteristicas=caracteristicas,
            si_no_disponible=d.accion("si_no_disponible", "continuar"),
        )
        return scorecard, ratings
    except json.JSONDecodeError as error:
        raise ReglasInvalidas(f"modelo {nombre_modelo}: JSON inválido: {error}") from None


def _caracteristica(item: Any, donde: str) -> Caracteristica:
    d = _Lector(item, donde)
    tramos = []
    for i, crudo in enumerate(d.lista("tramos")):
        t = _Lector(crudo, f"{donde}.tramos[{i}]")
        if "valor" in t.datos:
            tramos.append(Tramo(puntos=t.numero("puntos"), valor=t.datos["valor"], por_valor=True))
        else:
            hasta = t.numero("hasta") if "hasta" in t.datos else None
            tramos.append(Tramo(puntos=t.numero("puntos"), hasta=hasta))
    return Caracteristica(
        nombre=d.texto("nombre"),
        expresion=d.expresion("expresion"),
        tramos=tuple(tramos),
        sin_dato=d.numero("sin_dato"),
    )


def _politica(datos: dict, ratings: tuple[str, ...]) -> Politica:
    d = _Lector(datos, "politica")
    rechazar = tuple(d.lista("rechazar_ratings", []))
    derivar = tuple(d.lista("derivar_ratings", []))
    for nombre in (*rechazar, *derivar):
        if nombre not in ratings:
            raise ReglasInvalidas(f"politica: rating desconocido {nombre!r}")

    spreads = d.dict("spread_por_rating")
    for nombre in ratings:
        if nombre not in rechazar and not isinstance(spreads.get(nombre), (int, float)):
            raise ReglasInvalidas(f"politica.spread_por_rating: falta spread para el rating {nombre}")

    plazos = d.sub("plazo_meses")
    plazo_minimo, plazo_maximo = plazos.entero("minimo"), plazos.entero("maximo")
    if not 0 < plazo_minimo <= plazo_maximo:
        raise ReglasInvalidas("politica.plazo_meses: se espera 0 < minimo <= maximo")

    return Politica(
        rechazar_ratings=rechazar,
        derivar_ratings=derivar,
        tasa_base=d.expresion("tasa_base"),
        spread_por_rating={k: float(v) for k, v in spreads.items()},
        capacidad_pago=_capacidad_pago(d.sub("capacidad_pago")),
        monto_minimo=d.numero("monto_minimo"),
        redondeo_monto=d.numero("redondeo_monto") if "redondeo_monto" in d.datos else 1.0,
        plazo_minimo=plazo_minimo,
        plazo_maximo=plazo_maximo,
        si_no_disponible=d.accion("si_no_disponible", "derivar"),
    )


def _capacidad_pago(d: _Lector) -> CapacidadPago:
    tipo = d.texto("tipo")
    if tipo == "porcentaje_ventas":
        return CapacidadPago(tipo, d.numero("porcentaje_cuota"), d.expresion("ventas_anuales"))
    if tipo == "cuota_ingreso":
        tabla = d.dict("ingreso_anual_por_categoria")
        return CapacidadPago(
            tipo,
            d.numero("relacion_cuota_ingreso"),
            d.expresion("categoria"),
            {str(k): float(v) for k, v in tabla.items()},
        )
    raise ReglasInvalidas(
        f"politica.capacidad_pago.tipo: {tipo!r} no es válido (opciones: {', '.join(TIPOS_CAPACIDAD_PAGO)})"
    )
