"""Monitoreo de cartera: PD esperada contra default observado (parte del chat, ADR 0008).

Cruza las evaluaciones que ofrecieron un crédito con lo que la entidad informa de su
cartera (``FuenteDeCartera``) y calcula, sin agente, los números que después explica el
chat. Todo es determinístico: el agente no hace cuentas (ADR 0001).

**Default esperado contra observado.** La PD del scorecard es a 12 meses, pero la cartera
casi nunca tiene 12 meses de historia. Cada crédito aporta lo que se esperaba a su propio
horizonte observado ``m``, suponiendo hazard constante:

    PD_m = 1 - (1 - PD_12) ** (m / 12)

Se suman los defaults esperados (E) y los observados (O, 90+ días de mora dentro de los
primeros 12 meses, la misma definición que el scorecard) y se compara ``O/E`` con un
intervalo de confianza de Poisson. Los créditos con menos de ``MESES_MINIMOS`` meses no
entran: todavía no pueden llegar a 90 días de mora.

**Mora temprana.** Créditos con 30+ días de mora en los primeros 3 meses, por cohorte de
desembolso. Anticipa el deterioro de las cohortes nuevas, que todavía no maduraron.

No guarda nada: la cartera es de la entidad y se lee en cada consulta.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date

from relevo.cartera import DIAS_DEFAULT, HORIZONTE_MESES, RegistroCartera
from relevo.repositorio import Otorgada

MESES_MINIMOS = 3  # antes no se puede llegar a 90 días de mora
MESES_MORA_TEMPRANA = 3
DIAS_MORA_TEMPRANA = 30
COHORTES_RECIENTES = 3  # las que se comparan contra las anteriores en la mora temprana
Z = 1.96  # intervalo del 95%


@dataclass(frozen=True)
class Umbrales:
    """Cuándo un O/E es alerta. Solo cuenta si el piso del intervalo supera 1 (no es ruido)."""

    amarillo: float = 1.2
    rojo: float = 1.5
    creditos_minimos: int = 30  # con menos créditos maduros, no se opina
    mora_temprana: float = 1.5  # cohortes recientes contra anteriores


UMBRALES = Umbrales()


@dataclass(frozen=True)
class Grupo:
    """Un corte de la cartera (todo, un rating, una cohorte, etc.)."""

    clave: str
    creditos: int  # desembolsados con al menos MESES_MINIMOS meses observados
    meses_promedio: float
    pd_12m: float  # promedio de la PD del scorecard
    esperados: float  # E
    observados: int  # O
    ic: tuple[float, float] | None  # intervalo de O/E
    mora_temprana_base: int  # créditos con MESES_MORA_TEMPRANA meses observados
    mora_temprana: int  # de esos, los que tuvieron 30+ días de mora
    estado: str  # verde | amarillo | rojo | sobreestima | pocos-datos

    @property
    def ratio(self) -> float | None:
        return self.observados / self.esperados if self.esperados else None

    @property
    def tasa_mora_temprana(self) -> float | None:
        return self.mora_temprana / self.mora_temprana_base if self.mora_temprana_base else None


@dataclass(frozen=True)
class Embudo:
    otorgadas: int
    informadas: int  # de las que la entidad informó algo
    convertidas: int
    desembolsadas: int


@dataclass(frozen=True)
class Alerta:
    nivel: str  # amarillo | rojo
    texto: str


@dataclass(frozen=True)
class Informe:
    corte: date | None  # el último mes informado por la entidad
    embudo: Embudo
    total: Grupo
    por_rating: list[Grupo]
    por_cohorte: list[Grupo]  # mes de desembolso, de la más vieja a la más nueva
    por_segmento: list[Grupo]
    por_version: list[Grupo]
    por_rating_cohorte: dict[str, list[Grupo]]  # rating -> sus cohortes
    alertas: list[Alerta] = field(default_factory=list)


@dataclass(frozen=True)
class _Credito:
    otorgada: Otorgada
    registro: RegistroCartera

    @property
    def meses(self) -> int:
        return min(self.registro.meses_observados(), HORIZONTE_MESES)

    @property
    def esperado(self) -> float:
        return 1 - (1 - self.otorgada.pd) ** (self.meses / HORIZONTE_MESES)

    @property
    def en_default(self) -> bool:
        return self.registro.en_default(HORIZONTE_MESES, DIAS_DEFAULT)

    @property
    def cohorte(self) -> str:
        assert self.registro.desembolso is not None
        return self.registro.desembolso.fecha.strftime("%Y-%m")


def calcular(
    otorgadas: Iterable[Otorgada], registros: Iterable[RegistroCartera], umbrales: Umbrales = UMBRALES
) -> Informe:
    otorgadas = {o.solicitud_id: o for o in otorgadas}
    registros = [r for r in registros if r.solicitud_id in otorgadas]
    desembolsados = [_Credito(otorgadas[r.solicitud_id], r) for r in registros if r.desembolso]
    embudo = Embudo(
        otorgadas=len(otorgadas),
        informadas=len(registros),
        convertidas=sum(r.convertida for r in registros),
        desembolsadas=len(desembolsados),
    )
    corte = max((e.periodo for r in registros for e in r.estados), default=None)

    def cortar(clave: Callable[[_Credito], str], orden: Callable[[str], object] = str) -> list[Grupo]:
        grupos: dict[str, list[_Credito]] = defaultdict(list)
        for c in desembolsados:
            grupos[clave(c)].append(c)
        return [_grupo(k, grupos[k], umbrales) for k in sorted(grupos, key=orden)]

    ratings = sorted({c.otorgada.rating for c in desembolsados})
    por_rating_cohorte = {
        rating: [
            _grupo(k, [c for c in desembolsados if c.otorgada.rating == rating and c.cohorte == k], umbrales)
            for k in sorted({c.cohorte for c in desembolsados if c.otorgada.rating == rating})
        ]
        for rating in ratings
    }
    por_rating = cortar(lambda c: c.otorgada.rating)
    por_cohorte = cortar(lambda c: c.cohorte)
    return Informe(
        corte=corte,
        embudo=embudo,
        total=_grupo("total", desembolsados, umbrales),
        por_rating=por_rating,
        por_cohorte=por_cohorte,
        por_segmento=cortar(lambda c: c.otorgada.segmento),
        por_version=cortar(lambda c: c.otorgada.version_reglas),
        por_rating_cohorte=por_rating_cohorte,
        alertas=_alertas(por_rating, por_rating_cohorte, umbrales),
    )


def _grupo(clave: str, creditos: list[_Credito], umbrales: Umbrales) -> Grupo:
    maduros = [c for c in creditos if c.meses >= MESES_MINIMOS]
    esperados = sum(c.esperado for c in maduros)
    observados = sum(c.en_default for c in maduros)
    tempranos = [c for c in creditos if c.registro.meses_observados() >= MESES_MORA_TEMPRANA]
    ic = intervalo_poisson(observados, esperados) if esperados else None
    return Grupo(
        clave=clave,
        creditos=len(maduros),
        meses_promedio=sum(c.meses for c in maduros) / len(maduros) if maduros else 0.0,
        pd_12m=sum(c.otorgada.pd for c in maduros) / len(maduros) if maduros else 0.0,
        esperados=esperados,
        observados=observados,
        ic=ic,
        mora_temprana_base=len(tempranos),
        mora_temprana=sum(c.registro.peor_mora(MESES_MORA_TEMPRANA) >= DIAS_MORA_TEMPRANA for c in tempranos),
        estado=_estado(len(maduros), observados, esperados, ic, umbrales),
    )


def _estado(
    creditos: int, observados: int, esperados: float, ic: tuple[float, float] | None, umbrales: Umbrales
) -> str:
    if ic is None or creditos < umbrales.creditos_minimos:
        return "pocos-datos"
    ratio = observados / esperados
    if ic[0] > 1 and ratio > umbrales.rojo:
        return "rojo"
    if ic[0] > 1 and ratio > umbrales.amarillo:
        return "amarillo"
    if ic[1] < 1:
        return "sobreestima"
    return "verde"


def intervalo_poisson(observados: int, esperados: float, z: float = Z) -> tuple[float, float]:
    """Intervalo de O/E con la aproximación de Byar al intervalo exacto de Poisson."""
    if observados == 0:
        bajo = 0.0
    else:
        bajo = observados * (1 - 1 / (9 * observados) - z / (3 * math.sqrt(observados))) ** 3
    o = observados + 1
    alto = o * (1 - 1 / (9 * o) + z / (3 * math.sqrt(o))) ** 3
    return bajo / esperados, alto / esperados


def _alertas(por_rating: list[Grupo], por_rating_cohorte: dict[str, list[Grupo]], umbrales: Umbrales) -> list[Alerta]:
    alertas = []
    for g in por_rating:
        if g.estado in ("amarillo", "rojo"):
            alertas.append(
                Alerta(
                    g.estado,
                    f"Rating {g.clave}: {g.observados} defaults observados contra {_d(g.esperados, 1)} esperados "
                    f"(O/E {_d(g.ratio)}, IC 95% {_d(g.ic[0])}–{_d(g.ic[1])}). El scorecard subestima el riesgo.",
                )
            )

    # orden: con la exposición de cada uno, un rating peor tiene que defaultear más
    tasas = [(g.clave, g.pd_12m, g.observados / (g.creditos * g.meses_promedio / 12)) for g in por_rating if g.creditos]
    tasas.sort(key=lambda t: t[1])
    for (mejor, _, tasa_mejor), (peor, _, tasa_peor) in zip(tasas, tasas[1:], strict=False):
        if tasa_mejor > tasa_peor:
            alertas.append(
                Alerta(
                    "amarillo",
                    f"Se rompe el orden: el rating {mejor} defaultea más que el {peor} "
                    f"({_pct(tasa_mejor)} contra {_pct(tasa_peor)} anualizado).",
                )
            )

    for rating, cohortes in por_rating_cohorte.items():
        if (alerta := _mora_temprana(rating, cohortes, umbrales)) is not None:
            alertas.append(alerta)

    alertas.sort(key=lambda a: a.nivel != "rojo")
    return alertas


def _mora_temprana(rating: str, cohortes: list[Grupo], umbrales: Umbrales) -> Alerta | None:
    """Cada una de las últimas cohortes con mora temprana medible contra todas las anteriores juntas,
    con un test de dos proporciones. De a una, porque el deterioro suele empezar en la más nueva y
    sumarla con otras lo diluye."""
    medibles = [g for g in cohortes if g.mora_temprana_base]
    if len(medibles) <= COHORTES_RECIENTES:
        return None
    recientes, anteriores = medibles[-COHORTES_RECIENTES:], medibles[:-COHORTES_RECIENTES]
    n0, x0 = sum(g.mora_temprana_base for g in anteriores), sum(g.mora_temprana for g in anteriores)
    p0 = x0 / n0
    deterioradas = [g for g in recientes if _peor_que(g.mora_temprana, g.mora_temprana_base, x0, n0, umbrales)]
    if not deterioradas or p0 == 0:
        return None
    detalle = ", ".join(f"{g.clave} ({_pct(g.tasa_mora_temprana)})" for g in deterioradas)
    return Alerta(
        "amarillo",
        f"Rating {rating}: sube la mora temprana (30+ días en {MESES_MORA_TEMPRANA} meses) en las cohortes "
        f"{detalle}, contra {_pct(p0)} de las anteriores. Puede ser un deterioro que todavía no llegó al default.",
    )


def _peor_que(x1: int, n1: int, x0: int, n0: int, umbrales: Umbrales) -> bool:
    p1, p0, p = x1 / n1, x0 / n0, (x0 + x1) / (n0 + n1)
    if p0 == 0 or p in (0, 1):
        return False
    z = (p1 - p0) / math.sqrt(p * (1 - p) * (1 / n0 + 1 / n1))
    return p1 / p0 > umbrales.mora_temprana and z > Z


def _d(valor: float, decimales: int = 2) -> str:
    """Con coma decimal, como el resto de la app."""
    return f"{valor:.{decimales}f}".replace(".", ",")


def _pct(valor: float) -> str:
    return f"{_d(valor * 100, 1)} %"
