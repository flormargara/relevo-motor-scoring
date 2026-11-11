"""Carga y validación de las reglas versionadas."""

import copy
import json
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
    assert reglas.id == f"{segmento}.v3"
    assert reglas.killers[0].id == "identificacion_invalida"
    assert not reglas.scorecard.provisional
    assert cargar_reglas(segmento, version=1).scorecard.provisional


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


def test_los_ratings_los_define_el_modelo(tmp_path: Path):
    modelo = json.loads((DIRECTORIO_MODELOS / "pyme.v1.json").read_text())
    modelo["ratings"] = [
        {"rating": "A", "pd_hasta": 0.03},
        {"rating": "B", "pd_hasta": 0.15},
        {"rating": "E", "pd_hasta": 1},
    ]
    (tmp_path / "pyme.v1.json").write_text(json.dumps(modelo))
    datos = modificar(lambda d: d.pop("ratings", None))
    datos["politica"]["spread_por_rating"] = {"A": 5, "B": 9}

    reglas = reglas_desde_dict(datos, tmp_path)

    assert [(r.nombre, r.pd_hasta) for r in reglas.ratings] == [("A", 0.03), ("B", 0.15), ("E", 1)]


@pytest.mark.parametrize(
    "cambio, mensaje",
    [
        (lambda r: r.pop(), "debe ser 1"),
        (lambda r: r.reverse(), "orden"),
        (lambda r: r.clear(), "vacío"),
    ],
)
def test_detecta_ratings_mal_formados_en_el_modelo(tmp_path: Path, cambio, mensaje):
    modelo = json.loads((DIRECTORIO_MODELOS / "pyme.v1.json").read_text())
    cambio(modelo["ratings"])
    (tmp_path / "pyme.v1.json").write_text(json.dumps(modelo))

    with pytest.raises(ReglasInvalidas, match=f"modelo pyme.v1.json.*{mensaje}"):
        reglas_desde_dict(datos_pyme(), tmp_path)


def test_las_reglas_no_pueden_declarar_ratings():
    datos = modificar(lambda d: d.update(ratings=[{"rating": "A", "pd_hasta": 1}]))

    with pytest.raises(ReglasInvalidas, match="ratings.*modelo"):
        reglas_desde_dict(datos, DIRECTORIO_MODELOS)


def test_las_fuentes_referenciadas(reglas_pyme):
    assert reglas_pyme.fuentes == ("cendeu", "propios", "igj", "cheques", "documentos", "bureau", "badlar")
