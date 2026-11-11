"""Catálogo de variables por flujo y validación de condiciones contra él (ADR 0007)."""

import pytest

from relevo.catalogo import catalogo, validar_condicion


def nombres(flujo: str) -> set[str]:
    return {v.nombre for v in catalogo(flujo)}


def test_el_catalogo_de_cada_flujo_tiene_solo_las_fuentes_que_consulta():
    pyme, mono = nombres("pyme"), nombres("monotributista")

    assert {"solicitud.monto", "cendeu.peor_situacion_12m", "igj.antiguedad_anios", "igj.sin_antecedentes",
            "documentos.ventas_anuales", "documentos.confianza_minima", "badlar.tasa"} <= pyme
    assert {"identidad.edad", "documentos.antiguedad_actividad_anios", "arca.categoria_monotributo"} <= mono
    assert not any(n.startswith("identidad.") for n in pyme)
    assert not any(n.startswith("igj.") for n in mono)
    assert "documentos.ventas_anuales" not in mono


def test_una_condicion_valida_dice_que_variables_usa():
    validacion = validar_condicion("pyme", "propios.lista_negra or cendeu.deuda_total > 50000000")

    assert validacion.ok
    assert validacion.usa == ("propios.lista_negra", "cendeu.deuda_total")


@pytest.mark.parametrize(
    "texto, mensaje",
    [
        ("identidad.edad < 18", "identidad.edad no está en el catálogo de PyME · Originación"),
        ("cendeu.peor_situacion_12 >= 3", "¿Quisiste decir cendeu.peor_situacion_12m?"),
        ("", "La condición está vacía"),
        ("propios.lista_negra or", "incompleta o mal escrita"),
        ("propios.lista_negra.algo", "fuente.variable"),
        ("open('x')", "funciones"),
        ("lista_negra", "fuente.variable"),
    ],
)
def test_una_condicion_invalida_explica_por_que(texto, mensaje):
    validacion = validar_condicion("pyme", texto)

    assert not validacion.ok
    assert mensaje in validacion.error


def test_cada_variable_tiene_tipo_y_descripcion():
    for variable in catalogo("pyme"):
        assert variable.tipo and variable.descripcion, variable.nombre
