"""API del motor (``/api/v1``), con fuentes falsas."""

import pytest
from adapters_falsos import FabricaFalsa
from fastapi.testclient import TestClient

from conftest import BASE_PYME, CUIL_HUMANA, CUIT_PYME
from relevo.app import crear_app
from relevo.consultor import NoDisponible, SinAntecedentes
from relevo.consultor_real import cargar_config_fuentes
from relevo.repositorio import Repositorio
from relevo.servicio import Servicio

FUENTES_PYME = {k: v for k, v in BASE_PYME.items() if k != "documentos"}
PYME = {
    "cuit": "30-71234567-1",
    "segmento": "pyme",
    "monto": 10_000_000,
    "plazo_meses": 12,
    "documentos": {"ventas_anuales": 240_000_000},
}


@pytest.fixture
def api(tmp_path):
    def armar(**cambios):
        servicio = Servicio(
            Repositorio(),
            adapters=FabricaFalsa(FUENTES_PYME, **cambios),
            config_fuentes=cargar_config_fuentes(),
            dormir=lambda s: None,
            directorio_documentos=tmp_path,
        )
        return TestClient(crear_app(servicio)), servicio

    return armar


def test_evalua_una_solicitud_y_devuelve_el_contrato_completo(api):
    http, _ = api()

    respuesta = http.post("/api/v1/evaluaciones", json=PYME)

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["veredicto"] == "APROBADO"
    assert cuerpo["version_reglas"] == "pyme.v3"
    assert cuerpo["solicitud"] == {"cuit": CUIT_PYME, "segmento": "pyme", "monto": 10_000_000, "plazo_meses": 12}
    assert cuerpo["rating"] and 0 < cuerpo["pd"] < 1
    assert cuerpo["oferta"]["monto"] == 10_000_000
    assert any(p["regla"] == "identificacion_invalida" for p in cuerpo["traza"])
    fuentes = {f["fuente"]: f for f in cuerpo["fuentes"]}
    assert fuentes["cendeu"]["resultado"] == "disponible"
    assert fuentes["documentos"]["variables"]["ventas_anuales"] == 240_000_000
    assert fuentes["documentos"]["variables"]["confianza_minima"] == 1.0  # declarado por API


def test_la_evaluacion_queda_guardada_y_se_ve_en_la_pantalla(api):
    http, _ = api()
    creada = http.post("/api/v1/evaluaciones", json=PYME).json()

    assert http.get(f"/api/v1/evaluaciones/{creada['id']}").json() == creada
    assert http.get(f"/solicitudes/{creada['solicitud_id']}").status_code == 200
    assert CUIT_PYME in http.get("/").text


def test_fuente_sin_antecedentes_o_no_disponible_se_distingue(api):
    http, _ = api(bureau=SinAntecedentes(), cheques=NoDisponible("timeout"))

    fuentes = {f["fuente"]: f for f in http.post("/api/v1/evaluaciones", json=PYME).json()["fuentes"]}

    assert fuentes["bureau"]["resultado"] == "sin_antecedentes"
    assert fuentes["cheques"]["resultado"] == "no_disponible"
    assert fuentes["cheques"]["motivo"].startswith("timeout")  # el consultor agrega los reintentos
    assert fuentes["cheques"]["variables"] is None


def test_reevaluar_suma_una_evaluacion_al_historial(api):
    http, _ = api()
    primera = http.post("/api/v1/evaluaciones", json=PYME).json()

    segunda = http.post(f"/api/v1/solicitudes/{primera['solicitud_id']}/evaluaciones")
    historial = http.get(f"/api/v1/solicitudes/{primera['solicitud_id']}/evaluaciones").json()

    assert segunda.status_code == 201
    assert [e["id"] for e in historial] == [segunda.json()["id"], primera["id"]]


@pytest.mark.parametrize(
    "cambios, mensaje",
    [
        ({"segmento": "consumo"}, None),  # lo rechaza el esquema
        ({"monto": 0}, None),
        ({"cuit": ""}, "falta el CUIT"),
        ({"cuit": CUIL_HUMANA, "segmento": "monotributista"}, "consentimiento"),
    ],
)
def test_solicitud_invalida_es_422_y_no_se_guarda(api, cambios, mensaje):
    http, servicio = api()

    respuesta = http.post("/api/v1/evaluaciones", json=PYME | cambios)

    assert respuesta.status_code == 422
    if mensaje:
        assert mensaje in respuesta.json()["detail"]
    assert servicio.repositorio.bandeja() == []


def test_lo_que_no_existe_es_404(api):
    http, _ = api()

    assert http.get("/api/v1/evaluaciones/999").status_code == 404
    assert http.get("/api/v1/solicitudes/nada/evaluaciones").status_code == 404
    assert http.post("/api/v1/solicitudes/nada/evaluaciones").status_code == 404
    assert http.get("/api/v1/reglas/consumo").status_code == 404


def test_reglas_vigentes_con_ratings_y_catalogo(api):
    http, _ = api()

    reglas = http.get("/api/v1/reglas/pyme").json()

    assert reglas["id"] == f"pyme.v{reglas['version']}"
    assert reglas["contenido"]["killers"]
    assert "ratings" not in reglas["contenido"]  # son del modelo
    assert [r["rating"] for r in reglas["ratings"]][0] == "A"
    assert {"nombre": "documentos.ventas_anuales", "fuente": "documentos"}.items() <= next(
        v for v in reglas["catalogo"] if v["nombre"] == "documentos.ventas_anuales"
    ).items()


def test_el_esquema_openapi_tiene_solo_la_api(api):
    http, _ = api()

    rutas = set(http.get("/openapi.json").json()["paths"])

    assert rutas == {
        "/api/v1/evaluaciones",
        "/api/v1/evaluaciones/{evaluacion_id}",
        "/api/v1/solicitudes/{solicitud_id}/evaluaciones",
        "/api/v1/reglas/{flujo}",
    }
    assert http.get("/docs").status_code == 200
