"""Diferencias entre dos versiones de reglas de un flujo (ADR 0007).

Es lo que se revisa antes de publicar y lo que muestra el historial: reglas agregadas,
quitadas, modificadas y reordenadas; qué decide la política con cada rating; spreads
y campos de la oferta. Los ratings y sus topes no aparecen: son del modelo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from relevo.reglas import Regla, Reglas

_CAMPOS_REGLA = (("descripcion", "descripción"), ("condicion", "condición"), ("si_no_disponible", "si falta el dato"))
_DECISION = {"aprobar": "aprueba", "derivar": "deriva", "rechazar": "rechaza"}


@dataclass(frozen=True)
class Cambio:
    tipo: str  # agregada | quitada | modificada | orden
    que: str
    antes: str | None
    despues: str | None


def diferencias(antes: Reglas, despues: Reglas) -> list[Cambio]:
    cambios: list[Cambio] = []
    for nombre, plural, a, b in (
        ("Killer", "los killers", antes.killers, despues.killers),
        ("Derivación", "las derivaciones", antes.derivaciones, despues.derivaciones),
    ):
        cambios += _reglas(nombre, plural, a, b)

    if antes.scorecard.modelo != despues.scorecard.modelo:
        cambios.append(Cambio("modificada", "Scorecard · modelo", antes.scorecard.modelo, despues.scorecard.modelo))
    if antes.scorecard.si_no_disponible != despues.scorecard.si_no_disponible:
        cambios.append(
            Cambio(
                "modificada",
                "Scorecard · si falta un dato",
                antes.scorecard.si_no_disponible,
                despues.scorecard.si_no_disponible,
            )
        )
    for rating in dict.fromkeys(r.nombre for r in (*antes.ratings, *despues.ratings)):
        d0, d1 = _decision(antes, rating), _decision(despues, rating)
        if d0 != d1:
            cambios.append(Cambio("modificada", f"Rating {rating} · decisión", d0, d1))
    for rating in dict.fromkeys((*antes.politica.spread_por_rating, *despues.politica.spread_por_rating)):
        s0, s1 = antes.politica.spread_por_rating.get(rating), despues.politica.spread_por_rating.get(rating)
        if s0 != s1:
            cambios.append(Cambio("modificada", f"Rating {rating} · spread (pp)", _texto(s0), _texto(s1)))

    for etiqueta, valor in (
        ("tasa base", lambda r: r.politica.tasa_base.texto),
        ("forma de medir la capacidad de pago", lambda r: r.politica.capacidad_pago.tipo),
        ("variable de capacidad de pago", lambda r: r.politica.capacidad_pago.variable.texto),
        ("cuota máxima", lambda r: r.politica.capacidad_pago.porcentaje_cuota),
        ("monto mínimo", lambda r: r.politica.monto_minimo),
        ("redondeo del monto", lambda r: r.politica.redondeo_monto),
        ("plazo mínimo", lambda r: r.politica.plazo_minimo),
        ("plazo máximo", lambda r: r.politica.plazo_maximo),
        ("si falta un dato", lambda r: r.politica.si_no_disponible),
    ):
        v0, v1 = valor(antes), valor(despues)
        if v0 != v1:
            cambios.append(Cambio("modificada", f"Política · {etiqueta}", _texto(v0), _texto(v1)))
    ingresos_antes = antes.politica.capacidad_pago.ingreso_anual_por_categoria or {}
    ingresos_despues = despues.politica.capacidad_pago.ingreso_anual_por_categoria or {}
    for categoria in dict.fromkeys((*ingresos_antes, *ingresos_despues)):
        i0, i1 = ingresos_antes.get(categoria), ingresos_despues.get(categoria)
        if i0 != i1:
            que = f"Política · ingreso anual de la categoría {categoria}"
            cambios.append(Cambio("modificada", que, _texto(i0), _texto(i1)))
    return cambios


def _reglas(nombre: str, plural: str, antes: tuple[Regla, ...], despues: tuple[Regla, ...]) -> list[Cambio]:
    cambios = []
    por_id_antes, por_id_despues = {r.id: r for r in antes}, {r.id: r for r in despues}
    for regla in antes:
        if regla.id not in por_id_despues:
            cambios.append(Cambio("quitada", f"{nombre} {regla.id}", regla.condicion.texto, None))
    for regla in despues:
        if regla.id not in por_id_antes:
            cambios.append(Cambio("agregada", f"{nombre} {regla.id}", None, regla.condicion.texto))
    for regla in despues:
        anterior = por_id_antes.get(regla.id)
        if anterior is None:
            continue
        for campo, etiqueta in _CAMPOS_REGLA:
            v0, v1 = getattr(anterior, campo), getattr(regla, campo)
            if campo == "condicion":
                v0, v1 = v0.texto, v1.texto
            if v0 != v1:
                cambios.append(Cambio("modificada", f"{nombre} {regla.id} · {etiqueta}", v0, v1))
    comunes_antes = [r.id for r in antes if r.id in por_id_despues]
    comunes_despues = [r.id for r in despues if r.id in por_id_antes]
    if comunes_antes != comunes_despues:
        cambios.append(Cambio("orden", f"Orden de {plural}", " → ".join(comunes_antes), " → ".join(comunes_despues)))
    return cambios


def _decision(reglas: Reglas, rating: str) -> str:
    if rating in reglas.politica.rechazar_ratings:
        return _DECISION["rechazar"]
    if rating in reglas.politica.derivar_ratings:
        return _DECISION["derivar"]
    return _DECISION["aprobar"]


def _texto(valor: Any) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    if isinstance(valor, int) and not isinstance(valor, bool):
        return f"{valor:,}".replace(",", ".")
    if isinstance(valor, float):
        return f"{valor}".replace(".", ",")
    return str(valor)
