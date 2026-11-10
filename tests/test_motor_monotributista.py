"""Comportamiento del motor para monotributistas, a través de `evaluar`."""

import pytest

from conftest import BASE_MONOTRIBUTISTA, CUIT_PYME, armar_consultor, cuota_francesa, solicitud_monotributista
from relevo.dominio import Veredicto
from relevo.motor import evaluar


def evaluar_mono(reglas, solicitud=None, **cambios):
    consultor = armar_consultor(BASE_MONOTRIBUTISTA, **cambios)
    return evaluar(solicitud or solicitud_monotributista(), reglas, consultor)


def test_monotributista_sano_queda_aprobado(reglas_monotributista):
    evaluacion = evaluar_mono(reglas_monotributista)

    # 45 + 30 + 35 + 35 + 25 = 170 → odds = 20·2^(20/30)
    assert evaluacion.puntos == 170
    assert evaluacion.rating == "B"
    assert evaluacion.veredicto is Veredicto.APROBADO
    assert evaluacion.oferta.tasa_nominal_anual == 42.0  # 30 + spread B 12
    assert evaluacion.oferta.cuota == cuota_francesa(2_000_000, 42, 12)


def test_la_categoria_define_la_capacidad_de_pago(reglas_monotributista):
    evaluacion = evaluar_mono(
        reglas_monotributista,
        solicitud=solicitud_monotributista(monto=20_000_000),
        documentos={"categoria_monotributo": "A"},
    )

    # categoría A: 8 M anuales → cuota máxima 30 % de 666.666,67
    assert evaluacion.oferta.cuota <= 200_000.01
    assert evaluacion.oferta.monto < 20_000_000


@pytest.mark.parametrize("edad", [17, 76])
def test_edad_fuera_de_rango_rechaza(reglas_monotributista, edad):
    evaluacion = evaluar_mono(reglas_monotributista, identidad={"edad": edad})

    assert evaluacion.veredicto is Veredicto.RECHAZADO
    assert evaluacion.motivos[0].startswith("edad_fuera_de_rango")


def test_un_cuit_de_persona_juridica_no_es_monotributista(reglas_monotributista):
    evaluacion = evaluar_mono(reglas_monotributista, solicitud=solicitud_monotributista(cuit=CUIT_PYME))

    assert evaluacion.veredicto is Veredicto.RECHAZADO
    assert evaluacion.motivos[0].startswith("identificacion_invalida")


def test_categoria_desconocida_deriva_sin_oferta(reglas_monotributista):
    evaluacion = evaluar_mono(reglas_monotributista, documentos={"categoria_monotributo": "Z"})

    assert evaluacion.veredicto is Veredicto.DERIVAR
    assert evaluacion.oferta is None
    assert any("categoría de monotributo desconocida: 'Z'" in m for m in evaluacion.motivos)
