"""ARCA: snapshot del padrón público de condición tributaria."""

import zipfile
from datetime import date
from pathlib import Path

import pytest

from relevo.consultor import SinAntecedentes
from relevo.fuentes import FuenteCaida
from relevo.fuentes.arca import Arca, armar_snapshot, leer_linea

# Las tres primeras líneas son reales (padrón del 2026-11-07). Las de personas son
# sintéticas, con CUIT inventados, para no versionar datos de personas reales.


def linea(cuit, denominacion, ganancias, iva, monotributo, sociedad, empleador, actividad):
    return f"{cuit}{denominacion:<30}{ganancias:<2}{iva:<2}{monotributo:<2}{sociedad:<1}{empleador:<1}{actividad:<2}\n"


PADRON = (
    "30500010912BANCO DE LA NACION ARGENTINA  ACACNINS00\n"
    "30540088213GARBARINO S A                 NINININS00\n"
    "30546689979YPF SOCIEDAD ANONIMA          ACACNINS00\n"
    + linea("20111111112", "PEÑA PERSONA SINTETICA", "NI", "NI", "C", "N", "N", "08")
    + linea("27222222223", "OTRA PERSONA SINTETICA", "NI", "NI", "AL", "N", "N", "10")
    + linea("23333333334", "SIN DATOS SINTETICA", "", "", "D", "N", "", "08")
    + "linea que no es un registro\n"
)


@pytest.fixture
def snapshot_arca(tmp_path: Path) -> Path:
    zip_arca = tmp_path / "arca.zip"
    with zipfile.ZipFile(zip_arca, "w") as archivo:
        archivo.writestr("utlfile/padr/SELE-SAL-CONSTA.p20out1.20261107.tmp", PADRON.encode("latin-1"))
    destino = tmp_path / "arca.sqlite"
    assert armar_snapshot(zip_arca, destino, hoy=date(2026, 11, 10)) == 6
    return destino


def arca(ruta):
    adapter = Arca(ruta)
    return lambda cuit: adapter.traducir(adapter.consultar(cuit, timeout=1))


def test_arca_empresa_inscripta(snapshot_arca):
    variables = arca(snapshot_arca)("30500010912").variables

    assert set(variables) == set(Arca.variables)  # contrato con el catálogo (ADR 0007)
    assert variables == {
        "denominacion": "BANCO DE LA NACION ARGENTINA",
        "ganancias": "activo",
        "iva": "activo",
        "es_monotributista": False,
        "categoria_monotributo": None,
        "tipo_monotributo": None,
        "actividad_monotributo": None,
        "empleador": True,
        "integrante_sociedad": False,
        "inscripcion_vigente": True,
        "fecha_padron": "2026-11-07",
    }


def test_arca_empresa_sin_inscripcion_vigente(snapshot_arca):
    """Garbarino (en concurso) figura como no inscripta en Ganancias ni en IVA."""
    variables = arca(snapshot_arca)("30540088213").variables

    assert variables["ganancias"] == "no_inscripto"
    assert variables["iva"] == "no_inscripto"
    assert variables["inscripcion_vigente"] is False


def test_arca_monotributista_general_lee_latin1(snapshot_arca):
    variables = arca(snapshot_arca)("20111111112").variables

    assert variables["denominacion"] == "PEÑA PERSONA SINTETICA"
    assert variables["es_monotributista"] is True
    assert variables["categoria_monotributo"] == "C"
    assert variables["tipo_monotributo"] == "general"
    assert variables["actividad_monotributo"] == "servicios_locacion"
    assert variables["empleador"] is False
    assert variables["inscripcion_vigente"] is True


def test_arca_monotributo_social_es_categoria_a(snapshot_arca):
    variables = arca(snapshot_arca)("27222222223").variables

    assert variables["categoria_monotributo"] == "A"
    assert variables["tipo_monotributo"] == "social"
    assert variables["actividad_monotributo"] == "ventas"


def test_arca_campos_en_blanco_son_sin_dato(snapshot_arca):
    variables = arca(snapshot_arca)("23333333334").variables

    assert variables["ganancias"] is None
    assert variables["iva"] is None
    assert variables["empleador"] is None
    assert variables["categoria_monotributo"] == "D"


def test_arca_cuit_que_no_esta_es_sin_antecedentes(snapshot_arca):
    assert arca(snapshot_arca)("30500959629") == SinAntecedentes()
    assert arca(snapshot_arca)("123") == SinAntecedentes()


def test_arca_sin_snapshot_es_fuente_caida(tmp_path):
    with pytest.raises(FuenteCaida):
        Arca(tmp_path / "no_existe.sqlite").consultar("30500010912", timeout=1)


def test_leer_linea_ignora_lo_que_no_es_un_registro():
    assert leer_linea("linea que no es un registro\n") is None
    assert leer_linea("30500010912BANCO DE LA NACION ARGENTINA  ACACNINS00\r\n")["actividad_monotributo"] == "00"
