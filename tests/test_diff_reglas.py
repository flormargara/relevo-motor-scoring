"""Diferencias entre dos versiones de reglas: lo que se revisa antes de publicar y en el historial."""

import copy

import yaml

from relevo.diff_reglas import Cambio, diferencias
from relevo.reglas import DIRECTORIO_MODELOS, DIRECTORIO_REGLAS, reglas_desde_dict

V1 = yaml.safe_load((DIRECTORIO_REGLAS / "pyme.v1.yaml").read_text())


def con(cambio) -> object:
    datos = copy.deepcopy(V1)
    cambio(datos)
    return reglas_desde_dict(datos, DIRECTORIO_MODELOS)


def test_sin_cambios_no_hay_diferencias():
    assert diferencias(con(lambda d: None), con(lambda d: None)) == []


def test_reglas_agregadas_quitadas_modificadas_y_reordenadas():
    def cambiar(d):
        d["killers"][2]["condicion"] = "propios.lista_negra or propios.creditos_previos > 3"
        d["killers"].pop(1)  # identificacion_rechazada_por_bcra
        d["killers"][0], d["killers"][1] = d["killers"][1], d["killers"][0]
        nueva = {"id": "monto_alto", "descripcion": "Pide mucho", "condicion": "solicitud.monto > 1000"}
        d["derivaciones"].append(nueva)

    cambios = diferencias(con(lambda d: None), con(cambiar))

    quitada = Cambio("quitada", "Killer identificacion_rechazada_por_bcra", "cendeu.identificacion_rechazada", None)
    assert quitada in cambios
    assert Cambio("agregada", "Derivación monto_alto", None, "solicitud.monto > 1000") in cambios
    assert (
        Cambio(
            "modificada",
            "Killer lista_negra · condición",
            "propios.lista_negra",
            "propios.lista_negra or propios.creditos_previos > 3",
        )
        in cambios
    )
    assert any(c.tipo == "orden" and c.que == "Orden de los killers" for c in cambios)


def test_decision_y_spread_por_rating_y_campos_de_la_politica():
    def cambiar(d):
        d["politica"]["derivar_ratings"] = ["D"]
        d["politica"]["spread_por_rating"]["A"] = 7
        d["politica"]["monto_minimo"] = 2_000_000

    cambios = diferencias(con(lambda d: None), con(cambiar))

    assert cambios == [
        Cambio("modificada", "Rating D · decisión", "aprueba", "deriva"),
        Cambio("modificada", "Rating A · spread (pp)", "6", "7"),
        Cambio("modificada", "Política · monto mínimo", "1.000.000", "2.000.000"),
    ]


def test_tambien_ve_la_capacidad_de_pago_de_monotributista_y_el_scorecard():
    base = yaml.safe_load((DIRECTORIO_REGLAS / "monotributista.v3.yaml").read_text())
    cambiado = copy.deepcopy(base)
    cambiado["politica"]["capacidad_pago"]["ingreso_anual_por_categoria"]["D"] = 22_000_000
    cambiado["scorecard"]["si_no_disponible"] = "derivar"

    cambios = diferencias(reglas_desde_dict(base, DIRECTORIO_MODELOS), reglas_desde_dict(cambiado, DIRECTORIO_MODELOS))

    assert cambios == [
        Cambio("modificada", "Scorecard · si falta un dato", "continuar", "derivar"),
        Cambio("modificada", "Política · ingreso anual de la categoría D", "21.000.000", "22.000.000"),
    ]
