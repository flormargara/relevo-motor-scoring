"""Versiones de reglas guardadas en la base, con los YAML como semilla (ADR 0007)."""

from pathlib import Path

import pytest
import yaml

from relevo.reglas import DIRECTORIO_REGLAS, ReglasInvalidas
from relevo.repositorio import Repositorio
from relevo.versiones_reglas import ConflictoDeVersion, VersionesReglas


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


def contenido_v1() -> dict:
    return yaml.safe_load((DIRECTORIO_REGLAS / "pyme.v1.yaml").read_text())


def test_publicar_crea_la_version_siguiente_y_pasa_a_ser_la_vigente(tmp_path: Path):
    versiones = VersionesReglas(Repositorio())
    versiones.importar(semilla(tmp_path, 1))
    borrador = contenido_v1()
    borrador["politica"]["derivar_ratings"] = ["D"]

    publicada = versiones.publicar("pyme", borrador, autor="Ana", motivo="D pasa a analista", base=1)

    assert publicada.id == "pyme.v2"
    assert versiones.reglas("pyme").politica.derivar_ratings == ("D",)
    assert versiones.reglas("pyme", version=1).politica.derivar_ratings == ()
    guardada = versiones.repositorio.version_reglas("pyme", 2)
    assert (guardada.autor, guardada.motivo, guardada.base) == ("Ana", "D pasa a analista", 1)


def test_publicar_sobre_una_version_que_ya_no_es_la_vigente_es_un_conflicto(tmp_path: Path):
    versiones = VersionesReglas(Repositorio())
    versiones.importar(semilla(tmp_path, 1, 2))

    with pytest.raises(ConflictoDeVersion, match="v2"):
        versiones.publicar("pyme", contenido_v1(), autor="Ana", motivo="x", base=1)


@pytest.mark.parametrize(
    "cambio, autor, motivo, mensaje",
    [
        (lambda d: d["killers"][2].update(condicion="identidad.edad < 18"), "Ana", "x", "identidad.edad"),
        (lambda d: d["killers"][2].update(condicion="propios.lista_negra =="), "Ana", "x", "incompleta"),
        (lambda d: d["politica"].update(derivar_ratings=["D"]), " ", "x", "autor"),
        (lambda d: d["politica"].update(derivar_ratings=["D"]), "Ana", "", "motivo"),
        (lambda d: None, "Ana", "x", "no tiene cambios"),
    ],
)
def test_publicar_valida_antes_de_guardar(tmp_path: Path, cambio, autor, motivo, mensaje):
    versiones = VersionesReglas(Repositorio())
    versiones.importar(semilla(tmp_path, 1))
    borrador = contenido_v1()
    cambio(borrador)

    with pytest.raises(ReglasInvalidas, match=mensaje):
        versiones.publicar("pyme", borrador, autor=autor, motivo=motivo, base=1)
    assert versiones.reglas("pyme").id == "pyme.v1"


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


def test_el_borrador_es_uno_por_flujo_y_se_guarda_sin_afectar_la_vigente(tmp_path: Path):
    versiones = VersionesReglas(Repositorio())
    versiones.importar(semilla(tmp_path, 1))
    assert versiones.borrador("pyme") is None

    a_medias = contenido_v1()
    a_medias["killers"][2]["condicion"] = "propios.lista_negra or"  # se guarda aunque todavía no valide
    versiones.guardar_borrador("pyme", a_medias, base=1)

    borrador = versiones.borrador("pyme")
    assert borrador.base == 1
    assert borrador.contenido["killers"][2]["condicion"] == "propios.lista_negra or"
    assert versiones.reglas("pyme").id == "pyme.v1"


def test_publicar_y_descartar_cierran_el_borrador(tmp_path: Path):
    versiones = VersionesReglas(Repositorio())
    versiones.importar(semilla(tmp_path, 1))
    cambiado = contenido_v1()
    cambiado["politica"]["derivar_ratings"] = ["D"]

    versiones.guardar_borrador("pyme", cambiado, base=1)
    versiones.publicar("pyme", cambiado, autor="Ana", motivo="x", base=1)
    assert versiones.borrador("pyme") is None

    versiones.guardar_borrador("pyme", contenido_v1(), base=2)
    versiones.descartar_borrador("pyme")
    assert versiones.borrador("pyme") is None
