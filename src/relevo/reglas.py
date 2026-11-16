"""Reglas de un flujo (ADR 0002 y 0007).

``reglas_desde_dict`` construye y valida unas reglas con el modelo del scorecard que
referencian (``modelos/<archivo>.json``): unas reglas mal formadas fallan al cargarlas
y nunca en medio de una evaluación. Las versiones viven en la base (``versiones_reglas``);
``cargar_reglas`` lee los YAML de ``rules/``, que son la semilla.
"""

from __future__ import annotations

import hashlib
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
class Rating:
    nombre: str
    pd_hasta: float


@dataclass(frozen=True)
class ModeloExterno:
    """Un modelo de ML que vive en otro repositorio y se consulta como fuente (ADR 0010)."""

    modelo: str
    version: str

    @property
    def id(self) -> str:
        return f"{self.modelo}@{self.version}"


PD_EXTERNA = Expresion("modelo_ml.pd")


@dataclass(frozen=True)
class Scorecard:
    """El modelo que da la PD: un scorecard de puntos, o un modelo de ML externo (``externo``)."""

    modelo: str  # archivo del descriptor en modelos/
    provisional: bool
    puntos_base: float
    odds_base: float
    pdo: float
    caracteristicas: tuple[Caracteristica, ...]
    si_no_disponible: str = "continuar"
    externo: ModeloExterno | None = None
    ratings: tuple[Rating, ...] = ()

    @property
    def id(self) -> str:
        """Cómo se nombra en la traza: modelo@versión si es de ML, el archivo si es de puntos."""
        return self.externo.id if self.externo else self.modelo

    def rating_para(self, pd: float) -> str:
        return next(r.nombre for r in self.ratings if pd <= r.pd_hasta)

    def pd(self, puntos: float) -> float:
        """Escala estándar: odds (buenos/malos) se duplican cada `pdo` puntos."""
        odds = self.odds_base * 2 ** ((puntos - self.puntos_base) / self.pdo)
        return 1 / (1 + odds)


@dataclass(frozen=True)
class Brazo:
    """Un modelo de un split, con su peso (ADR 0010)."""

    scorecard: Scorecard
    peso: float


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
    flujo: str
    version: int
    killers: tuple[Regla, ...]
    derivaciones: tuple[Regla, ...]
    scorecard: Scorecard  # el que decide; con split, el primero del reparto
    ratings: tuple[Rating, ...]
    politica: Politica
    split: tuple[Brazo, ...] = ()  # varios modelos que se reparten las solicitudes
    sombra: tuple[Scorecard, ...] = ()  # se calculan y se guardan, no deciden
    semilla: str = ""  # del reparto del split

    @property
    def id(self) -> str:
        return f"{self.flujo}.v{self.version}"

    @property
    def decisores(self) -> tuple[Scorecard, ...]:
        """Los modelos que pueden decidir: los del split, o el scorecard."""
        return tuple(b.scorecard for b in self.split) or (self.scorecard,)

    @property
    def modelos_externos(self) -> tuple[ModeloExterno, ...]:
        """Los modelos de ML que hay que consultar: los que deciden y los que están en sombra."""
        return tuple(dict.fromkeys(s.externo for s in (*self.decisores, *self.sombra) if s.externo))

    def asignar(self, cuit: str) -> Scorecard:
        """El modelo que decide para ese solicitante. Con split, un hash del CUIT y la semilla: el mismo
        solicitante cae siempre en el mismo modelo, y la evaluación se puede reproducir."""
        if not self.split:
            return self.scorecard
        total = sum(b.peso for b in self.split)
        punto = _fraccion(f"{self.semilla}:{''.join(c for c in cuit if c.isdigit())}") * total
        for brazo in self.split:
            if punto < brazo.peso:
                return brazo.scorecard
            punto -= brazo.peso
        return self.split[-1].scorecard

    @property
    def _expresiones(self) -> list[Expresion]:
        return [
            *(r.condicion for r in self.killers),
            *(r.condicion for r in self.derivaciones),
            *(c.expresion for s in (*self.decisores, *self.sombra) for c in s.caracteristicas),
            *((PD_EXTERNA,) if self.modelos_externos else ()),
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
        return self.scorecard.rating_para(pd)

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
    if reglas.flujo != segmento or reglas.version != elegida:
        raise ReglasInvalidas(f"{archivo.name} declara la versión {reglas.version} del flujo {reglas.flujo!r}")
    return reglas


def reglas_desde_dict(datos: Any, directorio_modelos: Path = DIRECTORIO_MODELOS) -> Reglas:
    """Construye y valida las reglas a partir del contenido de un YAML."""
    d = _Lector(datos, "reglas")
    if "ratings" in d.datos:
        raise ReglasInvalidas("ratings: los define el modelo del scorecard, no las reglas")
    scorecard, split, sombra, semilla = _scorecard(d.dict("scorecard"), directorio_modelos)
    ratings = scorecard.ratings
    nombres = tuple(r.nombre for r in ratings)
    return Reglas(
        # `segmento:` es el nombre viejo de la clave, en las versiones importadas antes del ADR 0007
        flujo=d.texto("flujo" if "flujo" in d.datos else "segmento"),
        version=d.entero("version"),
        killers=_reglas(d.lista("killers"), "killers"),
        derivaciones=_reglas(d.lista("derivaciones", []), "derivaciones"),
        scorecard=scorecard,
        ratings=ratings,
        politica=_politica(d.dict("politica"), nombres),
        split=split,
        sombra=sombra,
        semilla=semilla,
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


def _scorecard(
    datos: dict, directorio_modelos: Path
) -> tuple[Scorecard, tuple[Brazo, ...], tuple[Scorecard, ...], str]:
    """El modelo que decide (o el split entre varios) y los modelos en sombra (ADR 0007 y 0010).

    Los ratings son de cada modelo, no de las reglas. Los modelos que pueden decidir tienen que
    tener los mismos ratings, porque la política decide por rating; los de sombra no."""
    d = _Lector(datos, "scorecard")
    if ("modelo" in d.datos) == ("split" in d.datos):
        raise ReglasInvalidas("scorecard: tiene que tener modelo o split (uno de los dos)")
    nombres = [d.texto("modelo")] if "modelo" in d.datos else []
    pesos: list[float] = []
    for i, item in enumerate(d.lista("split", [])):
        brazo = _Lector(item, f"scorecard.split[{i}]")
        nombres.append(brazo.texto("modelo"))
        pesos.append(brazo.numero("peso"))
    if "split" in d.datos and len(nombres) < 2:
        raise ReglasInvalidas("scorecard.split: tiene que repartir entre al menos dos modelos")
    if any(p <= 0 for p in pesos):
        raise ReglasInvalidas("scorecard.split: los pesos tienen que ser mayores que cero")
    en_sombra = d.lista("sombra", [])
    if not all(isinstance(n, str) for n in en_sombra):
        raise ReglasInvalidas("scorecard.sombra: tiene que ser una lista de modelos")
    if len(set(nombres + en_sombra)) != len(nombres) + len(en_sombra):
        raise ReglasInvalidas("scorecard: un mismo modelo aparece más de una vez entre split y sombra")

    modelos = {n: _modelo(n, directorio_modelos) for n in (*nombres, *en_sombra)}
    hay_externo = any(modelos[n].externo for n in nombres)
    si_no_disponible = d.accion("si_no_disponible", "derivar" if hay_externo else "continuar")
    if hay_externo and si_no_disponible == "continuar":
        externos = ", ".join(n for n in nombres if modelos[n].externo)
        raise ReglasInvalidas(
            f"scorecard: con un modelo externo ({externos}) si_no_disponible tiene que ser derivar o rechazar"
        )
    decisores = [replace(modelos[n], si_no_disponible=si_no_disponible) for n in nombres]
    ratings = {tuple(r.nombre for r in s.ratings) for s in decisores}
    if len(ratings) > 1:
        raise ReglasInvalidas("scorecard.split: los modelos tienen que tener los mismos ratings")
    split = tuple(Brazo(s, p) for s, p in zip(decisores, pesos, strict=False)) if pesos else ()
    semilla = d.texto("semilla", "") or ("split:" + ",".join(nombres) if split else "")
    return decisores[0], split, tuple(modelos[n] for n in en_sombra), semilla


def _modelo(nombre: str, directorio_modelos: Path) -> Scorecard:
    """Un modelo de ``modelos/``: un scorecard de puntos o un modelo de ML externo."""
    archivo = directorio_modelos / nombre
    if not archivo.is_file():
        raise ReglasInvalidas(f"scorecard: no existe el modelo {archivo}")
    try:
        modelo = _Lector(json.loads(archivo.read_text(encoding="utf-8")), f"modelo {nombre}")
    except json.JSONDecodeError as error:
        raise ReglasInvalidas(f"modelo {nombre}: JSON inválido: {error}") from None
    ratings = _ratings(modelo.lista("ratings"), f"modelo {nombre}")
    provisional = bool(modelo.datos.get("provisional", False))
    if modelo.datos.get("tipo") == "externo":
        externo = ModeloExterno(modelo=modelo.texto("modelo"), version=str(modelo.datos.get("version", "")))
        if not externo.version:
            raise ReglasInvalidas(f"modelo {nombre}: falta la versión")
        return Scorecard(nombre, provisional, 0, 1, 1, (), externo=externo, ratings=ratings)
    escala = modelo.sub("escala")
    caracteristicas = tuple(
        _caracteristica(item, f"modelo {nombre}: caracteristicas[{i}]")
        for i, item in enumerate(modelo.lista("caracteristicas"))
    )
    if not caracteristicas:
        raise ReglasInvalidas(f"modelo {nombre}: no tiene características")
    return Scorecard(
        modelo=nombre,
        provisional=provisional,
        puntos_base=escala.numero("puntos_base"),
        odds_base=escala.numero("odds_base"),
        pdo=escala.numero("pdo"),
        caracteristicas=caracteristicas,
        ratings=ratings,
    )


def _fraccion(texto: str) -> float:
    """Un número en [0, 1) determinístico para ese texto."""
    return int(hashlib.sha256(texto.encode()).hexdigest()[:12], 16) / 16**12


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
