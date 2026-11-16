"""Lectura de documentos (agente documental), con un ``ClaudeFalso``."""

import json
from datetime import date

import pytest
from adapters_falsos import ClaudeFalso

from relevo.clientes_claude import AgenteNoDisponible
from relevo.lectura_documentos import CampoExtraido, RespuestaInvalida, extraer_documento

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
    assert "No compares el CUIT con el nombre del archivo" in cliente.pedidos[0]["sistema"]


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
