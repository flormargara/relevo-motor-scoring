"""De punta a punta: evaluar + ConsultorReal + adapters reales, con el HTTP del BCRA
servido desde las respuestas reales guardadas en fixtures."""

import json
import os
import zipfile
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from relevo.consultor_real import AlmacenEnMemoria, ConsultorReal, adapters_por_defecto, cargar_config_fuentes
from relevo.dominio import Solicitud, Veredicto
from relevo.fuentes.igj import armar_snapshot
from relevo.fuentes.juguete import cargar_juguete
from relevo.motor import evaluar
from relevo.reglas import cargar_reglas

FIXTURES = Path(__file__).parent / "fixtures" / "bcra"
HOY = date(2026, 11, 10)
YPF, VICENTIN = "30546689979", "30500959629"


def bcra_desde_fixtures(pedidos):
    def responder(request: httpx.Request) -> httpx.Response:
        pedidos.append(request.url.path)
        partes = request.url.path.rstrip("/").split("/")
        if "estadisticas" in partes:
            nombre = "badlar_7"
        else:
            cuit = partes[-1]
            endpoint = partes[-2] if partes[-2] != "Deudas" else "actual"
            nombre = f"{cuit}_{endpoint.lower()}"
        archivo = FIXTURES / f"{nombre}.json"
        if not archivo.exists():
            return httpx.Response(404, json={"status": 404, "errorMessages": ["No se encontró datos"]})
        cuerpo = json.loads(archivo.read_text())
        return httpx.Response(cuerpo["status"], json=cuerpo)

    return httpx.Client(transport=httpx.MockTransport(responder))


@pytest.fixture(scope="module")
def snapshot_igj(tmp_path_factory):
    carpeta = tmp_path_factory.mktemp("igj")
    with zipfile.ZipFile(carpeta / "igj.zip", "w") as archivo:
        archivo.writestr(
            "igj-entidades-202609.csv",
            "numero_correlativo,tipo_societario,descripcion_tipo_societario,razon_social,dada_de_baja,"
            "codigo_baja,detalle_baja,cuit\n1532404,50,SOC. ANONIMA,YPF,,,,30546689979\n",
        )
        archivo.writestr(
            "igj-balances-202609.csv",
            "numero_correlativo,tipo_societario,descripcion_tipo_societario,razon_social,fecha_balance,"
            "fecha_presentacion,capital_informado\n1532404,50,SOC. ANONIMA,YPF,19911231,19930526,0\n",
        )
        archivo.writestr("igj-asambleas-202609.csv", "numero_correlativo,fecha_realizacion\n")
    armar_snapshot(carpeta / "igj.zip", carpeta / "igj.sqlite", hoy=HOY)
    return carpeta / "igj.sqlite"


def evaluar_en_vivo_simulado(cuit, snapshot_igj, documentos, cliente, almacen=None):
    reglas = cargar_reglas("pyme")
    consultor = ConsultorReal(
        identificacion=cuit,
        adapters=adapters_por_defecto(cliente, documentos=documentos, igj=snapshot_igj, hoy=lambda: HOY),
        config=cargar_config_fuentes(),
        almacen=almacen or AlmacenEnMemoria(),
        clave_solicitud=f"sol-{cuit}",
        precargar=reglas.fuentes,
        ahora=lambda: datetime(2026, 11, 10, 9, 0),
        dormir=lambda s: None,
    )
    return evaluar(Solicitud(cuit=cuit, segmento="pyme", monto=50_000_000, plazo_meses=24), reglas, consultor)


def test_ypf_pasa_los_killers_aunque_tenga_situacion_5_inmaterial(snapshot_igj):
    pedidos = []
    evaluacion = evaluar_en_vivo_simulado(
        YPF, snapshot_igj, {"ventas_anuales": 25e12, "confianza_minima": 0.95}, bcra_desde_fixtures(pedidos)
    )

    assert evaluacion.veredicto is Veredicto.APROBADO
    assert evaluacion.rating == "C"  # pierde puntos por la situación 5 y las 33 entidades
    assert evaluacion.oferta.tasa_nominal_anual == 21.5 + 15  # BADLAR real del fixture + spread C
    assert {s.fuente for s in evaluacion.snapshots} == {
        "cendeu",
        "cheques",
        "badlar",
        "igj",
        "propios",
        "documentos",
        "bureau",
    }
    igj = next(s for s in evaluacion.snapshots if s.fuente == "igj")
    assert igj.resultado.variables["antiguedad_anios"] > 30


def test_vicentin_se_rechaza_sin_gastar_en_el_bureau(snapshot_igj):
    evaluacion = evaluar_en_vivo_simulado(
        VICENTIN, snapshot_igj, {"ventas_anuales": 1e12, "confianza_minima": 0.95}, bcra_desde_fixtures([])
    )

    assert evaluacion.veredicto is Veredicto.RECHAZADO
    assert evaluacion.motivos[0].startswith("situacion_bcra_3_o_peor")
    assert "bureau" in evaluacion.fuentes_no_consultadas
    assert "bureau" not in {s.fuente for s in evaluacion.snapshots}


def test_bcra_caido_deriva_y_no_rompe(snapshot_igj):
    cliente = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})))

    evaluacion = evaluar_en_vivo_simulado(YPF, snapshot_igj, {"ventas_anuales": 25e12, "confianza_minima": 1}, cliente)

    assert evaluacion.veredicto is Veredicto.DERIVAR
    cendeu = next(s for s in evaluacion.snapshots if s.fuente == "cendeu")
    assert cendeu.intentos == 2
    assert "HTTP 503" in cendeu.resultado.motivo


# --- contra la API real (opcional) -------------------------------------------

en_vivo = pytest.mark.skipif(
    os.environ.get("RELEVO_EN_VIVO") != "1", reason="consulta la API real del BCRA; correr con RELEVO_EN_VIVO=1"
)


@en_vivo
def test_en_vivo_la_api_del_bcra_mantiene_el_formato(snapshot_igj):
    with httpx.Client() as cliente:
        adapters = adapters_por_defecto(cliente, juguete=cargar_juguete(), igj=snapshot_igj)
        cendeu = adapters["cendeu"].traducir(adapters["cendeu"].consultar(YPF, timeout=20))
        badlar = adapters["badlar"].traducir(adapters["badlar"].consultar("", timeout=20))
        sin_datos = adapters["cendeu"].traducir(adapters["cendeu"].consultar("20301234563", timeout=20))

    assert cendeu.variables["denominacion"].startswith("YPF")
    assert cendeu.variables["entidades_con_deuda"] > 0
    assert 0 < badlar.variables["tasa"] < 200
    assert type(sin_datos).__name__ == "SinAntecedentes"
