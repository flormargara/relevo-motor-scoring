"""App web: Bandeja, Caso y Reglas, con fuentes falsas."""

import re

import pytest
from adapters_falsos import FabricaFalsa
from fastapi.testclient import TestClient

from conftest import BASE_PYME, CUIL_MONOTRIBUTISTA, CUIT_PYME
from relevo.app import crear_app, pesos, porcentaje
from relevo.consultor import SinAntecedentes
from relevo.consultor_real import cargar_config_fuentes
from relevo.repositorio import Repositorio
from relevo.servicio import Servicio

FUENTES_PYME = {k: v for k, v in BASE_PYME.items() if k != "documentos"}


@pytest.fixture
def cliente():
    def armar(**cambios):
        servicio = Servicio(
            Repositorio(),
            adapters=FabricaFalsa(FUENTES_PYME, **cambios),
            config_fuentes=cargar_config_fuentes(),
            dormir=lambda s: None,
        )
        return TestClient(crear_app(servicio)), servicio

    return armar


def alta(cliente, **campos):
    datos = {
        "cuit": CUIT_PYME,
        "segmento": "pyme",
        "monto": "10000000",
        "plazo_meses": "12",
        "ventas_anuales": "240000000",
    } | campos
    return cliente.post("/solicitudes", data=datos, follow_redirects=False)


def test_bandeja_vacia(cliente):
    respuesta = cliente()[0].get("/")

    assert respuesta.status_code == 200
    assert "No hay solicitudes" in respuesta.text


def test_alta_evalua_y_muestra_el_caso(cliente):
    http, _ = cliente()

    respuesta = alta(http)
    assert respuesta.status_code == 303
    caso = http.get(respuesta.headers["location"])

    assert caso.status_code == 200
    assert "APROBADO" in caso.text
    assert "identificacion_invalida" in caso.text  # la traza
    assert "$ 10.000.000" in caso.text  # la oferta
    assert "pyme.v2" in caso.text
    assert "respuesta cruda" in caso.text

    bandeja = http.get("/")
    assert CUIT_PYME in bandeja.text and "APROBADO" in bandeja.text


def test_alta_de_persona_humana_sin_consentimiento_muestra_el_error(cliente):
    respuesta = alta(cliente()[0], cuit=CUIL_MONOTRIBUTISTA, segmento="monotributista")

    assert respuesta.status_code == 400
    assert "consentimiento" in respuesta.text
    assert CUIL_MONOTRIBUTISTA in respuesta.text  # conserva lo que se cargó


def test_derivado_permite_registrar_la_resolucion(cliente):
    http, servicio = cliente(cendeu=SinAntecedentes())
    caso_url = alta(http).headers["location"]
    id = caso_url.rsplit("/", 1)[1]
    evaluacion_id = servicio.repositorio.ultima_evaluacion(id).id

    assert "DERIVAR" in http.get(caso_url).text
    respuesta = http.post(
        f"/solicitudes/{id}/resolucion",
        follow_redirects=False,
        data={
            "evaluacion_id": evaluacion_id,
            "decision": "aprobar",
            "motivo": "conozco al cliente",
            "analista": "Flor",
        },
    )
    assert respuesta.status_code == 303

    caso = http.get(caso_url).text
    assert "conozco al cliente" in caso and "no reemplaza el veredicto" in caso


def test_no_se_resuelve_un_aprobado(cliente):
    http, servicio = cliente()
    id = alta(http).headers["location"].rsplit("/", 1)[1]
    evaluacion_id = servicio.repositorio.ultima_evaluacion(id).id

    respuesta = http.post(
        f"/solicitudes/{id}/resolucion",
        data={"evaluacion_id": evaluacion_id, "decision": "aprobar", "motivo": "x", "analista": "y"},
    )

    assert respuesta.status_code == 400


def test_reevaluar_y_ver_una_evaluacion_anterior(cliente):
    http, servicio = cliente()
    caso_url = alta(http).headers["location"]
    id = caso_url.rsplit("/", 1)[1]
    primera = servicio.repositorio.ultima_evaluacion(id).id

    http.post(f"/solicitudes/{id}/evaluar")
    caso = http.get(caso_url).text
    anterior = http.get(f"{caso_url}?evaluacion={primera}").text

    assert "Evaluaciones" in caso
    assert "evaluación anterior" in anterior
    assert http.get(f"{caso_url}?evaluacion=9999").status_code == 404


def test_borrar(cliente):
    http, _ = cliente()
    caso_url = alta(http).headers["location"]

    http.post(f"{caso_url}/borrar")

    assert http.get(caso_url).status_code == 404
    assert "No hay solicitudes" in http.get("/").text


def test_evaluar_pendientes(cliente):
    http, servicio = cliente()
    servicio.cargar_precargados()
    assert "Evaluar pendientes" in http.get("/").text

    http.post("/evaluar-pendientes")

    assert all(f.veredicto for f in servicio.repositorio.bandeja())


@pytest.mark.parametrize("segmento", ["pyme", "monotributista"])
def test_pantalla_de_reglas(cliente, segmento):
    pagina = cliente()[0].get(f"/reglas?segmento={segmento}").text

    assert f"{segmento}.v2" in pagina
    assert "situacion_bcra_3_o_peor" in pagina
    assert "AUC" in pagina


def test_caso_inexistente(cliente):
    assert cliente()[0].get("/solicitudes/nada").status_code == 404


def test_formato_argentino():
    assert pesos(1234567.891) == "$ 1.234.568"
    assert pesos(204438.35, 2) == "$ 204.438,35"
    assert porcentaje(0.0439) == "4,39 %"
    assert re.fullmatch(r"—", pesos(None))
