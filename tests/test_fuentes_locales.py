"""IGJ (snapshot), fuentes de juguete y documentos."""

import zipfile
from datetime import date
from pathlib import Path

import pytest

from relevo.consultor import Disponible, SinAntecedentes
from relevo.fuentes import FuenteCaida
from relevo.fuentes.documentos import Documentos
from relevo.fuentes.igj import Igj, armar_snapshot
from relevo.fuentes.juguete import Bureau, Identidad, Propios, cargar_juguete

HOY = date(2026, 11, 10)

ENTIDADES = """﻿numero_correlativo,tipo_societario,descripcion_tipo_societario,razon_social,dada_de_baja,codigo_baja,detalle_baja,cuit
1,50,SOC. ANONIMA,YPF,,,,30546689979
2,50,SOC. ANONIMA,VIEJA SA,S,00147,CANCELACION,30111111118
3,50,SOC. ANONIMA,SIN CUIT SA,,,,
4,53,S.R.L.,NUEVA SRL,,,,30222222229
5,50,SOC. ANONIMA,YPF LEGAJO VIEJO,S,00100,FUSION,30546689979
"""
BALANCES = """﻿numero_correlativo,tipo_societario,descripcion_tipo_societario,razon_social,fecha_balance,fecha_presentacion,capital_informado
1,50,SOC. ANONIMA,YPF,19911231,19930526,"0,00"
2,50,SOC. ANONIMA,VIEJA SA,20000630,20001010,"1,00"
5,50,SOC. ANONIMA,YPF LEGAJO VIEJO,19850630,19851010,"1,00"
4,53,S.R.L.,NUEVA SRL,99999999,20260101,"1,00"
"""
ASAMBLEAS = """﻿numero_correlativo,tipo_societario,descripcion_tipo_societario,razon_social,tipo_asamblea,descripcion_tipo_asamblea,numero_asamblea,fecha_realizacion,fecha_presentacion
1,50,SOC. ANONIMA,YPF,O,Ordinaria,1,19921125,19930526
4,53,S.R.L.,NUEVA SRL,O,Ordinaria,1,20260301,20260310
"""


@pytest.fixture
def snapshot_igj(tmp_path: Path) -> Path:
    zip_igj = tmp_path / "igj.zip"
    with zipfile.ZipFile(zip_igj, "w") as archivo:
        archivo.writestr("igj-entidades-202609.csv", ENTIDADES)
        archivo.writestr("igj-balances-202609.csv", BALANCES)
        archivo.writestr("igj-asambleas-202609.csv", ASAMBLEAS)
        archivo.writestr("igj-entidades-202607.csv", "numero_correlativo,cuit\n")  # período viejo: se ignora
    destino = tmp_path / "igj.sqlite"
    assert armar_snapshot(zip_igj, destino, hoy=HOY) == 3
    return destino


def igj(ruta):
    adapter = Igj(ruta, hoy=lambda: HOY)
    return lambda cuit: adapter.traducir(adapter.consultar(cuit, timeout=1))


def test_igj_sociedad_activa_con_antiguedad_desde_el_primer_registro(snapshot_igj):
    variables = igj(snapshot_igj)("30546689979").variables

    # dos legajos con el mismo CUIT: vale el activo, y la fecha más vieja de los dos
    assert variables["razon_social"] == "YPF"
    assert variables["dada_de_baja"] is False
    assert variables["fecha_primer_registro"] == "1985-06-30"
    assert variables["antiguedad_anios"] == pytest.approx(41.36, abs=0.01)
    assert variables["periodo_snapshot"] == "202609"


def test_igj_sociedad_dada_de_baja(snapshot_igj):
    variables = igj(snapshot_igj)("30111111118").variables

    assert variables["dada_de_baja"] is True
    assert variables["detalle_baja"] == "CANCELACION"


def test_igj_ignora_fechas_invalidas(snapshot_igj):
    variables = igj(snapshot_igj)("30222222229").variables

    assert variables["fecha_primer_registro"] == "2026-03-01"
    assert variables["antiguedad_anios"] < 1


def test_igj_cuit_que_no_esta_es_sin_antecedentes(snapshot_igj):
    assert igj(snapshot_igj)("30500959629") == SinAntecedentes()


def test_igj_sin_snapshot_es_fuente_caida(tmp_path):
    with pytest.raises(FuenteCaida, match="snapshot"):
        Igj(tmp_path / "no_existe.sqlite").consultar("30546689979", timeout=1)


def test_igj_sin_fecha_conocida_la_antiguedad_es_sin_dato(tmp_path):
    zip_igj = tmp_path / "igj.zip"
    with zipfile.ZipFile(zip_igj, "w") as archivo:
        archivo.writestr("igj-entidades-202609.csv", ENTIDADES)
        archivo.writestr("igj-balances-202609.csv", BALANCES.splitlines()[0] + "\n")
        archivo.writestr("igj-asambleas-202609.csv", ASAMBLEAS.splitlines()[0] + "\n")
    armar_snapshot(zip_igj, tmp_path / "igj.sqlite", hoy=HOY)

    assert igj(tmp_path / "igj.sqlite")("30546689979").variables["antiguedad_anios"] is None


# --- juguete --------------------------------------------------------------


def consultar(adapter, cuit):
    return adapter.traducir(adapter.consultar(cuit, timeout=1))


def test_bureau_usa_el_caso_configurado_o_un_valor_determinista():
    datos = {"bureau": {"casos": {"30546689979": {"score": 880}}}}

    assert consultar(Bureau(datos), "30546689979") == Disponible({"score": 880})
    otro = consultar(Bureau(datos), "30999999995").variables["score"]
    assert 300 <= otro <= 950
    assert consultar(Bureau(datos), "30999999995").variables["score"] == otro


def test_juguete_puede_simular_una_caida():
    with pytest.raises(FuenteCaida):
        Bureau({"bureau": {"caidas": ["30546689979"]}}).consultar("30546689979", timeout=1)


def test_lista_negra():
    propios = Propios({"propios": {"lista_negra": ["30999999995"]}})

    assert consultar(propios, "30999999995").variables["lista_negra"] is True
    assert consultar(propios, "30546689979").variables["lista_negra"] is False


def test_identidad_calcula_la_edad_a_la_fecha_de_consulta():
    datos = {"identidad": {"casos": {"27301234568": {"fecha_nacimiento": "1986-11-11"}}}}

    assert consultar(Identidad(datos, hoy=lambda: HOY), "27301234568").variables["edad"] == 39
    assert consultar(Identidad(datos, hoy=lambda: date(2026, 11, 11)), "27301234568").variables["edad"] == 40
    assert 25 <= consultar(Identidad(datos, hoy=lambda: HOY), "20111111112").variables["edad"] <= 71


def test_el_archivo_de_juguete_carga():
    assert "bureau" in cargar_juguete()


# --- documentos -----------------------------------------------------------


def test_documentos():
    assert consultar(Documentos({"ventas_anuales": 1}), "x") == Disponible({"ventas_anuales": 1})
    assert consultar(Documentos(None), "x") == SinAntecedentes()


def test_identidad_declarada_expone_edad_origen_y_no_validada():
    from relevo.fuentes.identidad import IdentidadDeclarada

    adapter = IdentidadDeclarada(date(1986, 11, 11), hoy=lambda: HOY)

    variables = consultar(adapter, "27301234568").variables
    assert variables == {"edad": 39, "validada": False, "origen": "declarada"}


def test_identidad_declarada_sin_fecha_es_sin_antecedentes():
    from relevo.consultor import SinAntecedentes
    from relevo.fuentes.identidad import IdentidadDeclarada

    assert isinstance(consultar(IdentidadDeclarada(None, hoy=lambda: HOY), "27301234568"), SinAntecedentes)
