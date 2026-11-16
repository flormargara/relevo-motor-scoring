"""Monitoreo: PD esperada contra default observado, mora temprana y alertas."""

import math
from datetime import date, datetime

import pytest
from test_sintetico_cartera import PD_POR_RATING, _oferta

from conftest import BASE_PYME, armar_consultor, solicitud_pyme
from relevo import sintetico_cartera
from relevo.cartera import Desembolso, EstadoMensual, RegistroCartera
from relevo.dominio import Veredicto
from relevo.monitoreo import calcular, intervalo_poisson
from relevo.motor import evaluar
from relevo.repositorio import Otorgada, Repositorio


def _otorgada(id: str, rating: str = "B", pd: float = 0.04, segmento: str = "pyme") -> Otorgada:
    return Otorgada(id, segmento, datetime(2026, 1, 5), "pyme.v3", rating, pd)


def _registro(
    id: str, meses: int, mora: dict[int, int] | None = None, desde: date = date(2026, 1, 1)
) -> RegistroCartera:
    mora = mora or {}
    estados = tuple(
        EstadoMensual(
            date(desde.year + (desde.month + i) // 12, (desde.month + i) % 12 + 1, 1), 1000.0, 100.0, mora.get(i + 1, 0)
        )
        for i in range(meses)
    )
    return RegistroCartera(id, True, Desembolso(desde, 1200.0, 12, 60.0), estados)


def test_cada_credito_aporta_la_pd_a_su_horizonte():
    informe = calcular([_otorgada("a", pd=0.12)], [_registro("a", 6)])

    assert informe.total.esperados == pytest.approx(1 - 0.88**0.5)
    assert informe.total.observados == 0


def test_los_creditos_muy_nuevos_no_entran_en_el_default():
    informe = calcular([_otorgada("a"), _otorgada("b")], [_registro("a", 2), _registro("b", 3)])

    assert informe.total.creditos == 1  # solo b llega a MESES_MINIMOS


def test_default_es_90_dias_en_los_primeros_12_meses():
    otorgadas = [_otorgada(i) for i in "abc"]
    registros = [
        _registro("a", 6, {4: 90}),
        _registro("b", 14, {13: 120}),  # después del horizonte: no cuenta
        _registro("c", 6, {2: 60}),
    ]

    assert calcular(otorgadas, registros).total.observados == 1


def test_embudo_y_registros_ajenos():
    otorgadas = [_otorgada("a"), _otorgada("b"), _otorgada("c")]
    registros = [
        _registro("a", 4),
        RegistroCartera("b", convertida=False),
        _registro("ajena", 4),  # la entidad informa algo que no es una otorgada: se ignora
    ]

    informe = calcular(otorgadas, registros)

    assert (informe.embudo.otorgadas, informe.embudo.informadas) == (3, 2)
    assert (informe.embudo.convertidas, informe.embudo.desembolsadas) == (1, 1)
    assert informe.corte == date(2026, 5, 1)


def test_intervalo_poisson():
    bajo, alto = intervalo_poisson(100, 100.0)
    assert bajo < 1 < alto and alto - bajo == pytest.approx(0.4, abs=0.02)
    assert intervalo_poisson(0, 2.0)[0] == 0


def test_un_rating_subestimado_es_rojo_y_alerta():
    otorgadas = [_otorgada(f"b{i}", "B", 0.04) for i in range(100)]
    registros = [_registro(f"b{i}", 12, {6: 90} if i < 15 else None) for i in range(100)]

    informe = calcular(otorgadas, registros)

    (b,) = informe.por_rating
    assert b.estado == "rojo" and b.ratio == pytest.approx(15 / 4, rel=0.01)
    assert any("Rating B" in a.texto and a.nivel == "rojo" for a in informe.alertas)


def test_con_pocos_creditos_no_opina():
    informe = calcular([_otorgada("a")], [_registro("a", 12, {6: 90})])

    assert informe.total.estado == "pocos-datos" and informe.alertas == []


def test_alerta_si_se_rompe_el_orden_de_los_ratings():
    otorgadas = [_otorgada(f"a{i}", "A", 0.02) for i in range(60)] + [_otorgada(f"c{i}", "C", 0.08) for i in range(60)]
    registros = [_registro(f"a{i}", 12, {6: 90} if i < 6 else None) for i in range(60)] + [
        _registro(f"c{i}", 12, {6: 90} if i < 2 else None) for i in range(60)
    ]

    alertas = [a.texto for a in calcular(otorgadas, registros).alertas]

    assert any("Se rompe el orden: el rating A defaultea más que el C" in t for t in alertas)


# --- contra la cartera sintética ---------------------------------------------


def _sintetica(desvios):
    """Evaluaciones repartidas entre ene-2025 y ago-2026, con mora generada desde su PD."""
    base = []
    for i, rating in enumerate(list(PD_POR_RATING) * 2400):
        mes = (i // 5) % 20
        evaluada = date(2025 + mes // 12, mes % 12 + 1, 10)
        base.append(sintetico_cartera.Otorgada(f"s{i}", evaluada, PD_POR_RATING[rating], rating, _oferta(12)))
    otorgadas = [
        Otorgada(
            o.solicitud_id, "pyme", datetime(o.evaluada_en.year, o.evaluada_en.month, 10), "pyme.v3", o.rating, o.pd
        )
        for o in base
    ]
    return calcular(otorgadas, sintetico_cartera.generar(base, date(2026, 11, 30), desvios=desvios))


def test_una_cartera_calibrada_no_es_roja():
    informe = _sintetica(())

    assert {g.clave: g.estado for g in informe.por_rating if g.estado == "rojo"} == {}
    assert 0.8 < informe.total.ratio < 1.25


def test_detecta_el_desvio_plantado_en_la_mora_temprana():
    informe = _sintetica([sintetico_cartera.desvio_plantado(date(2026, 11, 30))])

    assert any(a.texto.startswith("Rating C: sube la mora temprana") for a in informe.alertas)
    assert not any("sube la mora temprana" in a.texto and not a.texto.startswith("Rating C") for a in informe.alertas)


# --- repositorio ---------------------------------------------------------------


def test_el_repositorio_lee_las_otorgadas(tmp_path, reglas_pyme):
    repo = Repositorio(tmp_path / "relevo.sqlite")
    aprobada = repo.crear_solicitud(solicitud_pyme(), {"ventas_anuales": 240_000_000})
    evaluacion = evaluar(solicitud_pyme(), reglas_pyme, armar_consultor(BASE_PYME))
    assert evaluacion.veredicto is Veredicto.APROBADO
    repo.guardar_evaluacion(aprobada, evaluacion, datetime(2026, 1, 10, 9, 0))
    repo.crear_solicitud(solicitud_pyme())  # sin evaluar: no cuenta

    (otorgada,) = repo.otorgadas()

    assert (otorgada.solicitud_id, otorgada.segmento, otorgada.rating) == (aprobada, "pyme", evaluacion.rating)
    assert math.isclose(otorgada.pd, evaluacion.pd)
    assert otorgada.version_reglas == evaluacion.version_reglas
