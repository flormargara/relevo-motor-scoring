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


# --- agente de cartera -------------------------------------------------------


def test_el_agente_de_cartera_recibe_el_monitoreo_calculado():
    from datetime import date, datetime

    from relevo.cartera import Desembolso, EstadoMensual, RegistroCartera
    from relevo.chat import responder_cartera
    from relevo.monitoreo import calcular
    from relevo.repositorio import Otorgada

    otorgadas = [Otorgada(f"s{i}", "pyme", datetime(2026, 1, 5), "pyme.v3", "C", 0.06) for i in range(40)]
    registros = [
        RegistroCartera(
            f"s{i}",
            True,
            Desembolso(date(2026, 1, 10), 1000.0, 12, 60.0),
            tuple(EstadoMensual(date(2026, m, 1), 900.0, 100.0, 90 if i < 8 and m == 5 else 0) for m in range(2, 8)),
        )
        for i in range(40)
    ]
    cliente = ClaudeFalso("El rating C defaultea más de lo esperado.")

    respuesta = responder_cartera(
        calcular(otorgadas, registros), [Turno("analista", "¿Cómo viene?")], "¿Y el C?", cliente
    )

    assert respuesta == "El rating C defaultea más de lo esperado."
    pedido = cliente.pedidos[0]
    assert '"default_observado":8' in pedido["prompt"] and '"estado":"rojo"' in pedido["prompt"]
    assert "analista: ¿Cómo viene?" in pedido["prompt"] and "¿Y el C?" in pedido["prompt"]
    assert "no hacés cuentas nuevas" in pedido["sistema"] and pedido["archivos"] == []
