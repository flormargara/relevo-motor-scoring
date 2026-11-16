"""Traducción de lo extraído a las variables de la fuente ``documentos`` del motor."""

import json
from datetime import date

from conftest import CUIT_PYME
from relevo.catalogo import catalogo
from relevo.fuentes.documentos import variables_documentos
from relevo.lectura_documentos import CampoExtraido, DocumentoExtraido, documento_manual

HOY = date(2026, 11, 9)


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
    # contrato con el catálogo del flujo (ADR 0007)
    del_catalogo = {v.nombre for v in catalogo("monotributista") if v.fuente == "documentos"}
    assert {f"documentos.{k}" for k in variables} == del_catalogo - {"documentos.sin_antecedentes"}


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
