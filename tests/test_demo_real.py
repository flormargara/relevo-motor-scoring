"""Base de demo con datos reales de la central: sin S3, con una extracción armada a mano."""

import sqlite3
from datetime import date

import pytest

from relevo.cartera import CarteraEnArchivos
from relevo.consultor import Disponible, SinAntecedentes
from relevo.demo_real import (
    PORCENTAJE_RESERVADO,
    Deuda,
    Extraccion,
    construir,
    crudo_cendeu,
    es_reservado,
    mora_observada,
    sql_reservado,
)
from relevo.fuentes.cendeu import Cendeu
from relevo.repositorio import Repositorio

PYME = "30712345671"
MONO = "20123456786"
SIN_DEUDA = "27111111117"


def test_la_reserva_es_un_porcentaje_fijo_y_estable():
    cuits = [20_000_000_000 + i * 7919 for i in range(20_000)]
    tasa = sum(map(es_reservado, cuits)) / len(cuits)

    assert tasa == pytest.approx(PORCENTAJE_RESERVADO / 100, abs=0.006)
    assert es_reservado("20223326189") == es_reservado(20223326189)


def test_la_reserva_en_sql_coincide_con_python():
    duckdb = pytest.importorskip("duckdb", reason="instalar el extra `demo`")
    cuits = [20_000_000_000 + i * 104_729 for i in range(3000)]

    en_sql = {c for (c,) in duckdb.sql(f"SELECT cuit FROM unnest({cuits}) t(cuit) WHERE {sql_reservado()}").fetchall()}

    assert en_sql == {c for c in cuits if es_reservado(c)}
    assert en_sql


DEUDAS = [
    Deuda(202508, "BANCO A", 1, 1000.0),
    Deuda(202509, "BANCO A", 2, 1100.0),
    Deuda(202509, "BANCO B", 1, 400.0, proceso_judicial=False),
    Deuda(202511, "BANCO A", 3, 1200.0),  # posterior a la observación
    Deuda(202512, "BANCO A", 5, 1300.0, proceso_judicial=True),
]


def test_el_crudo_de_cendeu_no_mira_el_futuro_y_lo_traduce_el_adapter_real():
    resultado = Cendeu(None).traducir(crudo_cendeu(PYME, DEUDAS, 202509))

    assert isinstance(resultado, Disponible)
    v = resultado.variables
    assert v["periodo"] == "202509"
    assert v["peor_situacion_12m"] == 2  # la situación 3 y la 5 son posteriores
    assert v["peor_situacion_actual"] == 2
    assert v["deuda_total"] == 1_500_000  # miles → pesos
    assert v["entidades_con_deuda"] == 2
    assert v["meses_informados"] == 2
    assert not v["en_proceso_judicial"]


def test_sin_deuda_hasta_la_observacion_es_sin_antecedentes():
    assert isinstance(Cendeu(None).traducir(crudo_cendeu(PYME, DEUDAS, 202507)), SinAntecedentes)


def test_sin_deuda_en_el_periodo_pero_con_historia():
    resultado = Cendeu(None).traducir(crudo_cendeu(PYME, DEUDAS, 202510))

    assert resultado.variables["peor_situacion_12m"] == 2
    assert resultado.variables["deuda_total"] == 0  # /Deudas respondió 404


def test_mora_observada_traduce_la_peor_situacion_de_cada_mes():
    assert mora_observada(DEUDAS, despues_de=202509) == {date(2025, 11, 1): 120, date(2025, 12, 1): 365}
    assert mora_observada(DEUDAS, despues_de=202512) == {}


def test_la_extraccion_se_guarda_y_se_carga_igual(tmp_path):
    extraccion = Extraccion({202509: [PYME]}, {PYME: DEUDAS}, {MONO: date(1985, 3, 2)})
    extraccion.guardar(tmp_path / "x.json")

    assert Extraccion.cargar(tmp_path / "x.json") == extraccion


@pytest.fixture
def arca(tmp_path):
    ruta = tmp_path / "arca.sqlite"
    with sqlite3.connect(ruta) as con:
        con.execute(
            "CREATE TABLE contribuyentes (cuit INTEGER PRIMARY KEY, denominacion TEXT, ganancias TEXT, iva TEXT, "
            "monotributo TEXT, integrante_sociedad TEXT, empleador TEXT, actividad_monotributo TEXT)"
        )
        con.execute("CREATE TABLE meta (clave TEXT PRIMARY KEY, valor TEXT)")
        con.executemany(
            "INSERT INTO contribuyentes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (int(PYME), "PYME SA", "AC", "AC", "NI", "N", "S", None),
                (int(MONO), "PERSONA", "NI", "NI", "D", "N", "N", "01"),
                (int(SIN_DEUDA), "OTRA", "NI", "NI", "NI", "N", "N", None),  # no es monotributista
            ],
        )
        con.execute("INSERT INTO meta VALUES ('fecha_padron', '2026-10-30')")
    return ruta


@pytest.fixture
def igj(tmp_path):
    """IGJ sin ninguna sociedad: todas quedan sin antecedentes, como fuera de CABA."""
    ruta = tmp_path / "igj.sqlite"
    with sqlite3.connect(ruta) as con:
        con.execute(
            "CREATE TABLE entidades (cuit TEXT PRIMARY KEY, razon_social TEXT, tipo_societario TEXT, "
            "dada_de_baja INTEGER, detalle_baja TEXT, primer_registro TEXT)"
        )
        con.execute("CREATE TABLE meta (clave TEXT PRIMARY KEY, valor TEXT)")
    return ruta


BADLAR = {
    p: {"status": 200, "body": {"results": [{"detalle": [{"fecha": "2025-10-10", "valor": 40.0}]}]}}
    for p in (202509, 202510)
}


def test_construye_evaluaciones_fechadas_y_cartera_con_mora_real(tmp_path, arca, igj):
    # al día cuando pide, y después se atrasa
    se_atrasa = [Deuda(p, "BANCO A", 1, 300.0) for p in (202509, 202510, 202511)] + [
        Deuda(202512, "BANCO A", 2, 300.0),
        Deuda(202601, "BANCO A", 3, 300.0),
    ]
    extraccion = Extraccion(
        candidatos={202509: [PYME, SIN_DEUDA, MONO], 202510: [PYME]},
        deudas={PYME: DEUDAS, MONO: se_atrasa},
        nacimientos={MONO: date(1985, 3, 2)},
    )
    repo = Repositorio(":memory:")

    conteo = construir(
        repo, extraccion, BADLAR, pyme_por_periodo=1, mono_por_periodo=1, hasta=date(2026, 1, 31),
        destino_cartera=tmp_path / "cartera", arca=arca, igj=igj,
    )  # fmt: skip

    assert conteo["pyme"] == 2 and conteo["monotributista"] == 1  # SIN_DEUDA no es monotributista
    filas = {f.solicitud.id: f for f in repo.bandeja()}
    assert set(filas) == {f"demo-202509-{PYME}", f"demo-202509-{MONO}", f"demo-202510-{PYME}"}
    guardada = repo.evaluacion(filas[f"demo-202509-{PYME}"].evaluacion_id)
    assert guardada.creada_en.date().replace(day=1) == date(2025, 10, 1)  # el mes siguiente a la observación
    cendeu = next(s for s in guardada.evaluacion.snapshots if s.fuente == "cendeu")
    assert cendeu.resultado.variables["peor_situacion_12m"] == 2
    assert cendeu.consultado_en == guardada.creada_en
    cheques = next(s for s in guardada.evaluacion.snapshots if s.fuente == "cheques")
    assert isinstance(cheques.resultado, SinAntecedentes)  # supuesto de la demo: el bucket no tiene cheques

    registros = {r.solicitud_id: r for r in CarteraEnArchivos(tmp_path / "cartera").registros()}
    assert len(registros) == conteo["otorgadas"]
    for id, registro in registros.items():
        cuit = id.rsplit("-", 1)[1]
        for estado in registro.estados:
            esperado = mora_observada(extraccion.deudas.get(cuit, []), int(id.split("-")[1])).get(estado.periodo, 0)
            assert estado.dias_mora == esperado
    assert any(e.dias_mora > 0 for r in registros.values() for e in r.estados)  # el chequeo no es vacío
