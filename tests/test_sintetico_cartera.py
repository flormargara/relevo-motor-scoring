"""Cartera sintética posterior a la evaluación: coherente con la PD y con desvíos plantados."""

import random
from datetime import date, datetime

import pytest

from conftest import BASE_PYME, armar_consultor, cuota_francesa, solicitud_pyme
from relevo.cartera import CarteraEnArchivos
from relevo.dominio import Oferta, Veredicto
from relevo.motor import evaluar
from relevo.repositorio import Repositorio
from relevo.sintetico_cartera import Desvio, Otorgada, desvio_plantado, generar, main, otorgadas

HASTA = date(2026, 11, 30)
PD_POR_RATING = {"A": 0.015, "B": 0.04, "C": 0.08, "D": 0.15, "E": 0.30}


def _oferta(plazo: int = 18, monto: float = 1_000_000, tna: float = 60.0) -> Oferta:
    return Oferta(monto, plazo, tna, round(cuota_francesa(monto, tna, plazo), 2))


def _otorgadas(n: int, evaluada_en: date = date(2025, 6, 10), ratings=tuple(PD_POR_RATING)) -> list[Otorgada]:
    rng = random.Random(7)
    resultado = []
    for i in range(n):
        rating = ratings[i % len(ratings)]
        plazo = rng.choice([12, 18, 24])
        resultado.append(Otorgada(f"s{i}", evaluada_en, PD_POR_RATING[rating], rating, _oferta(plazo)))
    return resultado


@pytest.fixture(scope="module")
def madura():
    """Desembolsos con más de 12 meses observados, sin desvíos."""
    return _otorgadas(25_000), generar(_otorgadas(25_000), HASTA)


def test_es_deterministica_y_estable_al_agregar_solicitudes():
    base = _otorgadas(200)

    assert generar(base, HASTA) == generar(base, HASTA)
    assert generar(base, HASTA) != generar(base, HASTA, semilla=1)
    assert generar(base[:50], HASTA) == generar(base, HASTA)[:50]


def test_el_default_observado_acompana_la_pd(madura):
    base, registros = madura
    for rating, pd in PD_POR_RATING.items():
        desembolsados = [r for o, r in zip(base, registros, strict=True) if o.rating == rating and r.desembolso]
        observado = sum(r.en_default() for r in desembolsados) / len(desembolsados)
        assert observado == pytest.approx(pd, rel=0.25, abs=0.006), rating


def test_convierten_mas_los_peores_ratings(madura):
    base, registros = madura
    conversion = {
        rating: sum(r.convertida for o, r in zip(base, registros, strict=True) if o.rating == rating)
        for rating in PD_POR_RATING
    }
    assert conversion["A"] < conversion["E"]
    assert all((r.desembolso is not None) == r.convertida for r in registros)


def test_los_creditos_sanos_se_cancelan_en_su_plazo(madura):
    base, registros = madura
    for o, r in zip(base[:2000], registros[:2000], strict=True):
        plazo_cumplido = o.oferta.plazo_meses <= 12  # desembolsados en jun-2025, observados hasta nov-2026
        if r.desembolso and plazo_cumplido and not r.en_default(meses=None):
            assert len(r.estados) == o.oferta.plazo_meses
            assert r.estados[-1].saldo == pytest.approx(0, abs=1)
            assert sum(e.pagado for e in r.estados) >= o.oferta.monto


def test_solo_informa_meses_cerrados():
    registros = generar(_otorgadas(500, evaluada_en=date(2026, 9, 1)), date(2026, 11, 15))

    periodos = {e.periodo for r in registros for e in r.estados}
    assert periodos <= {date(2026, 10, 1)}


def test_el_desvio_plantado_deteriora_solo_su_rating():
    hasta = date(2026, 11, 30)
    desvio = desvio_plantado(hasta)
    assert desvio == Desvio("C", date(2026, 7, 1), 3.0)

    def mora_temprana(desvios, rating):
        base = _otorgadas(12_000, evaluada_en=date(2026, 7, 5), ratings=(rating,))
        registros = [r for r in generar(base, hasta, desvios=desvios) if r.desembolso]
        return sum(r.peor_mora() >= 30 for r in registros) / len(registros)

    assert mora_temprana([desvio], "C") > 2 * mora_temprana([], "C")
    assert mora_temprana([desvio], "B") == mora_temprana([], "B")


def test_parte_de_las_otorgadas_del_repositorio(tmp_path, reglas_pyme):
    repo = Repositorio(tmp_path / "relevo.sqlite")
    aprobada = repo.crear_solicitud(solicitud_pyme(), {"ventas_anuales": 240_000_000})
    evaluacion = evaluar(solicitud_pyme(), reglas_pyme, armar_consultor(BASE_PYME))
    assert evaluacion.veredicto is Veredicto.APROBADO
    repo.guardar_evaluacion(aprobada, evaluacion, datetime(2026, 1, 10, 9, 0))
    repo.crear_solicitud(solicitud_pyme())  # sin evaluar: no cuenta

    assert [(o.solicitud_id, o.evaluada_en, o.rating) for o in otorgadas(repo)] == [
        (aprobada, date(2026, 1, 10), evaluacion.rating)
    ]

    destino = main(
        ["--db", str(tmp_path / "relevo.sqlite"), "--destino", str(tmp_path / "cartera"), "--hasta", "2026-11-15"]
    )
    assert [r.solicitud_id for r in CarteraEnArchivos(destino).registros()] == [aprobada]
