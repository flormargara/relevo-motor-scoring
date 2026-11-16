"""Simulador de políticas: el motor con reglas candidatas sobre lo ya consultado (parte del chat, ADR 0008).

Vuelve a correr el motor sobre la última evaluación de cada solicitud de un flujo, dos
veces: con las reglas vigentes y con las candidatas. Nunca consulta una fuente (no tiene
costo) ni escribe nada: usa los snapshots que se guardaron al evaluar. Si una regla
candidata necesita una fuente que en su momento no se consultó (por ejemplo, una paga que
la evaluación original no llegó a pedir), la solicitud queda *no evaluable* y sale de la
comparación; se informa cuántas son y por qué fuente.

Compara las dos políticas en:

- **Volumen**: aprobadas, derivadas, rechazadas, tasa de aprobación y monto ofrecido.
- **Riesgo esperado a 12 meses** de lo aprobado, con la PD del scorecard y con la PD
  corregida por el O/E que el monitoreo mide en cada rating (el scorecard puede estar
  descalibrado). Es una estimación: vale igual para las dos políticas.
- **Los que salen** (aprobados hoy que la candidata no aprueba): de los que tuvieron
  crédito, su default real observado.
- **Los que entran** (no aprobados hoy que la candidata aprueba): no tuvieron crédito, así
  que no hay mora observada; solo la PD estimada, con y sin corrección.
"""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from relevo.cartera import RegistroCartera
from relevo.consultor import NoDisponible, ResultadoFuente, Snapshot
from relevo.diff_reglas import Cambio
from relevo.dominio import Evaluacion, Veredicto
from relevo.monitoreo import Grupo, calcular
from relevo.motor import evaluar
from relevo.reglas import Reglas
from relevo.repositorio import EvaluacionGuardada, Otorgada

NO_CONSULTADA = "no se consultó en la evaluación original (el simulador no consulta fuentes)"


class ConsultorDeSnapshots:
    """``Consultor`` que responde con los snapshots de una evaluación guardada y anota las
    fuentes que se le piden y no tiene."""

    def __init__(self, snapshots: Iterable[Snapshot]):
        self._resultados = {s.fuente: s.resultado for s in snapshots}
        self.faltantes: list[str] = []

    def variables(self, fuente: str) -> ResultadoFuente:
        if fuente in self._resultados:
            return self._resultados[fuente]
        self.faltantes.append(fuente)
        return NoDisponible(NO_CONSULTADA)

    def snapshots(self) -> list[Snapshot]:
        return []


@dataclass(frozen=True)
class Libro:
    """Lo que aprueba una política."""

    reglas: str
    evaluadas: int
    aprobadas: int
    derivadas: int
    rechazadas: int
    monto: float  # suma de las ofertas aprobadas
    pd_promedio: float | None  # del scorecard, de las aprobadas
    pd_corregida_promedio: float | None  # con el O/E del monitoreo
    aprobadas_por_rating: dict[str, int]

    @property
    def tasa_aprobacion(self) -> float | None:
        return self.aprobadas / self.evaluadas if self.evaluadas else None


@dataclass(frozen=True)
class Movimiento:
    """Solicitudes que cambian de aprobada a no aprobada, o al revés."""

    solicitudes: int
    pd_promedio: float | None
    pd_corregida_promedio: float | None
    con_credito: Grupo | None  # las que tuvieron un crédito real, con su default observado


@dataclass(frozen=True)
class Simulacion:
    flujo: str
    vigente: Libro
    candidata: Libro
    cambios: list[Cambio]
    matriz: dict[tuple[str, str], int]  # (veredicto vigente, veredicto candidata) -> solicitudes
    salen: Movimiento
    entran: Movimiento
    no_evaluables: dict[str, int]  # fuente no consultada -> solicitudes
    factores: dict[str, float]  # O/E por rating con que se corrige la PD


@dataclass(frozen=True)
class _Corrida:
    solicitud_id: str
    vigente: Evaluacion
    candidata: Evaluacion


def simular(
    flujo: str,
    casos: Iterable[EvaluacionGuardada],
    vigente: Reglas,
    candidata: Reglas,
    cambios: list[Cambio],
    otorgadas: Iterable[Otorgada],
    registros: Iterable[RegistroCartera],
) -> Simulacion:
    otorgadas = list(otorgadas)
    registros = list(registros)
    factores = factores_de_correccion(calcular(otorgadas, registros).por_rating)

    corridas: list[_Corrida] = []
    no_evaluables: Counter[str] = Counter()
    for caso in casos:
        evaluacion = caso.evaluacion
        consultor = ConsultorDeSnapshots(evaluacion.snapshots)
        con_vigente = evaluar(evaluacion.solicitud, vigente, consultor)
        con_candidata = evaluar(evaluacion.solicitud, candidata, consultor)
        if consultor.faltantes:
            no_evaluables.update(dict.fromkeys(consultor.faltantes, 1))
            continue
        corridas.append(_Corrida(caso.solicitud_id, con_vigente, con_candidata))

    def libro(reglas: Reglas, cual: str) -> Libro:
        evaluaciones = [getattr(c, cual) for c in corridas]
        aprobadas = [e for e in evaluaciones if _aprueba(e)]
        return Libro(
            reglas=reglas.id if cual == "vigente" else "candidata",
            evaluadas=len(evaluaciones),
            aprobadas=len(aprobadas),
            derivadas=sum(e.veredicto is Veredicto.DERIVAR for e in evaluaciones),
            rechazadas=sum(e.veredicto is Veredicto.RECHAZADO for e in evaluaciones),
            monto=sum(e.oferta.monto for e in aprobadas if e.oferta),
            pd_promedio=_promedio(e.pd for e in aprobadas),
            pd_corregida_promedio=_promedio(_corregida(e, factores) for e in aprobadas),
            aprobadas_por_rating=dict(sorted(Counter(e.rating for e in aprobadas if e.rating).items())),
        )

    otorgadas_por_id = {o.solicitud_id: o for o in otorgadas}

    def movimiento(seleccion: list[_Corrida], cual: str) -> Movimiento:
        evaluaciones = [getattr(c, cual) for c in seleccion]
        reales = [otorgadas_por_id[c.solicitud_id] for c in seleccion if c.solicitud_id in otorgadas_por_id]
        grupo = calcular(reales, registros).total if reales else None
        return Movimiento(
            solicitudes=len(seleccion),
            pd_promedio=_promedio(e.pd for e in evaluaciones),
            pd_corregida_promedio=_promedio(_corregida(e, factores) for e in evaluaciones),
            con_credito=grupo if grupo and grupo.creditos else None,
        )

    return Simulacion(
        flujo=flujo,
        vigente=libro(vigente, "vigente"),
        candidata=libro(candidata, "candidata"),
        cambios=cambios,
        matriz=dict(Counter((c.vigente.veredicto.value, c.candidata.veredicto.value) for c in corridas)),
        salen=movimiento([c for c in corridas if _aprueba(c.vigente) and not _aprueba(c.candidata)], "vigente"),
        entran=movimiento([c for c in corridas if _aprueba(c.candidata) and not _aprueba(c.vigente)], "candidata"),
        no_evaluables=dict(no_evaluables.most_common()),
        factores=factores,
    )


def factores_de_correccion(por_rating: Iterable[Grupo]) -> dict[str, float]:
    """El O/E de cada rating con datos suficientes; para el resto, el de toda la cartera con datos."""
    con_datos = [g for g in por_rating if g.estado != "pocos-datos" and g.ratio is not None]
    factores = {g.clave: g.ratio for g in con_datos}
    esperados = sum(g.esperados for g in con_datos)
    if esperados:
        factores["*"] = sum(g.observados for g in con_datos) / esperados
    return factores


def _aprueba(evaluacion: Evaluacion) -> bool:
    return evaluacion.veredicto is Veredicto.APROBADO and evaluacion.oferta is not None


def _corregida(evaluacion: Evaluacion, factores: Mapping[str, float]) -> float | None:
    if evaluacion.pd is None:
        return None
    factor = factores.get(evaluacion.rating or "", factores.get("*", 1.0))
    return min(1.0, evaluacion.pd * factor)


def _promedio(valores: Iterable[float | None]) -> float | None:
    lista = [v for v in valores if v is not None]
    return sum(lista) / len(lista) if lista else None


# --- cambios a las reglas ------------------------------------------------------


class CambioInvalido(ValueError):
    """Una operación sobre las reglas que no se puede aplicar."""


SECCIONES = ("killers", "derivaciones")


def aplicar_cambios(contenido: Mapping, operaciones: Iterable[Mapping]) -> dict:
    """Aplica operaciones a una copia del contenido de las reglas. No valida las reglas que
    resultan (eso lo hace quien las use): solo que cada operación tenga sentido.

    - ``{"op": "poner", "ruta": "politica.rechazar_ratings", "valor": ["D", "E"]}``
    - ``{"op": "cambiar_regla", "seccion": "killers", "id": "...", "campos": {"condicion": "..."}}``
    - ``{"op": "agregar_regla", "seccion": "derivaciones", "regla": {"id": ..., "descripcion": ...,
      "condicion": ...}, "despues_de": "id opcional"}``
    - ``{"op": "quitar_regla", "seccion": "killers", "id": "..."}``
    """
    resultado = copy.deepcopy(dict(contenido))
    for operacion in operaciones:
        if not isinstance(operacion, Mapping):
            raise CambioInvalido(f"cada cambio tiene que ser un objeto, no {operacion!r}")
        match operacion.get("op"):
            case "poner":
                _poner(resultado, str(operacion.get("ruta", "")), operacion.get("valor"))
            case "cambiar_regla":
                regla = _regla(resultado, operacion)
                campos = operacion.get("campos")
                if not isinstance(campos, Mapping) or not campos:
                    raise CambioInvalido("cambiar_regla necesita los campos a cambiar")
                if "id" in campos:
                    raise CambioInvalido("el id de una regla no se cambia: quitala y agregá otra")
                regla.update(campos)
            case "agregar_regla":
                reglas = _seccion(resultado, operacion)
                nueva = operacion.get("regla")
                if not isinstance(nueva, Mapping) or not nueva.get("id"):
                    raise CambioInvalido("agregar_regla necesita una regla con id")
                if any(r.get("id") == nueva["id"] for r in reglas):
                    raise CambioInvalido(f"ya hay una regla {nueva['id']!r} en {operacion['seccion']}")
                posicion = len(reglas)
                if despues := operacion.get("despues_de"):
                    posicion = next((i + 1 for i, r in enumerate(reglas) if r.get("id") == despues), None)
                    if posicion is None:
                        raise CambioInvalido(f"no hay una regla {despues!r} en {operacion['seccion']}")
                reglas.insert(posicion, dict(nueva))
            case "quitar_regla":
                reglas = _seccion(resultado, operacion)
                reglas.remove(_regla(resultado, operacion))
            case otra:
                raise CambioInvalido(f"operación desconocida: {otra!r}")
    return resultado


def _poner(contenido: dict, ruta: str, valor: object) -> None:
    partes = [p for p in ruta.split(".") if p]
    if not partes or partes[0] in SECCIONES:
        raise CambioInvalido(f"ruta inválida {ruta!r} (las reglas se cambian con cambiar_regla)")
    nodo = contenido
    for parte in partes[:-1]:
        if not isinstance(nodo.get(parte), dict):
            raise CambioInvalido(f"no existe {ruta!r} en las reglas")
        nodo = nodo[parte]
    nodo[partes[-1]] = valor


def _seccion(contenido: dict, operacion: Mapping) -> list:
    seccion = operacion.get("seccion")
    if seccion not in SECCIONES:
        raise CambioInvalido(f"la sección tiene que ser killers o derivaciones, no {seccion!r}")
    return contenido.setdefault(seccion, [])


def _regla(contenido: dict, operacion: Mapping) -> dict:
    regla = next((r for r in _seccion(contenido, operacion) if r.get("id") == operacion.get("id")), None)
    if regla is None:
        raise CambioInvalido(f"no hay una regla {operacion.get('id')!r} en {operacion.get('seccion')}")
    return regla
