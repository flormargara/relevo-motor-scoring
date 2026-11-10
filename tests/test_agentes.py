"""Agentes documental y de informe, con un ``ClaudeFalso``."""

import json
from datetime import date

import pytest
from adapters_falsos import ClaudeFalso

from conftest import BASE_PYME, CUIT_PYME, armar_consultor, solicitud_pyme
from relevo.agentes import (
    CampoExtraido,
    DocumentoExtraido,
    RespuestaInvalida,
    Turno,
    documento_manual,
    extraer_documento,
    redactar_informe,
    responder,
    variables_documentos,
)
from relevo.clientes_claude import AgenteNoDisponible
from relevo.motor import evaluar

HOY = date(2026, 11, 9)


def respuesta_balance(**campos):
    base = {
        "cuit": {"valor": "30-71234567-1", "confianza": 0.98},
        "denominacion": {"valor": "Metalúrgica Ejemplo SA", "confianza": 0.97},
        "fecha_cierre_ejercicio": {"valor": "2025-12-31", "confianza": 0.95},
        "ventas_anuales": {"valor": 240_000_000, "confianza": 0.9},
    }
    return json.dumps({"tipo": "balance", "campos": base | campos, "observaciones": "En pesos."})


# --- agente documental -------------------------------------------------------


def test_extrae_un_balance(tmp_path):
    archivo = tmp_path / "balance.pdf"
    archivo.write_bytes(b"%PDF")
    cliente = ClaudeFalso(respuesta_balance())

    doc = extraer_documento(archivo, cliente)

    assert doc.tipo == "balance"
    assert doc.campos["cuit"] == CampoExtraido("30712345671", 0.98)
    assert doc.campos["ventas_anuales"] == CampoExtraido(240_000_000.0, 0.9)
    assert doc.observaciones == "En pesos."
    assert cliente.pedidos[0]["archivos"] == [archivo]


def test_tolera_texto_alrededor_del_json(tmp_path):
    cliente = ClaudeFalso("Acá está:\n```json\n" + respuesta_balance() + "\n```")

    assert extraer_documento(tmp_path / "b.pdf", cliente).tipo == "balance"


@pytest.mark.parametrize(
    "valor, esperado",
    [("1.234.567,89", 1_234_567.89), ("1234567.5", 1_234_567.5), ("$ 2.500.000", 2_500_000.0), ("mucho", None)],
)
def test_normaliza_montos_escritos_como_texto(tmp_path, valor, esperado):
    cliente = ClaudeFalso(respuesta_balance(ventas_anuales={"valor": valor, "confianza": 0.9}))

    campo = extraer_documento(tmp_path / "b.pdf", cliente).campos["ventas_anuales"]

    assert campo.valor == esperado
    assert campo.confianza == (0.9 if esperado is not None else 0.0)


def test_valor_invalido_o_ausente_queda_sin_dato_con_confianza_cero(tmp_path):
    respuesta = json.dumps(
        {
            "tipo": "constancia_arca",
            "campos": {
                "cuit": {"valor": "27301234568", "confianza": 0.99},
                "categoria_monotributo": {"valor": "Z", "confianza": 0.9},
                "fecha_inicio_actividades": {"valor": "hace mucho", "confianza": 0.8},
            },
        }
    )

    doc = extraer_documento(tmp_path / "c.pdf", ClaudeFalso(respuesta))

    assert doc.campos["categoria_monotributo"] == CampoExtraido(None, 0.0)
    assert doc.campos["fecha_inicio_actividades"] == CampoExtraido(None, 0.0)
    assert doc.campos["denominacion"] == CampoExtraido(None, 0.0)  # no vino


def test_respuesta_sin_json_es_respuesta_invalida(tmp_path):
    with pytest.raises(RespuestaInvalida):
        extraer_documento(tmp_path / "b.pdf", ClaudeFalso("No pude leer el archivo."))


def test_agente_caido_se_propaga(tmp_path):
    with pytest.raises(AgenteNoDisponible):
        extraer_documento(tmp_path / "b.pdf", ClaudeFalso(AgenteNoDisponible("caído")))


# --- variables para el motor -------------------------------------------------


def constancia(categoria="D", inicio="2020-11-09", cuit="27301234568", confianza=0.95):
    return DocumentoExtraido(
        "constancia.pdf",
        "constancia_arca",
        {
            "cuit": CampoExtraido(cuit, 0.99),
            "categoria_monotributo": CampoExtraido(categoria, confianza),
            "fecha_inicio_actividades": CampoExtraido(inicio, 0.9),
        },
    )


def test_variables_de_monotributista_calculan_la_antiguedad():
    variables = variables_documentos([constancia()], "monotributista", "27-30123456-8", HOY)

    assert variables == {
        "categoria_monotributo": "D",
        "antiguedad_actividad_anios": 6.0,
        "cuit_coincide": True,
        "confianza_minima": 0.9,
    }


def test_variable_faltante_queda_sin_dato_y_baja_la_confianza_a_cero():
    variables = variables_documentos([constancia(categoria=None)], "monotributista", "27301234568", HOY)

    assert variables["categoria_monotributo"] is None
    assert variables["confianza_minima"] == 0.0


def test_cuit_distinto_al_de_la_solicitud_baja_la_confianza_a_cero():
    variables = variables_documentos([constancia(cuit="20111111112")], "monotributista", "27301234568", HOY)

    assert variables["cuit_coincide"] is False
    assert variables["confianza_minima"] == 0.0


def test_la_carga_manual_pisa_lo_extraido():
    manual = documento_manual({"categoria_monotributo": "f", "antiguedad_actividad_anios": ""})

    variables = variables_documentos(
        [constancia(categoria="D", confianza=0.6), manual], "monotributista", "27301234568", HOY
    )

    assert manual.campos == {"categoria_monotributo": CampoExtraido("F", 1.0)}
    assert variables["categoria_monotributo"] == "F"
    assert variables["confianza_minima"] == 0.9  # la de la antigüedad, extraída


def test_sin_documentos_no_hay_variables():
    assert variables_documentos([], "pyme", CUIT_PYME, HOY) is None
    assert documento_manual({"ventas_anuales": None}) is None


def test_documento_extraido_ida_y_vuelta_por_dict():
    doc = constancia()

    assert DocumentoExtraido.desde_dict(json.loads(json.dumps(doc.a_dict()))) == doc


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
