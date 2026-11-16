"""Chat with your data: interpretar, ejecutar y explicar, con un agente y datos falsos."""

import json

import pytest
from adapters_falsos import ClaudeFalso

from relevo.chat_datos import Plan, Turno, leer_plan, responder
from relevo.reglas import ReglasInvalidas

FLUJOS = {"pyme": "PyME · Originación", "monotributista": "Monotributista · Originación"}


class DatosFalsos:
    def __init__(self, falla_primera=False):
        self.simulaciones = []
        self.falla_primera = falla_primera

    def flujos(self):
        return FLUJOS

    def contexto_reglas(self, flujo):
        return f"reglas de {flujo}"

    def monitoreo(self):
        raise AssertionError("no se pidió")

    def evaluaciones(self):
        return []

    def simular(self, flujo, origen, operaciones, version):
        self.simulaciones.append((flujo, origen, list(operaciones), version))
        if self.falla_primera and len(self.simulaciones) == 1:
            raise ReglasInvalidas("foo.bar no está en el catálogo")
        return {"simulado": True}


@pytest.mark.parametrize(
    "texto, plan",
    [
        ('{"analisis": "monitoreo"}', Plan("monitoreo")),
        ('Claro:\n```json\n{"analisis": "evaluaciones"}\n```', Plan("evaluaciones")),
        ('{"analisis": "ninguno", "motivo": "es un saludo"}', Plan("ninguno", motivo="es un saludo")),
        (
            '{"analisis": "simulacion", "flujo": "pyme", "origen": "version", "version": "2"}',
            Plan("simulacion", flujo="pyme", origen="version", version=2),
        ),
        (
            '{"analisis": "simulacion", "flujo": "pyme", "cambios": [{"op": "quitar_regla"}]}',
            Plan("simulacion", flujo="pyme", origen="cambios", cambios=({"op": "quitar_regla"},)),
        ),
    ],
)
def test_leer_plan(texto, plan):
    assert leer_plan(texto, FLUJOS) == plan


@pytest.mark.parametrize(
    "texto, motivo",
    [
        ("no sé", "no pude interpretar"),
        ('{"analisis": "borrar_todo"}', "no pude interpretar"),
        ('{"analisis": "simulacion", "flujo": "empresas"}', "no hay un flujo 'empresas'"),
        ('{"analisis": "simulacion", "flujo": "pyme", "origen": "internet"}', "no pueden salir de 'internet'"),
        ('{"analisis": "simulacion", "flujo": "pyme", "cambios": "rechazar D"}', "lista de operaciones"),
    ],
)
def test_planes_que_no_se_pueden_leer(texto, motivo):
    plan = leer_plan(texto, FLUJOS)

    assert plan.analisis == "ninguno" and motivo in plan.motivo


def test_responder_pasa_el_plan_anterior_y_explica_el_resultado():
    plan = {
        "analisis": "simulacion",
        "flujo": "pyme",
        "cambios": [{"op": "quitar_regla", "seccion": "killers", "id": "x"}],
    }
    cliente = ClaudeFalso(json.dumps(plan), "Aprobarías más.")
    datos = DatosFalsos()
    historial = [Turno("analista", "¿Y sin x?"), Turno("agente", "Antes.", {"analisis": "monitoreo"})]

    respuesta = responder("¿Y sin x?", historial, datos, cliente)

    assert respuesta.texto == "Aprobarías más." and respuesta.resultado == {"simulado": True}
    assert datos.simulaciones == [("pyme", "cambios", plan["cambios"], None)]
    pedido_plan, pedido_explicacion = cliente.pedidos
    assert "reglas de pyme" in pedido_plan["prompt"] and "reglas de monotributista" in pedido_plan["prompt"]
    assert '(pedido: {"analisis": "monitoreo"})' in pedido_plan["prompt"]
    assert "nada más que el JSON" in pedido_plan["sistema"]
    assert '{"simulado":true}' in pedido_explicacion["prompt"] and "(pedido:" not in pedido_explicacion["prompt"]
    assert "No hacés cuentas nuevas" in pedido_explicacion["sistema"]


def test_un_pedido_que_falla_se_corrige_una_vez():
    plan = json.dumps({"analisis": "simulacion", "flujo": "pyme", "cambios": [{"op": "poner"}]})
    cliente = ClaudeFalso(plan, plan, "No se pudo.")
    datos = DatosFalsos(falla_primera=True)

    respuesta = responder("¿Y si…?", [], datos, cliente)

    assert len(datos.simulaciones) == 2 and respuesta.error is None
    assert "El pedido anterior falló: foo.bar no está en el catálogo" in cliente.pedidos[1]["prompt"]


def test_simular_sin_cambios_no_llama_al_simulador():
    plan = json.dumps({"analisis": "simulacion", "flujo": "pyme"})
    cliente = ClaudeFalso(plan, plan, "Decime qué querés cambiar.")
    datos = DatosFalsos()

    respuesta = responder("Simulá algo", [], datos, cliente)

    assert datos.simulaciones == [] and respuesta.error == "no hay cambios para simular"
    assert '"error":"no hay cambios para simular"' in cliente.pedidos[-1]["prompt"]


def test_sin_analisis_explica_el_motivo():
    cliente = ClaudeFalso('{"analisis": "ninguno", "motivo": "no hay datos de cobranzas"}', "No tengo esos datos.")

    respuesta = responder("¿Cuánto cobramos?", [], DatosFalsos(), cliente)

    assert respuesta.texto == "No tengo esos datos." and respuesta.resultado is None
    assert "no hay datos de cobranzas" in cliente.pedidos[1]["prompt"]
