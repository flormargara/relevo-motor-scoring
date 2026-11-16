"""Chat con tus datos (agente de informe), con un ``ClaudeFalso``."""

import pytest
from adapters_falsos import ClaudeFalso

from conftest import BASE_PYME, armar_consultor, solicitud_pyme
from relevo.chat import Turno, redactar_informe, responder
from relevo.motor import evaluar

# --- agente de informe -------------------------------------------------------


@pytest.fixture
def evaluacion(reglas_pyme):
    return evaluar(solicitud_pyme(), reglas_pyme, armar_consultor(BASE_PYME))


def test_el_informe_recibe_la_evaluacion_completa(evaluacion):
    cliente = ClaudeFalso("RESUMEN\n- Aprobado.\n")

    informe = redactar_informe(evaluacion, cliente)

    assert informe == "RESUMEN\n- Aprobado."
    pedido = cliente.pedidos[0]
    assert '"veredicto": "APROBADO"' in pedido["prompt"]
    assert "lista_negra" in pedido["prompt"]  # la traza
    assert "no lo cambiás" in pedido["sistema"]
    assert "puntos están incluidos en el total" in pedido["sistema"]
    assert pedido["archivos"] == []


def test_responder_incluye_el_historial_y_la_pregunta(evaluacion):
    cliente = ClaudeFalso("Porque el rating es A.")
    historial = [Turno("analista", "¿Qué tasa tiene?"), Turno("agente", "BADLAR más spread.")]

    respuesta = responder(evaluacion, historial, "¿Por qué se aprobó?", cliente)

    assert respuesta == "Porque el rating es A."
    prompt = cliente.pedidos[0]["prompt"]
    assert "analista: ¿Qué tasa tiene?" in prompt and "agente: BADLAR más spread." in prompt
    assert prompt.rstrip().endswith("Respondé la pregunta en pocas frases, con los datos de la evaluación.")
    assert "¿Por qué se aprobó?" in prompt
