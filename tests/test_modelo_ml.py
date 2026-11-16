"""Modelo de ML externo (ADR 0010): fuente, descriptor, motor, servicio y control de metadata."""

from datetime import date, datetime

import httpx
import pytest
from adapters_falsos import FabricaFalsa
from fastapi.testclient import TestClient

from conftest import BASE_PYME, armar_consultor, solicitud_pyme
from relevo.catalogo import catalogo
from relevo.consultor import Disponible, NoDisponible, SinAntecedentes
from relevo.consultor_real import cargar_config_fuentes
from relevo.dominio import Veredicto
from relevo.fuentes import FuenteCaida
from relevo.fuentes.modelo_ml import ConexionModelo, ModeloML, metadata, periodo_de, verificar
from relevo.identificacion import es_valido
from relevo.modelo_prueba import _azar, crear_app
from relevo.motor import evaluar
from relevo.reglas import DIRECTORIO_REGLAS, ReglasInvalidas
from relevo.repositorio import Repositorio
from relevo.servicio import Servicio
from relevo.versiones_reglas import VersionesReglas

URL = "http://testserver"
_VALIDOS = [c for c in (f"3071234{i:04d}" for i in range(2000)) if es_valido(c)]
CUIT_CON_DATOS = next(c for c in _VALIDOS if _azar(c, "sin-datos") >= 0.05)
CUIT_SIN_DATOS = next(c for c in _VALIDOS if _azar(c, "sin-datos") < 0.05)
# TestClient avisa que no usa el timeout: el adapter real sí lo pasa a httpx
pytestmark = pytest.mark.filterwarnings("ignore:You should not use the .timeout. argument")


@pytest.fixture
def http():
    return TestClient(crear_app())


def _adapter(http, *versiones, periodo=202605, url=URL):
    return ModeloML(http, ConexionModelo(url), [("pyme-prueba", v) for v in versiones or ("1",)], "pyme", periodo)


def _modelos(entradas):
    """La fuente modelo_ml como la ve el motor: una entrada por modelo consultado (modelo@versión)."""
    return Disponible({"modelos": entradas})


def _ok(pd, version="1", factores=()):
    return {"estado": "disponible", "pd": pd, "version": f"pyme-prueba@{version}", "factores": list(factores)}


def _consultar(adapter, cuit=CUIT_CON_DATOS):
    return adapter.traducir(adapter.consultar(cuit, timeout=5))


# --- fuente --------------------------------------------------------------------------


def test_periodo_es_el_mes_anterior_a_la_evaluacion():
    assert periodo_de(date(2026, 6, 10)) == 202605
    assert periodo_de(date(2026, 1, 31)) == 202512


def test_la_fuente_trae_pd_version_y_factores(http):
    resultado = _consultar(_adapter(http))

    assert isinstance(resultado, Disponible)
    entrada = resultado.variables["modelos"]["pyme-prueba@1"]
    assert entrada["estado"] == "disponible" and 0 < entrada["pd"] < 1
    assert entrada["version"] == "pyme-prueba@1" and entrada["periodo_datos"] == 202605
    factores = entrada["factores"]
    assert factores and [abs(f["aporte"]) for f in factores] == sorted(
        (abs(f["aporte"]) for f in factores), reverse=True
    )


def test_una_consulta_trae_todos_los_modelos(http):
    resultado = _consultar(_adapter(http, "1", "2"))

    v1, v2 = (resultado.variables["modelos"][f"pyme-prueba@{v}"] for v in "12")
    assert v1["estado"] == v2["estado"] == "disponible" and v1["pd"] != v2["pd"]


def test_si_un_modelo_falla_los_otros_siguen(http):
    resultado = _consultar(_adapter(http, "1", "9"))

    assert resultado.variables["modelos"]["pyme-prueba@1"]["estado"] == "disponible"
    falla = resultado.variables["modelos"]["pyme-prueba@9"]
    assert falla["estado"] == "no_disponible" and "HTTP 400" in falla["motivo"]


def test_el_crudo_guarda_el_pedido(http):
    (respuesta,) = _adapter(http).consultar(CUIT_CON_DATOS, timeout=5)["respuestas"]

    assert respuesta["pedido"] == {
        "cuit": CUIT_CON_DATOS,
        "segmento": "pyme",
        "periodo": 202605,
        "modelo": "pyme-prueba",
        "version": "1",
    }


def test_404_es_sin_antecedentes(http):
    assert _consultar(_adapter(http), CUIT_SIN_DATOS) == SinAntecedentes()


def test_una_version_que_no_sirve_es_no_disponible_sin_reintentar(http):
    resultado = _consultar(_adapter(http, "9"))

    assert isinstance(resultado, NoDisponible) and "HTTP 400" in resultado.motivo


def test_sin_url_la_fuente_esta_caida(http):
    with pytest.raises(FuenteCaida, match="RELEVO_MODELO_URL"):
        _adapter(http, url=None).consultar(CUIT_CON_DATOS, timeout=5)


def test_5xx_y_timeout_se_reintentan():
    for transporte in (
        httpx.MockTransport(lambda r: httpx.Response(503)),
        httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("lento"))),
    ):
        with pytest.raises(FuenteCaida):
            _adapter(httpx.Client(transport=transporte)).consultar(CUIT_CON_DATOS, timeout=5)


@pytest.mark.parametrize(
    "cuerpo, motivo",
    [
        ({"modelo": "pyme-prueba", "version": "1", "periodo_datos": 202605}, "falta la pd"),
        ({"pd": 1.4, "modelo": "pyme-prueba", "version": "1", "periodo_datos": 202605}, "fuera de [0, 1]"),
        ({"pd": 0.1, "modelo": "otro", "version": "1", "periodo_datos": 202605}, "respondió otro@1"),
        ({"pd": 0.1, "modelo": "pyme-prueba", "version": "1", "periodo_datos": 202606}, "posteriores al período"),
    ],
)
def test_respuestas_fuera_de_contrato(cuerpo, motivo):
    adapter = _adapter(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=cuerpo))))

    resultado = _consultar(adapter)

    assert isinstance(resultado, NoDisponible) and motivo in resultado.motivo


def test_el_catalogo_tiene_la_pd_del_modelo():
    assert "modelo_ml.pd" in {v.nombre for v in catalogo("pyme")}
    assert "modelo_ml.pd" in {v.nombre for v in catalogo("monotributista")}


# --- metadata y CUIT reservados --------------------------------------------------------


def test_la_metadata_del_servidor_de_prueba_pasa_el_control(http):
    modelos = metadata(http, ConexionModelo(URL))

    assert verificar(modelos, "pyme-prueba", "1") == []
    assert verificar(modelos, "pyme-prueba", "9") == ["el servicio no publica pyme-prueba@9"]


@pytest.mark.parametrize(
    "exclusion, problema",
    [
        ({}, "no declara que excluyó"),
        ({"regla": "md5-8hex-mod100", "porcentaje": 10}, "al 10 %"),
        ({"regla": "por fecha", "porcentaje": 5}, "con por fecha"),
    ],
)
def test_la_exclusion_tiene_que_coincidir_con_la_de_la_demo(exclusion, problema):
    modelos = metadata(TestClient(crear_app(exclusion=exclusion)), ConexionModelo(URL))

    (aviso,) = verificar(modelos, "pyme-prueba", "1")
    assert problema in aviso


# --- reglas y motor ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def versiones():
    versiones = VersionesReglas(Repositorio())
    versiones.importar(DIRECTORIO_REGLAS)
    return versiones


def _con_modelo(versiones, archivo="pyme-prueba.v1.json", si_no_disponible="derivar"):
    contenido = versiones.contenido("pyme")
    contenido["scorecard"] = {"modelo": archivo, "si_no_disponible": si_no_disponible}
    return versiones.candidata("pyme", contenido)[0]


def test_el_descriptor_externo_reemplaza_al_scorecard(versiones):
    reglas = _con_modelo(versiones)

    assert reglas.scorecard.externo.id == "pyme-prueba@1"
    assert "modelo_ml" in reglas.fuentes
    assert [r.nombre for r in reglas.ratings] == ["A", "B", "C", "D", "E"]


def test_con_modelo_externo_no_se_puede_seguir_sin_pd(versiones):
    with pytest.raises(ReglasInvalidas, match="tiene que ser derivar o rechazar"):
        _con_modelo(versiones, si_no_disponible="continuar")


def test_el_motor_usa_la_pd_del_modelo(versiones):
    factores = [{"variable": "peor_situacion_12m", "valor": 1, "aporte": -0.4}]
    consultor = armar_consultor(BASE_PYME, modelo_ml=_modelos({"pyme-prueba@1": _ok(0.07, factores=factores)}))

    evaluacion = evaluar(solicitud_pyme(), _con_modelo(versiones), consultor)

    assert (evaluacion.pd, evaluacion.rating, evaluacion.puntos) == (0.07, "C", None)
    paso = next(p for p in evaluacion.traza if p.etapa == "scorecard")
    assert (paso.regla, paso.resultado, paso.detalle["factores"]) == ("pyme-prueba@1", "ok", factores)
    rating = next(p for p in evaluacion.traza if p.etapa == "rating")
    assert rating.detalle["externo"] == "pyme-prueba@1"


@pytest.mark.parametrize(
    "resultado, accion, veredicto",
    [
        (NoDisponible("caído"), "derivar", Veredicto.DERIVAR),
        (SinAntecedentes(), "derivar", Veredicto.DERIVAR),
        (NoDisponible("caído"), "rechazar", Veredicto.RECHAZADO),
    ],
)
def test_sin_pd_se_deriva_o_se_rechaza(versiones, resultado, accion, veredicto):
    consultor = armar_consultor(BASE_PYME, modelo_ml=resultado)

    evaluacion = evaluar(solicitud_pyme(), _con_modelo(versiones, si_no_disponible=accion), consultor)

    assert evaluacion.veredicto is veredicto and evaluacion.rating is None and evaluacion.oferta is None
    assert any("sin PD del modelo" in m for m in evaluacion.motivos)


# --- servicio --------------------------------------------------------------------------


class Reloj:
    def __call__(self):
        return datetime(2026, 6, 10, 9, 0)


def _servicio(http, url=URL):
    servicio = Servicio(
        Repositorio(),
        adapters=FabricaFalsa({k: v for k, v in BASE_PYME.items() if k != "documentos"}),
        config_fuentes=cargar_config_fuentes(),
        ahora=Reloj(),
        dormir=lambda s: None,
        conexion_modelo=ConexionModelo(url),
    )
    servicio._cliente = http
    contenido = servicio.versiones.contenido("pyme")
    contenido["scorecard"] = {"modelo": "pyme-prueba.v1.json", "si_no_disponible": "derivar"}
    servicio.versiones.publicar("pyme", contenido, "test", "modelo de ML", base=3)
    return servicio


def test_el_servicio_consulta_el_modelo_con_el_periodo_anterior(http):
    servicio = _servicio(http)
    id = servicio.alta(CUIT_CON_DATOS, "pyme", 10_000_000, 12, {"ventas_anuales": 240_000_000})

    evaluacion = servicio.repositorio.evaluacion(servicio.evaluar(id)).evaluacion

    assert evaluacion.version_reglas == "pyme.v4" and evaluacion.pd is not None
    snapshot = next(s for s in evaluacion.snapshots if s.fuente == "modelo_ml")
    assert snapshot.crudo["respuestas"][0]["pedido"]["periodo"] == 202605


def test_avisos_del_modelo_en_backoffice(http):
    assert _servicio(http).avisos_modelo("pyme") == []
    assert _servicio(TestClient(crear_app(exclusion={})), URL).avisos_modelo("pyme") == [
        "pyme-prueba@1 no declara que excluyó los CUIT reservados para la demo (ADR 0009)"
    ]
    assert "RELEVO_MODELO_URL" in _servicio(http, url=None).avisos_modelo("pyme")[0]


def test_sin_modelo_externo_no_hay_avisos_ni_consulta(http):
    servicio = Servicio(Repositorio(), conexion_modelo=ConexionModelo(None))

    assert servicio.avisos_modelo("pyme") == []
    assert {m["archivo"] for m in servicio.modelos_disponibles("pyme")} >= {
        "pyme.v2.json",
        "pyme-prueba.v1.json",
        "pyme-prueba.v2.json",
    }


def test_la_traza_muestra_la_pd_del_modelo(versiones):
    from relevo.presentacion import flujo

    reglas = _con_modelo(versiones)
    con_pd = evaluar(
        solicitud_pyme(), reglas, armar_consultor(BASE_PYME, modelo_ml=_modelos({"pyme-prueba@1": _ok(0.07)}))
    )
    sin_pd = evaluar(solicitud_pyme(), reglas, armar_consultor(BASE_PYME, modelo_ml=NoDisponible("caído")))

    scorecard = {e.clave: (e.estado, e.resumen) for e in flujo(con_pd, reglas)}["scorecard"]
    assert scorecard == ("ok", "modelo de ML pyme-prueba@1 · PD 7,00 %")
    assert {e.clave: e.estado for e in flujo(sin_pd, reglas)}["scorecard"] == "corta"
