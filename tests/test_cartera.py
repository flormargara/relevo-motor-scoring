"""Seam FuenteDeCartera: los adapters cumplen el mismo contrato."""

from datetime import date

import pytest

from relevo.cartera import (
    CarteraEnArchivos,
    CarteraEnMemoria,
    CarteraInvalida,
    Desembolso,
    EstadoMensual,
    RegistroCartera,
    escribir_en_archivos,
)


def _estado(mes: int, dias_mora: int = 0) -> EstadoMensual:
    return EstadoMensual(date(2026, mes, 1), saldo=1000.0, pagado=0.0 if dias_mora else 100.0, dias_mora=dias_mora)


SANO = RegistroCartera(
    "sano",
    convertida=True,
    desembolso=Desembolso(date(2025, 12, 20), 1200.0, 12, 60.0),
    estados=(_estado(1), _estado(2), _estado(3)),
)
MOROSO = RegistroCartera(
    "moroso",
    convertida=True,
    desembolso=Desembolso(date(2025, 12, 5), 1200.0, 12, 60.0),
    estados=(_estado(1), _estado(2, 30), _estado(3, 60), _estado(4, 90), _estado(5, 120)),
)
NO_CONVERTIDA = RegistroCartera("no-convertida", convertida=False)
REGISTROS = [SANO, MOROSO, NO_CONVERTIDA]


@pytest.fixture(params=["memoria", "archivos"])
def fuente(request, tmp_path):
    if request.param == "memoria":
        return CarteraEnMemoria(REGISTROS)
    return CarteraEnArchivos(escribir_en_archivos(REGISTROS, tmp_path))


def test_devuelve_lo_que_informa_la_entidad(fuente):
    assert sorted(fuente.registros(), key=lambda r: r.solicitud_id) == sorted(REGISTROS, key=lambda r: r.solicitud_id)


def test_filtra_por_solicitud_y_omite_las_no_informadas(fuente):
    registros = fuente.registros(["moroso", "desconocida", "no-convertida", "moroso"])

    assert registros == [MOROSO, NO_CONVERTIDA]


def test_mora_y_default():
    assert SANO.peor_mora() == 0
    assert not SANO.en_default()
    assert MOROSO.peor_mora(meses=3) == 60
    assert not MOROSO.en_default(meses=3)
    assert MOROSO.en_default()
    assert MOROSO.meses_observados() == 5
    assert NO_CONVERTIDA.peor_mora() == 0


def test_la_entidad_declara_la_columna_del_id(tmp_path):
    escribir_en_archivos(REGISTROS, tmp_path)
    for archivo in tmp_path.glob("*.csv"):
        archivo.write_text(archivo.read_text().replace("solicitud_id", "nro_tramite", 1))

    assert len(CarteraEnArchivos(tmp_path, columna_solicitud="nro_tramite").registros()) == 3
    with pytest.raises(CarteraInvalida, match="nro_tramite|solicitud_id"):
        CarteraEnArchivos(tmp_path).registros()


def test_rechaza_datos_incoherentes(tmp_path):
    escribir_en_archivos(REGISTROS, tmp_path)
    conversiones = tmp_path / "conversiones.csv"
    conversiones.write_text(conversiones.read_text().replace("moroso,1", "moroso,0"))

    with pytest.raises(CarteraInvalida, match="desembolsos sin conversión"):
        CarteraEnArchivos(tmp_path).registros()


def test_falta_un_archivo(tmp_path):
    escribir_en_archivos(REGISTROS, tmp_path)
    (tmp_path / "estados.csv").unlink()

    with pytest.raises(CarteraInvalida, match="falta"):
        CarteraEnArchivos(tmp_path).registros()
