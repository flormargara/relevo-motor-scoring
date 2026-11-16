"""Cartera sintética posterior a la evaluación (offline, no es parte del runtime).

    python -m relevo.sintetico_cartera --hasta 2026-11-15

Para probar y demostrar el chat, el monitoreo, el simulador y el flujo de comportamiento
hacen falta datos de lo que pasó después de evaluar: conversión, desembolso, pagos y
mora. Son de la entidad (ADR 0008), así que acá se inventan y se escriben en el formato
de ``CarteraEnArchivos``. Así lo sintético se lee por el seam real ``FuenteDeCartera`` y
no por un camino especial.

- Parte de las solicitudes otorgadas (aprobadas por el motor, o derivadas y aprobadas
  por el analista) y se enlaza con ellas por el id de solicitud.
- Es coherente con el modelo: la probabilidad de entrar en default (90 días de mora)
  en los primeros 12 meses es la PD de la evaluación, con ruido por crédito.
- Trae desvíos plantados (por defecto, el rating C se deteriora en los desembolsos de
  los últimos 4 meses) para poder probar las alertas.
- Es determinística dada la semilla, y cada solicitud tiene su propio generador, así
  que agregar evaluaciones no cambia la cartera de las que ya estaban.
"""

from __future__ import annotations

import argparse
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from relevo.cartera import (
    DIAS_DEFAULT,
    HORIZONTE_MESES,
    Desembolso,
    EstadoMensual,
    RegistroCartera,
    escribir_en_archivos,
)
from relevo.dominio import Oferta, Veredicto
from relevo.repositorio import Repositorio

SEMILLA = 2026
# Selección adversa: los peores ratings tienen menos alternativas y toman más la oferta.
CONVERSION_POR_RATING = {"A": 0.55, "B": 0.60, "C": 0.65, "D": 0.70, "E": 0.75}
RUIDO_PD = 0.3  # desvío del log de la PD de cada crédito alrededor de la del modelo
DIAS_HASTA_DESEMBOLSO = 10
MESES_EXTRA_EN_DEFAULT = 12  # un crédito en default se sigue informando después del plazo


@dataclass(frozen=True)
class Otorgada:
    """Una solicitud con oferta que se le puede desembolsar al solicitante."""

    solicitud_id: str
    evaluada_en: date
    pd: float
    rating: str
    oferta: Oferta


@dataclass(frozen=True)
class Desvio:
    """Multiplica la PD de un rating en los desembolsos desde una fecha."""

    rating: str
    desde: date
    factor: float


def desvio_plantado(hasta: date) -> Desvio:
    return Desvio(rating="C", desde=_sumar_meses(_mes(hasta), -4), factor=3.0)


def generar(
    otorgadas: Iterable[Otorgada],
    hasta: date,
    semilla: int = SEMILLA,
    desvios: Sequence[Desvio] = (),
    mora: Mapping[str, Mapping[date, int]] | None = None,
) -> list[RegistroCartera]:
    """La cartera observada al cierre del mes de ``hasta``: solo meses completos.

    Con ``mora`` (días de mora por período, por solicitud) la mora de esas solicitudes no
    se sortea con la PD: es la observada (p. ej. en la Central de Deudores). La conversión,
    el desembolso y los pagos siguen siendo sintéticos."""
    mora = mora or {}
    return [_registro(o, hasta, semilla, desvios, mora.get(o.solicitud_id)) for o in otorgadas]


def _registro(
    o: Otorgada, hasta: date, semilla: int, desvios: Sequence[Desvio], mora_observada: Mapping[date, int] | None
) -> RegistroCartera:
    rng = random.Random(f"{semilla}:{o.solicitud_id}")
    if rng.random() >= CONVERSION_POR_RATING.get(o.rating, 0.65):
        return RegistroCartera(o.solicitud_id, convertida=False)

    fecha = o.evaluada_en + timedelta(days=rng.randint(0, DIAS_HASTA_DESEMBOLSO))
    desembolso = Desembolso(fecha, o.oferta.monto, o.oferta.plazo_meses, o.oferta.tasa_nominal_anual)
    if fecha > hasta:
        return RegistroCartera(o.solicitud_id, convertida=True, desembolso=desembolso)

    plazo = o.oferta.plazo_meses
    if mora_observada is not None:
        dias_mora = lambda k, periodo: mora_observada.get(periodo, 0)  # noqa: E731
    else:
        dias_mora = _mora_sorteada(o, fecha, rng, desvios)

    tasa = o.oferta.tasa_nominal_anual / 1200
    cuota = o.oferta.cuota
    saldo = o.oferta.monto
    adeudadas = 0  # cuotas impagas, que se pagan juntas al regularizar
    estados = []
    for k in range(1, plazo + MESES_EXTRA_EN_DEFAULT + 1):
        periodo = _sumar_meses(_mes(fecha), k)
        if _sumar_meses(periodo, 1) > hasta + timedelta(days=1):  # el mes todavía no cerró
            break
        if (dias := dias_mora(k, periodo)) > 0:
            adeudadas += 1
            estados.append(EstadoMensual(periodo, round(saldo, 2), 0.0, dias))
            continue
        pagado = 0.0
        for _ in range(1 + adeudadas):
            pago = min(cuota, saldo * (1 + tasa))
            saldo = saldo * (1 + tasa) - pago
            pagado += pago
        adeudadas = 0
        estados.append(EstadoMensual(periodo, round(max(saldo, 0.0), 2), round(pagado, 2), 0))
        if saldo <= 0.01:
            break
    return RegistroCartera(o.solicitud_id, convertida=True, desembolso=desembolso, estados=tuple(estados))


def _mora_sorteada(o: Otorgada, fecha: date, rng: random.Random, desvios: Sequence[Desvio]):
    """Días de mora por mes desde el desembolso, sorteados con la PD de la evaluación."""
    factor = math.prod(d.factor for d in desvios if d.rating == o.rating and fecha >= d.desde)
    ruido = math.exp(rng.gauss(0, RUIDO_PD) - RUIDO_PD**2 / 2)  # media 1: no corre la PD
    pd = min(0.95, o.pd * factor * ruido)

    meses_mora = DIAS_DEFAULT // 30
    plazo = o.oferta.plazo_meses
    # el primer impago tiene que caer dentro del plazo, antes de que el crédito se cancele
    ultimo_mes_default = max(meses_mora, min(HORIZONTE_MESES, plazo + meses_mora - 1))
    mes_default = rng.randint(meses_mora, ultimo_mes_default) if rng.random() < pd else None
    # quien no entra en default puede atrasarse un mes y regularizar al siguiente
    mes_atraso = rng.randint(1, max(1, plazo - 1)) if mes_default is None and rng.random() < 2 * pd else None
    primer_impago = mes_default - meses_mora + 1 if mes_default else None

    def dias(k: int, periodo: date) -> int:
        if primer_impago and k >= primer_impago:
            return 30 * (k - primer_impago + 1)
        return 30 if k == mes_atraso else 0

    return dias


def otorgadas(repositorio: Repositorio) -> list[Otorgada]:
    """Las solicitudes cuya última evaluación terminó en un crédito ofrecido."""
    resultado = []
    for fila in repositorio.bandeja():
        if fila.evaluacion_id is None:
            continue
        guardada = repositorio.evaluacion(fila.evaluacion_id)
        e = guardada.evaluacion
        aprobada = e.veredicto is Veredicto.APROBADO or (
            e.veredicto is Veredicto.DERIVAR
            and (resolucion := repositorio.resolucion(guardada.id)) is not None
            and resolucion.decision == "aprobar"
        )
        if aprobada and e.oferta and e.pd is not None and e.rating:
            resultado.append(Otorgada(fila.solicitud.id, guardada.creada_en.date(), e.pd, e.rating, e.oferta))
    return resultado


def _mes(fecha: date) -> date:
    return fecha.replace(day=1)


def _sumar_meses(mes: date, meses: int) -> date:
    indice = mes.year * 12 + mes.month - 1 + meses
    return date(indice // 12, indice % 12 + 1, 1)


# --- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description="Genera la cartera sintética de las solicitudes otorgadas.")
    parser.add_argument("--db", type=Path, default=Path("data/relevo.sqlite"))
    parser.add_argument("--destino", type=Path, default=Path("data/cartera"))
    parser.add_argument("--hasta", type=date.fromisoformat, default=date.today(), help="fecha de corte (ISO)")
    parser.add_argument("--semilla", type=int, default=SEMILLA)
    parser.add_argument("--sin-desvio", action="store_true", help="no plantar el deterioro del rating C")
    args = parser.parse_args(argv)

    base = otorgadas(Repositorio(args.db))
    desvios = () if args.sin_desvio else (desvio_plantado(args.hasta),)
    registros = generar(base, args.hasta, args.semilla, desvios)
    destino = escribir_en_archivos(registros, args.destino)
    convertidas = sum(r.convertida for r in registros)
    en_default = sum(r.en_default() for r in registros)
    print(f"{destino}: {len(registros)} otorgadas, {convertidas} convertidas, {en_default} en default a la fecha")
    for d in desvios:
        print(f"  desvío plantado: rating {d.rating} ×{d.factor} desde {d.desde}")
    return destino


if __name__ == "__main__":  # pragma: no cover
    main()
