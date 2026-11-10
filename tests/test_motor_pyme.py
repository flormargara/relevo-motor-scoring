"""Comportamiento del motor para PyME, siempre a través de `evaluar`."""

import math

import pytest

from conftest import (
    BASE_PYME,
    CUIL_HUMANA,
    CUIT_PYME_DV_INVALIDO,
    armar_consultor,
    cuota_francesa,
    solicitud_pyme,
)
from relevo.consultor import ConsultorFalso, NoDisponible, SinAntecedentes
from relevo.dominio import Oferta, Veredicto
from relevo.motor import evaluar


def evaluar_pyme(reglas, solicitud=None, **cambios):
    consultor = armar_consultor(BASE_PYME, **cambios)
    return evaluar(solicitud or solicitud_pyme(), reglas, consultor), consultor


# --- camino feliz ---------------------------------------------------------


def test_pyme_sana_queda_aprobada_con_la_oferta_pedida(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme)

    assert evaluacion.veredicto is Veredicto.APROBADO
    assert evaluacion.version_reglas == "pyme.v1"
    assert evaluacion.puntos == 210
    assert evaluacion.pd == pytest.approx(1 / 81)
    assert evaluacion.rating == "A"
    # tasa = BADLAR 30 + spread A 6
    assert evaluacion.oferta == Oferta(
        monto=10_000_000, plazo_meses=12, tasa_nominal_anual=36.0, cuota=cuota_francesa(10_000_000, 36, 12)
    )


def test_la_traza_cuenta_cada_paso(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme)

    killers = evaluacion.pasos("killer")
    assert [paso.regla for paso in killers] == [k.id for k in reglas_pyme.killers]
    assert {paso.resultado for paso in killers} == {"no salta"}
    assert killers[-1].detalle["valores"] == {"cheques.rechazados_impagos": 0}

    puntos = {paso.regla: paso.detalle["puntos"] for paso in evaluacion.pasos("scorecard")}
    assert puntos == {
        "peor_situacion_12m": 45,
        "entidades_con_deuda": 30,
        "antiguedad_anios": 40,
        "score_bureau": 55,
        "endeudamiento_sobre_ventas": 40,
    }
    assert evaluacion.pasos("rating")[0].resultado == "A"
    assert evaluacion.pasos("veredicto")[0].resultado == "APROBADO"
    assert evaluacion.fuentes_no_consultadas == ()


def test_guarda_un_snapshot_por_fuente_consultada(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme)

    assert sorted(s.fuente for s in evaluacion.snapshots) == sorted(BASE_PYME)


def test_es_deterministico(reglas_pyme):
    primera, _ = evaluar_pyme(reglas_pyme)
    segunda, _ = evaluar_pyme(reglas_pyme)

    assert primera == segunda


def test_cada_fuente_se_consulta_una_sola_vez(reglas_pyme):
    _, consultor = evaluar_pyme(reglas_pyme)

    assert len(consultor.pedidas) == len(set(consultor.pedidas))


# --- killers --------------------------------------------------------------


@pytest.mark.parametrize(
    "killer, cambios",
    [
        ("lista_negra", {"propios": {"lista_negra": True}}),
        ("sociedad_dada_de_baja", {"igj": {"dada_de_baja": True}}),
        ("antiguedad_menor_a_un_anio", {"igj": {"antiguedad_anios": 0.5}}),
        ("situacion_bcra_3_o_peor", {"cendeu": {"peor_situacion_12m": 3}}),
        ("cheques_rechazados_impagos", {"cheques": {"rechazados_impagos": 2}}),
    ],
)
def test_cada_killer_rechaza(reglas_pyme, killer, cambios):
    evaluacion, _ = evaluar_pyme(reglas_pyme, **cambios)

    assert evaluacion.veredicto is Veredicto.RECHAZADO
    assert evaluacion.motivos[0].startswith(killer)
    assert evaluacion.oferta is None and evaluacion.pd is None


def test_el_primer_killer_corta_y_no_consulta_la_fuente_paga(reglas_pyme):
    evaluacion, consultor = evaluar_pyme(reglas_pyme, propios={"lista_negra": True}, igj={"dada_de_baja": True})

    assert evaluacion.motivos == ("lista_negra: El solicitante está en la lista negra de la entidad",)
    assert [paso.regla for paso in evaluacion.pasos("killer")] == ["identificacion_invalida", "lista_negra"]
    assert "bureau" not in consultor.pedidas
    assert "bureau" in evaluacion.fuentes_no_consultadas


@pytest.mark.parametrize("cuit", [CUIT_PYME_DV_INVALIDO, CUIL_HUMANA, "3071234567", "30-71234567-1x", "abc"])
def test_identificacion_invalida_rechaza_sin_consultar_fuentes(reglas_pyme, cuit):
    evaluacion, consultor = evaluar_pyme(reglas_pyme, solicitud=solicitud_pyme(cuit=cuit))

    assert evaluacion.veredicto is Veredicto.RECHAZADO
    assert evaluacion.motivos[0].startswith("identificacion_invalida")
    assert consultor.pedidas == []


def test_acepta_cuit_con_guiones(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, solicitud=solicitud_pyme(cuit="30-71234567-1"))

    assert evaluacion.veredicto is Veredicto.APROBADO


# --- sin antecedentes y fuentes no disponibles ----------------------------


def test_sin_antecedentes_en_cendeu_no_es_falla_y_deriva(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, cendeu=SinAntecedentes())

    assert evaluacion.veredicto is Veredicto.DERIVAR
    assert evaluacion.motivos[0].startswith("sin_antecedentes_bcra")
    situacion = next(p for p in evaluacion.pasos("killer") if p.regla == "situacion_bcra_3_o_peor")
    assert situacion.resultado == "sin dato"
    puntos = {p.regla: p.detalle["puntos"] for p in evaluacion.pasos("scorecard")}
    assert puntos["peor_situacion_12m"] == 15  # puntos de "sin dato"
    assert evaluacion.oferta is not None  # la oferta queda como propuesta para el analista


def test_fuente_no_disponible_en_killer_continua_y_deriva(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, igj=NoDisponible("timeout"))

    assert evaluacion.veredicto is Veredicto.DERIVAR
    baja = next(p for p in evaluacion.pasos("killer") if p.regla == "sociedad_dada_de_baja")
    assert baja.resultado == "no evaluable"
    assert baja.detalle["motivo"] == "igj: timeout"
    assert any(m.startswith("sociedad_dada_de_baja") for m in evaluacion.motivos)
    # siguió evaluando los demás killers
    assert evaluacion.pasos("killer")[-1].regla == "cheques_rechazados_impagos"


def test_variable_ausente_se_trata_como_no_disponible(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, igj={"antiguedad_anios": 5, "dada_de_baja": None})
    assert evaluacion.veredicto is Veredicto.APROBADO  # None es un valor (falso)

    consultor = armar_consultor({**BASE_PYME, "igj": {"antiguedad_anios": 5}})
    evaluacion = evaluar(solicitud_pyme(), reglas_pyme, consultor)
    baja = next(p for p in evaluacion.pasos("killer") if p.regla == "sociedad_dada_de_baja")
    assert baja.resultado == "no evaluable"
    assert "variable ausente" in baja.detalle["motivo"]


def test_un_consultor_que_explota_no_rompe_el_motor(reglas_pyme):
    class ConsultorRoto(ConsultorFalso):
        def variables(self, fuente):
            if fuente == "cheques":
                raise RuntimeError("boom")
            return super().variables(fuente)

    consultor = ConsultorRoto(armar_consultor(BASE_PYME).respuestas)
    evaluacion = evaluar(solicitud_pyme(), reglas_pyme, consultor)

    assert evaluacion.veredicto is Veredicto.DERIVAR
    cheques = next(p for p in evaluacion.pasos("killer") if p.regla == "cheques_rechazados_impagos")
    assert cheques.detalle["motivo"] == "cheques: error inesperado: RuntimeError: boom"


@pytest.mark.parametrize(
    "accion, veredicto, ultimo_killer",
    [
        ("derivar", Veredicto.DERIVAR, "sociedad_dada_de_baja"),
        ("rechazar", Veredicto.RECHAZADO, "sociedad_dada_de_baja"),
        ("continuar", Veredicto.DERIVAR, "cheques_rechazados_impagos"),
    ],
)
def test_si_no_disponible_se_configura_por_regla(reglas_pyme, accion, veredicto, ultimo_killer):
    reglas = reglas_pyme.con_killer("sociedad_dada_de_baja", si_no_disponible=accion)

    evaluacion, consultor = evaluar_pyme(reglas, igj=NoDisponible("5xx"))

    assert evaluacion.veredicto is veredicto
    assert evaluacion.pasos("killer")[-1].regla == ultimo_killer
    if accion != "continuar":
        assert "bureau" not in consultor.pedidas  # decidido: no se gasta en la fuente paga


def test_badlar_caida_deriva_sin_oferta(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, badlar=NoDisponible("timeout"))

    assert evaluacion.veredicto is Veredicto.DERIVAR
    assert evaluacion.rating == "A"
    assert evaluacion.oferta is None
    assert any("badlar: timeout" in m for m in evaluacion.motivos)


def test_bureau_caido_usa_puntos_sin_dato_y_deriva(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, bureau=NoDisponible("timeout"))

    puntos = {p.regla: p for p in evaluacion.pasos("scorecard")}
    assert puntos["score_bureau"].detalle["puntos"] == 10
    assert puntos["score_bureau"].resultado == "no evaluable"
    assert evaluacion.veredicto is Veredicto.DERIVAR
    assert evaluacion.oferta is not None


# --- derivaciones y precedencia -------------------------------------------


def test_documentos_con_baja_confianza_derivan(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, documentos={"confianza_minima": 0.6})

    assert evaluacion.veredicto is Veredicto.DERIVAR
    assert evaluacion.motivos[0].startswith("documentos_baja_confianza")


PERFIL_RIESGOSO = {
    "cendeu": {"peor_situacion_12m": 2, "entidades_con_deuda": 6, "deuda_total": 200_000_000},
    "igj": {"antiguedad_anios": 2},
    "bureau": {"score": 300},
}


def test_rating_e_rechaza(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, **PERFIL_RIESGOSO)

    assert evaluacion.rating == "E"
    assert evaluacion.veredicto is Veredicto.RECHAZADO
    assert evaluacion.motivos == ("politica: rating E",)


def test_rechazo_le_gana_a_derivar(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, **PERFIL_RIESGOSO | {"documentos": {"confianza_minima": 0.5}})

    assert evaluacion.veredicto is Veredicto.RECHAZADO


def test_derivar_ratings_es_configurable(reglas_pyme):
    reglas = reglas_pyme.con_politica(derivar_ratings=("A",))

    evaluacion, _ = evaluar_pyme(reglas)

    assert evaluacion.veredicto is Veredicto.DERIVAR
    assert evaluacion.motivos == ("politica: rating A",)


# --- política -------------------------------------------------------------


def test_la_oferta_se_recorta_a_la_capacidad_de_pago(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, solicitud=solicitud_pyme(monto=50_000_000))

    # cuota máxima = 10 % de 20 M de ventas mensuales; tasa mensual 3 %
    i = 0.03
    tope = 2_000_000 * (1 - (1 + i) ** -12) / i
    assert evaluacion.veredicto is Veredicto.APROBADO
    assert evaluacion.oferta.monto == math.floor(tope / 1000) * 1000
    assert evaluacion.oferta.cuota <= 2_000_000


def test_el_plazo_se_ajusta_al_rango_de_la_politica(reglas_pyme):
    largo, _ = evaluar_pyme(reglas_pyme, solicitud=solicitud_pyme(plazo_meses=60))
    corto, _ = evaluar_pyme(reglas_pyme, solicitud=solicitud_pyme(plazo_meses=1))

    assert largo.oferta.plazo_meses == 36
    assert corto.oferta.plazo_meses == 6


def test_oferta_por_debajo_del_minimo_rechaza(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, documentos={"ventas_anuales": 1_200_000})

    assert evaluacion.veredicto is Veredicto.RECHAZADO
    assert evaluacion.oferta is None
    assert evaluacion.motivos[0].startswith("politica: la oferta máxima")


def test_sin_documentos_no_hay_capacidad_de_pago_y_deriva(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, documentos=SinAntecedentes())

    assert evaluacion.veredicto is Veredicto.DERIVAR
    assert evaluacion.oferta is None


def test_un_valor_con_tipo_incorrecto_no_rompe_el_motor(reglas_pyme):
    evaluacion, _ = evaluar_pyme(reglas_pyme, cendeu={"peor_situacion_12m": "tres"})

    assert evaluacion.veredicto is Veredicto.DERIVAR
    situacion = next(p for p in evaluacion.pasos("killer") if p.regla == "situacion_bcra_3_o_peor")
    assert situacion.resultado == "no evaluable"
    assert "tipo" in situacion.detalle["motivo"]
