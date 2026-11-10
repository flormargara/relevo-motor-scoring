"""Cartera sintética y entrenamiento del scorecard."""

import json

import pytest

pytest.importorskip("sklearn", reason="instalar el extra `sintetico`")

from relevo.reglas import DIRECTORIO_MODELOS, cargar_reglas  # noqa: E402
from relevo.sintetico import ESPECIFICACION, entrenar, generar_cartera, main  # noqa: E402

SEGMENTOS = ["pyme", "monotributista"]


@pytest.fixture(scope="module", params=SEGMENTOS)
def entrenado(request):
    segmento = request.param
    cartera = generar_cartera(segmento, filas=20_000)
    return segmento, cartera, entrenar(segmento, cartera, version=99)


def test_la_cartera_es_deterministica():
    assert generar_cartera("pyme", filas=500).equals(generar_cartera("pyme", filas=500))
    assert not generar_cartera("pyme", filas=500).equals(generar_cartera("pyme", filas=500, semilla=1))


def test_la_cartera_tiene_faltantes_como_en_produccion(entrenado):
    segmento, cartera, _ = entrenado
    assert 0.03 < cartera["default"].mean() < 0.12
    assert 0.10 < cartera["sin_antecedentes_bcra"].mean() < 0.25
    assert cartera.loc[cartera["sin_antecedentes_bcra"], "peor_situacion_12m"].isna().all()
    assert 0.02 < cartera["score_bureau"].isna().mean() < 0.06
    if segmento == "pyme":
        assert 0.55 < cartera["antiguedad_anios"].isna().mean() < 0.75  # IGJ solo cubre CABA


def test_discrimina_y_esta_calibrado(entrenado):
    _, _, modelo = entrenado
    metricas = modelo["metricas"]

    assert metricas["auc"] > 0.68
    assert metricas["pd_media_validacion"] == pytest.approx(metricas["tasa_default_validacion"], abs=0.01)
    observados = [r["default_observado"] for r in metricas["por_rating"] if r["participacion"] > 0.01]
    assert observados == sorted(observados)  # el default crece de A a E


def test_los_puntos_son_monotonos(entrenado):
    segmento, _, modelo = entrenado
    for spec, caracteristica in zip(ESPECIFICACION[segmento], modelo["caracteristicas"], strict=True):
        assert caracteristica["coeficiente"] < 0, spec.nombre  # WoE alto = menos riesgo
        if spec.monotona:
            puntos = [t["puntos"] for t in caracteristica["tramos"]]
            assert puntos == sorted(puntos) or puntos == sorted(puntos, reverse=True), spec.nombre


@pytest.mark.parametrize("segmento", SEGMENTOS)
def test_los_modelos_versionados_se_reproducen_con_su_semilla(segmento, tmp_path):
    archivo = main([segmento, "--version", "2", "--destino", str(tmp_path)])

    nuevo = json.loads(archivo.read_text())
    versionado = json.loads((DIRECTORIO_MODELOS / f"{segmento}.v2.json").read_text())
    assert nuevo["caracteristicas"] == versionado["caracteristicas"]


@pytest.mark.parametrize("segmento", SEGMENTOS)
def test_el_modelo_versionado_carga_en_las_reglas(segmento):
    reglas = cargar_reglas(segmento)

    assert reglas.scorecard.modelo == f"{segmento}.v2.json"
    assert not reglas.scorecard.provisional
