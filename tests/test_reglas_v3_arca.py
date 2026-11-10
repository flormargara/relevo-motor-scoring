"""Reglas v3: la condición tributaria de ARCA entra al motor."""

import pytest

from conftest import BASE_MONOTRIBUTISTA, BASE_PYME, armar_consultor, solicitud_monotributista, solicitud_pyme
from relevo.consultor import NoDisponible, SinAntecedentes
from relevo.dominio import Veredicto
from relevo.motor import evaluar
from relevo.reglas import cargar_reglas


def correr(segmento, base, solicitud, **cambios):
    reglas = cargar_reglas(segmento, version=3)
    return evaluar(solicitud, reglas, armar_consultor(base, **cambios))


@pytest.mark.parametrize(
    ("segmento", "base", "solicitud"),
    [
        ("pyme", BASE_PYME, solicitud_pyme()),
        ("monotributista", BASE_MONOTRIBUTISTA, solicitud_monotributista()),
    ],
)
class TestAmbosSegmentos:
    def test_sin_inscripcion_vigente_rechaza(self, segmento, base, solicitud):
        evaluacion = correr(segmento, base, solicitud, arca={"inscripcion_vigente": False})

        assert evaluacion.veredicto is Veredicto.RECHAZADO
        assert [m.split(":")[0] for m in evaluacion.motivos] == ["sin_inscripcion_vigente"]

    def test_cuit_fuera_del_padron_deriva(self, segmento, base, solicitud):
        evaluacion = correr(segmento, base, solicitud, arca=SinAntecedentes())

        assert evaluacion.veredicto is Veredicto.DERIVAR
        assert any(m.startswith("sin_antecedentes_arca") for m in evaluacion.motivos)

    def test_arca_caida_no_rompe_ni_rechaza(self, segmento, base, solicitud):
        evaluacion = correr(segmento, base, solicitud, arca=NoDisponible("sin snapshot"))

        assert evaluacion.veredicto is not Veredicto.RECHAZADO

    def test_inscripcion_vigente_aprueba(self, segmento, base, solicitud):
        assert correr(segmento, base, solicitud).veredicto is Veredicto.APROBADO


def test_la_capacidad_de_pago_usa_la_categoria_de_arca():
    sin_ajuste = correr("monotributista", BASE_MONOTRIBUTISTA, solicitud_monotributista(monto=50_000_000))
    categoria_a = correr(
        "monotributista",
        BASE_MONOTRIBUTISTA,
        solicitud_monotributista(monto=50_000_000),
        arca={"categoria_monotributo": "A"},
    )

    assert categoria_a.oferta.monto < sin_ajuste.oferta.monto


def test_sin_categoria_en_arca_la_oferta_deriva():
    evaluacion = correr(
        "monotributista",
        BASE_MONOTRIBUTISTA,
        solicitud_monotributista(monto=50_000_000),
        arca={"categoria_monotributo": None},
    )

    assert evaluacion.veredicto is Veredicto.DERIVAR


def test_monotributista_sin_fecha_de_nacimiento_deriva():
    evaluacion = correr("monotributista", BASE_MONOTRIBUTISTA, solicitud_monotributista(), identidad=SinAntecedentes())

    assert evaluacion.veredicto is Veredicto.DERIVAR
    assert any(m.startswith("sin_fecha_de_nacimiento") for m in evaluacion.motivos)
