"""Adapters del BCRA: traducción contra respuestas reales y manejo de HTTP."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from relevo.consultor import Disponible, NoDisponible, SinAntecedentes
from relevo.fuentes import FuenteCaida
from relevo.fuentes.badlar import Badlar
from relevo.fuentes.cendeu import Cendeu, Cheques

FIXTURES = Path(__file__).parent / "fixtures" / "bcra"
BNA, YPF, VICENTIN = "30500010912", "30546689979", "30500959629"
CELULOSA = "30501062150"
SIN_DATOS = "No se encontró datos para la identificación ingresada."
NO_404 = {"status": 404, "body": {"status": 404, "errorMessages": [SIN_DATOS]}}


def fixture(nombre):
    cuerpo = json.loads((FIXTURES / f"{nombre}.json").read_text())
    return {"status": cuerpo["status"], "body": cuerpo}


def crudo_cendeu(cuit):
    return {"actual": fixture(f"{cuit}_actual"), "historicas": fixture(f"{cuit}_historicas")}


def sin_cliente():
    return httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("no debería pedir HTTP")))


# --- CENDEU: traducción ---------------------------------------------------


def test_cendeu_deudor_sano():
    variables = Cendeu(sin_cliente()).traducir(crudo_cendeu(BNA)).variables

    assert variables["denominacion"] == "BANCO DE LA NACION ARGENTINA"
    assert variables["periodo"] == "202609"
    assert variables["peor_situacion_12m"] == 1
    assert variables["peor_situacion_actual"] == 1
    assert variables["deuda_irregular_12m"] == 0
    assert variables["entidades_con_deuda"] == 21
    assert variables["deuda_total"] == 329_154_542_000  # los montos llegan en miles
    assert variables["identificacion_rechazada"] is False
    assert variables["meses_informados"] == 12


def test_cendeu_situacion_5_inmaterial_queda_medida():
    """YPF tiene situación 5 por $86 mil con una no bancaria: la política decide si es material."""
    variables = Cendeu(sin_cliente()).traducir(crudo_cendeu(YPF)).variables

    assert variables["peor_situacion_12m"] == 5
    assert variables["deuda_irregular_12m"] == 86_000
    assert variables["proporcion_irregular_12m"] == pytest.approx(7.7357e-08, rel=1e-3)
    assert variables["entidades_con_deuda"] == 33


def test_cendeu_deudor_irregular():
    variables = Cendeu(sin_cliente()).traducir(crudo_cendeu(VICENTIN)).variables

    assert variables["peor_situacion_12m"] == 5
    assert variables["deuda_irregular_12m"] == 11_355_031_000
    assert variables["proporcion_irregular_12m"] == pytest.approx(0.999997, rel=1e-6)


def test_cendeu_404_en_ambos_es_sin_antecedentes():
    assert Cendeu(sin_cliente()).traducir({"actual": NO_404, "historicas": NO_404}) == SinAntecedentes()


def test_cendeu_sin_deuda_actual_pero_con_historia():
    crudo = {"actual": NO_404, "historicas": fixture(f"{VICENTIN}_historicas")}

    variables = Cendeu(sin_cliente()).traducir(crudo).variables

    assert variables["peor_situacion_12m"] == 5
    assert variables["deuda_total"] == 0
    assert variables["entidades_con_deuda"] == 0


def test_cendeu_400_es_identificacion_rechazada():
    resultado = Cendeu(sin_cliente()).traducir({"actual": fixture("123_actual"), "historicas": fixture("123_actual")})

    assert resultado == Disponible({"identificacion_rechazada": True})


# --- Cheques --------------------------------------------------------------


def test_cheques_cuenta_los_impagos():
    """Celulosa Argentina: 1652 cheques rechazados sin fondos en dos entidades; 4 se pagaron después."""
    variables = Cheques(sin_cliente()).traducir(fixture(f"{CELULOSA}_chequesrechazados")).variables

    assert variables["rechazados_total"] == 1652
    assert variables["rechazados_impagos"] == 1648
    assert variables["monto_impago"] == pytest.approx(26_845_062_084.82)


def test_cheques_404_es_sin_antecedentes():
    assert Cheques(sin_cliente()).traducir(fixture(f"{YPF}_chequesrechazados")) == SinAntecedentes()


# --- BADLAR ---------------------------------------------------------------


def test_badlar_toma_el_ultimo_valor_publicado():
    resultado = Badlar(sin_cliente()).traducir(fixture("badlar_7"))

    assert resultado == Disponible({"tasa": 21.5, "fecha": "2026-11-04", "desactualizada": False})


def test_badlar_sin_datos_en_la_ventana():
    crudo = {"status": 200, "body": {"status": 200, "results": [{"idVariable": 7, "detalle": []}]}}

    assert isinstance(Badlar(sin_cliente()).traducir(crudo), NoDisponible)


def test_badlar_pide_una_ventana_de_30_dias():
    pedidos = []

    def responder(request):
        pedidos.append(request.url)
        return httpx.Response(200, json=fixture("badlar_7")["body"])

    cliente = httpx.Client(transport=httpx.MockTransport(responder))
    Badlar(cliente, hoy=lambda: date(2026, 11, 10)).consultar("", timeout=5)

    assert pedidos[0].params["desde"] == "2026-10-11"
    assert pedidos[0].params["hasta"] == "2026-11-10"


# --- HTTP -----------------------------------------------------------------


def cliente_que_responde(status, cuerpo=None, excepcion=None):
    def responder(request):
        if excepcion:
            raise excepcion
        return httpx.Response(status, json=cuerpo if cuerpo is not None else {})

    return httpx.Client(transport=httpx.MockTransport(responder))


def test_consultar_cendeu_pide_los_dos_endpoints():
    urls = []

    def responder(request):
        urls.append(str(request.url))
        return httpx.Response(404, json=NO_404["body"])

    crudo = Cendeu(httpx.Client(transport=httpx.MockTransport(responder))).consultar(YPF, timeout=5)

    assert urls == [
        f"https://api.bcra.gob.ar/CentralDeDeudores/v1.0/Deudas/{YPF}",
        f"https://api.bcra.gob.ar/CentralDeDeudores/v1.0/Deudas/Historicas/{YPF}",
    ]
    assert crudo["actual"]["status"] == 404


@pytest.mark.parametrize(
    "cliente, motivo",
    [
        (cliente_que_responde(503), "HTTP 503"),
        (cliente_que_responde(200, excepcion=httpx.ReadTimeout("lento")), "timeout"),
        (cliente_que_responde(200, excepcion=httpx.ConnectError("sin red")), "error de red"),
    ],
)
def test_timeout_5xx_y_red_son_fuente_caida(cliente, motivo):
    with pytest.raises(FuenteCaida, match=motivo):
        Cheques(cliente).consultar(YPF, timeout=10)
