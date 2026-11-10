"""Evaluador de condiciones: el único internal seam del motor con tests propios."""

import pytest

from relevo.condiciones import CondicionInvalida, Expresion, SinDato


def resolver_desde(valores):
    def resolver(fuente, variable):
        return valores[f"{fuente}.{variable}"]

    return resolver


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("cendeu.peor_situacion_12m >= 3", True),
        ("cendeu.peor_situacion_12m < 3", False),
        ("cendeu.peor_situacion_12m >= 3 and propios.lista_negra", False),
        ("cendeu.peor_situacion_12m >= 3 or propios.lista_negra", True),
        ("not propios.lista_negra", True),
        ("1 <= cendeu.peor_situacion_12m <= 5", True),
        ("documentos.categoria in ['A', 'B']", True),
        ("documentos.categoria not in ('A', 'B')", False),
        ("cendeu.deuda_total / documentos.ventas_anuales > 0.5", False),
        ("-cendeu.peor_situacion_12m == -4", True),
    ],
)
def test_evalua_condiciones(texto, esperado):
    valores = {
        "cendeu.peor_situacion_12m": 4,
        "propios.lista_negra": False,
        "documentos.categoria": "A",
        "cendeu.deuda_total": 100,
        "documentos.ventas_anuales": 1000,
    }
    assert Expresion(texto).evaluar(resolver_desde(valores)) is esperado


def test_lista_las_variables_referenciadas():
    expresion = Expresion("cendeu.deuda_total / documentos.ventas_anuales > 0.5 and not propios.lista_negra")
    assert expresion.referencias == (
        ("cendeu", "deuda_total"),
        ("documentos", "ventas_anuales"),
        ("propios", "lista_negra"),
    )


def test_cortocircuito_no_resuelve_lo_que_no_hace_falta():
    pedidas = []

    def resolver(fuente, variable):
        pedidas.append(fuente)
        return {"propios": False, "bureau": 900}[fuente]

    Expresion("propios.lista_negra and bureau.score > 500").evaluar(resolver)

    assert pedidas == ["propios"]


@pytest.mark.parametrize(
    "texto",
    [
        "__import__('os').system('rm -rf /')",
        "cendeu.deuda.__class__",
        "lambda: 1",
        "cendeu.x if True else 2",
        "[x for x in cendeu.lista]",
        "deuda_total > 3",
        "cendeu.peor_situacion_12m >=",
        "cendeu.x ** 1000",
    ],
)
def test_rechaza_lo_que_no_esta_en_la_lista_blanca(texto):
    with pytest.raises(CondicionInvalida):
        Expresion(texto)


def test_division_por_cero_es_sin_dato():
    with pytest.raises(SinDato):
        Expresion("cendeu.deuda_total / documentos.ventas_anuales").evaluar(
            resolver_desde({"cendeu.deuda_total": 1, "documentos.ventas_anuales": 0})
        )


def test_registra_los_valores_usados():
    valores_usados = {}
    Expresion("cendeu.peor_situacion_12m >= 3").evaluar(
        resolver_desde({"cendeu.peor_situacion_12m": 1}), valores_usados
    )
    assert valores_usados == {"cendeu.peor_situacion_12m": 1}
