"""App web: Bandeja, Caso y Reglas, con fuentes y agente falsos."""

import json
import re

import pytest
from adapters_falsos import ClaudeFalso, FabricaFalsa
from fastapi.testclient import TestClient

from conftest import BASE_PYME, CUIL_MONOTRIBUTISTA, CUIT_PYME
from relevo.app import crear_app, pesos, porcentaje
from relevo.consultor import SinAntecedentes
from relevo.consultor_real import cargar_config_fuentes
from relevo.repositorio import Repositorio
from relevo.servicio import Servicio

FUENTES_PYME = {k: v for k, v in BASE_PYME.items() if k != "documentos"}


@pytest.fixture
def cliente(tmp_path):
    def armar(agente=None, **cambios):
        servicio = Servicio(
            Repositorio(),
            adapters=FabricaFalsa(FUENTES_PYME, **cambios),
            config_fuentes=cargar_config_fuentes(),
            dormir=lambda s: None,
            cliente_claude=agente,
            directorio_documentos=tmp_path,
        )
        return TestClient(crear_app(servicio)), servicio

    return armar


def alta(cliente, files=None, **campos):
    datos = {
        "cuit": CUIT_PYME,
        "segmento": "pyme",
        "monto": "10000000",
        "plazo_meses": "12",
        "ventas_anuales": "240000000",
    } | campos
    return cliente.post("/solicitudes", data=datos, files=files, follow_redirects=False)


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
    assert "pyme.v3" in caso.text
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

    assert f"{segmento}.v3" in pagina
    assert "situacion_bcra_3_o_peor" in pagina
    assert "AUC" in pagina


def test_caso_inexistente(cliente):
    assert cliente()[0].get("/solicitudes/nada").status_code == 404


def test_formato_argentino():
    assert pesos(1234567.891) == "$ 1.234.568"
    assert pesos(204438.35, 2) == "$ 204.438,35"
    assert porcentaje(0.0439) == "4,39 %"
    assert re.fullmatch(r"—", pesos(None))


# --- agentes ---------------------------------------------------------------

BALANCE = json.dumps(
    {
        "tipo": "balance",
        "campos": {
            "cuit": {"valor": CUIT_PYME, "confianza": 0.99},
            "ventas_anuales": {"valor": "240.000.000", "confianza": 0.93},
        },
        "observaciones": "Expresado en pesos.",
    }
)


def test_alta_con_documento_muestra_lo_extraido(cliente):
    http, servicio = cliente(agente=ClaudeFalso(BALANCE))

    respuesta = alta(http, ventas_anuales="", files={"archivos": ("balance.pdf", b"%PDF-1.4", "application/pdf")})
    caso = http.get(respuesta.headers["location"])

    assert respuesta.status_code == 303
    assert "balance.pdf" in caso.text and "Expresado en pesos." in caso.text
    assert "$ 240.000.000" in caso.text and "93 %" in caso.text
    assert "APROBADO" in caso.text


def test_alta_sin_archivos_sigue_funcionando_con_carga_manual(cliente):
    http, servicio = cliente()

    caso = http.get(alta(http).headers["location"])

    assert "carga manual" in caso.text and "APROBADO" in caso.text


def test_informe_y_chat_desde_el_caso(cliente):
    http, servicio = cliente(agente=ClaudeFalso("RESUMEN\n- Se aprueba con rating A.", "Por el score de bureau."))
    caso_url = alta(http).headers["location"]
    id = caso_url.rsplit("/", 1)[1]
    evaluacion_id = servicio.repositorio.ultima_evaluacion(id).id

    assert "Generar informe" in http.get(caso_url).text
    respuesta = http.post(f"{caso_url}/informe", data={"evaluacion_id": evaluacion_id}, follow_redirects=False)
    assert respuesta.status_code == 303
    http.post(f"{caso_url}/chat", data={"evaluacion_id": evaluacion_id, "pregunta": "¿Qué pesó más?"})

    caso = http.get(caso_url).text
    assert "Se aprueba con rating A." in caso and "Regenerar informe" in caso
    assert "¿Qué pesó más?" in caso and "Por el score de bureau." in caso


def test_agente_no_disponible_muestra_aviso_y_el_veredicto_sigue(cliente):
    http, _ = cliente()  # sin agente configurado
    caso_url = alta(http).headers["location"]
    id = caso_url.rsplit("/", 1)[1]

    caso = http.post(f"{caso_url}/informe", data={"evaluacion_id": 1})

    assert "No se pudo generar el informe" in caso.text and "no hay agente configurado" in caso.text
    assert "APROBADO" in caso.text
    assert id in caso.url.path


def test_informe_de_evaluacion_ajena_es_404(cliente):
    http, servicio = cliente(agente=ClaudeFalso("x"))
    primero = alta(http).headers["location"]
    alta(http, cuit="30500010912")

    otra = servicio.repositorio.bandeja()[0].evaluacion_id
    respuesta = http.post(f"{primero}/informe", data={"evaluacion_id": otra})

    assert respuesta.status_code == 404


def test_reextraer_documentos(cliente):
    from relevo.clientes_claude import AgenteNoDisponible

    http, servicio = cliente(agente=ClaudeFalso(AgenteNoDisponible("sin sesión de Claude Code"), BALANCE))
    caso_url = alta(http, ventas_anuales="", files={"archivos": ("b.pdf", b"%PDF", "application/pdf")}).headers[
        "location"
    ]
    assert "sin sesión de Claude Code" in http.get(caso_url).text

    caso = http.post(f"{caso_url}/documentos/extraer")

    assert "Reevaluá" in caso.text and "$ 240.000.000" in caso.text


def test_el_formulario_pide_la_fecha_de_nacimiento_y_la_guarda(cliente):
    from datetime import date

    http, servicio = cliente()

    assert 'name="fecha_nacimiento"' in http.get("/").text
    respuesta = alta(
        http, cuit=CUIL_MONOTRIBUTISTA, segmento="monotributista", consentimiento="true", fecha_nacimiento="1986-11-11"
    )

    assert respuesta.status_code == 303
    id = respuesta.headers["location"].rsplit("/", 1)[1]
    assert servicio.repositorio.solicitud(id).fecha_nacimiento == date(1986, 11, 11)


def test_fecha_de_nacimiento_invalida_muestra_el_error(cliente):
    respuesta = alta(
        cliente()[0],
        cuit=CUIL_MONOTRIBUTISTA,
        segmento="monotributista",
        consentimiento="true",
        fecha_nacimiento="1850-01-01",
    )

    assert respuesta.status_code == 400
    assert "fecha de nacimiento" in respuesta.text
