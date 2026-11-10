"""Carga y validación de las reglas versionadas."""

import copy
from pathlib import Path

import pytest
import yaml

from relevo.reglas import DIRECTORIO_MODELOS, DIRECTORIO_REGLAS, ReglasInvalidas, cargar_reglas, reglas_desde_dict


def datos_pyme():
    return yaml.safe_load((DIRECTORIO_REGLAS / "pyme.v1.yaml").read_text())


@pytest.mark.parametrize("segmento", ["pyme", "monotributista"])
def test_carga_las_reglas_de_cada_segmento(segmento):
    reglas = cargar_reglas(segmento)

    assert reglas.segmento == segmento
    assert reglas.id == f"{segmento}.v1"
    assert reglas.killers[0].id == "identificacion_invalida"
    assert reglas.scorecard.provisional


def test_toma_la_ultima_version_o_la_pedida(tmp_path: Path):
    for version in (1, 2, 10):
        datos = datos_pyme() | {"version": version}
        (tmp_path / f"pyme.v{version}.yaml").write_text(yaml.safe_dump(datos, allow_unicode=True))

    assert cargar_reglas("pyme", directorio_reglas=tmp_path).version == 10
    assert cargar_reglas("pyme", version=2, directorio_reglas=tmp_path).version == 2


def test_segmento_o_version_inexistente():
    with pytest.raises(ReglasInvalidas, match="no hay reglas"):
        cargar_reglas("cooperativa")
    with pytest.raises(ReglasInvalidas, match="no hay reglas"):
        cargar_reglas("pyme", version=99)


def test_la_version_del_archivo_debe_coincidir(tmp_path: Path):
    (tmp_path / "pyme.v3.yaml").write_text(yaml.safe_dump(datos_pyme()))

    with pytest.raises(ReglasInvalidas, match="versión"):
        cargar_reglas("pyme", directorio_reglas=tmp_path)


def modificar(cambio):
    datos = copy.deepcopy(datos_pyme())
    cambio(datos)
    return datos


@pytest.mark.parametrize(
    "cambio, mensaje",
    [
        (lambda d: d["killers"][1].update(condicion="propios.lista_negra =="), "sintaxis"),
        (lambda d: d["killers"][1].update(condicion="open('x')"), "no permitida"),
        (lambda d: d["killers"][1].update(si_no_disponible="ignorar"), "si_no_disponible"),
        (lambda d: d["killers"][1].pop("condicion"), "condicion"),
        (lambda d: d["killers"].append(dict(d["killers"][1])), "repetido"),
        (lambda d: d["killers"][1].update(condicion="solicitud.ingresos > 0"), "solicitud.ingresos"),
        (lambda d: d["ratings"].pop(), "1"),
        (lambda d: d["ratings"].reverse(), "orden"),
        (lambda d: d["politica"]["spread_por_rating"].pop("C"), "spread"),
        (lambda d: d["politica"]["capacidad_pago"].update(tipo="magia"), "capacidad_pago"),
        (lambda d: d["politica"].update(rechazar_ratings=["Z"]), "Z"),
        (lambda d: d["scorecard"].update(modelo="no_existe.json"), "no_existe.json"),
        (lambda d: d.pop("politica"), "politica"),
    ],
)
def test_detecta_reglas_mal_formadas_al_cargar(cambio, mensaje):
    with pytest.raises(ReglasInvalidas, match=mensaje):
        reglas_desde_dict(modificar(cambio), DIRECTORIO_MODELOS)


def test_modelo_mal_formado(tmp_path: Path):
    (tmp_path / "pyme.v1.json").write_text('{"escala": {"puntos_base": 1}, "caracteristicas": []}')

    with pytest.raises(ReglasInvalidas, match="modelo"):
        reglas_desde_dict(datos_pyme(), tmp_path)


def test_las_fuentes_referenciadas(reglas_pyme):
    assert reglas_pyme.fuentes == ("cendeu", "propios", "igj", "cheques", "documentos", "bureau", "badlar")
