"""Versiones de reglas guardadas en la base, con los YAML como semilla (ADR 0007)."""

from pathlib import Path

import pytest
import yaml

from relevo.reglas import DIRECTORIO_REGLAS, ReglasInvalidas
from relevo.repositorio import Repositorio
from relevo.versiones_reglas import VersionesReglas


def semilla(directorio: Path, *versiones: int) -> Path:
    """Escribe pyme.v<N>.yaml a partir de la v1 real, cambiando solo el número de versión."""
    datos = yaml.safe_load((DIRECTORIO_REGLAS / "pyme.v1.yaml").read_text())
    for version in versiones:
        texto = yaml.safe_dump(datos | {"version": version}, allow_unicode=True)
        (directorio / f"pyme.v{version}.yaml").write_text(texto)
    return directorio


def test_importa_la_semilla_y_la_vigente_es_la_ultima(tmp_path: Path):
    versiones = VersionesReglas(Repositorio())

    importadas = versiones.importar(semilla(tmp_path, 1, 2))

    assert importadas == ["pyme.v1", "pyme.v2"]
    assert versiones.reglas("pyme").id == "pyme.v2"
    assert versiones.reglas("pyme", version=1).id == "pyme.v1"


def test_importar_otra_vez_no_pisa_lo_que_ya_esta(tmp_path: Path):
    versiones = VersionesReglas(Repositorio())
    versiones.importar(semilla(tmp_path, 1))
    archivo = tmp_path / "pyme.v1.yaml"
    archivo.write_text(archivo.read_text().replace("lista_negra", "lista_negra_cambiada"))

    assert versiones.importar(tmp_path) == []
    assert "lista_negra" in [k.id for k in versiones.reglas("pyme", version=1).killers]


def test_flujo_o_version_inexistente():
    versiones = VersionesReglas(Repositorio())

    with pytest.raises(ReglasInvalidas, match="no hay reglas"):
        versiones.reglas("pyme")


def test_una_semilla_con_variables_fuera_del_catalogo_del_flujo_no_se_importa(tmp_path: Path):
    datos = yaml.safe_load((DIRECTORIO_REGLAS / "pyme.v1.yaml").read_text())
    datos["killers"][2]["condicion"] = "identidad.edad < 18"  # identidad no es fuente de PyME
    (tmp_path / "pyme.v1.yaml").write_text(yaml.safe_dump(datos, allow_unicode=True))

    with pytest.raises(ReglasInvalidas, match="identidad.edad no está en el catálogo"):
        VersionesReglas(Repositorio()).importar(tmp_path)


def test_una_semilla_invalida_no_se_importa(tmp_path: Path):
    (tmp_path / "pyme.v1.yaml").write_text("segmento: pyme\nversion: 1\nkillers: []\n")
    versiones = VersionesReglas(Repositorio())

    with pytest.raises(ReglasInvalidas):
        versiones.importar(tmp_path)
    with pytest.raises(ReglasInvalidas, match="no hay reglas"):
        versiones.reglas("pyme")
