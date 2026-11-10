from __future__ import annotations

from typing import Any

import pytest

from relevo.consultor import ConsultorFalso, Disponible, ResultadoFuente
from relevo.dominio import Solicitud
from relevo.reglas import cargar_reglas

CUIT_PYME = "30712345671"
CUIT_PYME_DV_INVALIDO = "30712345672"
CUIL_HUMANA = "20301234563"
CUIL_MONOTRIBUTISTA = "27301234568"

BASE_PYME: dict[str, dict[str, Any]] = {
    "propios": {"lista_negra": False},
    "igj": {"dada_de_baja": False, "antiguedad_anios": 12},
    "cendeu": {"peor_situacion_12m": 1, "entidades_con_deuda": 2, "deuda_total": 20_000_000},
    "cheques": {"rechazados_impagos": 0},
    "bureau": {"score": 850},
    "documentos": {"ventas_anuales": 240_000_000, "confianza_minima": 0.95},
    "badlar": {"tasa": 30.0},
}

BASE_MONOTRIBUTISTA: dict[str, dict[str, Any]] = {
    "propios": {"lista_negra": False},
    "identidad": {"edad": 40},
    "cendeu": {"peor_situacion_12m": 1, "entidades_con_deuda": 1, "deuda_total": 500_000},
    "cheques": {"rechazados_impagos": 0},
    "bureau": {"score": 720},
    "documentos": {
        "categoria_monotributo": "D",
        "antiguedad_actividad_anios": 6,
        "confianza_minima": 0.9,
    },
    "badlar": {"tasa": 30.0},
}


def armar_consultor(base: dict[str, dict[str, Any]], **cambios: ResultadoFuente | dict[str, Any]) -> ConsultorFalso:
    """Consultor con las fuentes de `base`. Un dict en `cambios` pisa variables;
    un ResultadoFuente reemplaza la fuente entera."""
    respuestas: dict[str, ResultadoFuente] = {}
    for fuente in base.keys() | cambios.keys():
        cambio = cambios.get(fuente)
        if cambio is None or isinstance(cambio, dict):
            respuestas[fuente] = Disponible({**base.get(fuente, {}), **(cambio or {})})
        else:
            respuestas[fuente] = cambio
    return ConsultorFalso(respuestas)


def solicitud_pyme(cuit: str = CUIT_PYME, monto: float = 10_000_000, plazo_meses: int = 12) -> Solicitud:
    return Solicitud(cuit=cuit, segmento="pyme", monto=monto, plazo_meses=plazo_meses)


def solicitud_monotributista(
    cuit: str = CUIL_MONOTRIBUTISTA, monto: float = 2_000_000, plazo_meses: int = 12
) -> Solicitud:
    return Solicitud(cuit=cuit, segmento="monotributista", monto=monto, plazo_meses=plazo_meses)


def cuota_francesa(monto: float, tna_porcentaje: float, plazo: int) -> float:
    i = tna_porcentaje / 100 / 12
    return round(monto * i / (1 - (1 + i) ** -plazo), 2)


@pytest.fixture(scope="session")
def reglas_pyme():
    return cargar_reglas("pyme")


@pytest.fixture(scope="session")
def reglas_monotributista():
    return cargar_reglas("monotributista")
