"""CUIT reservados para la demo, fuera del entrenamiento de los modelos de ML (ADR 0009).

La regla es la misma en todos lados: un CUIT es de la demo si ``md5(str(cuit))``, leído
como entero desde sus primeros 8 dígitos hexadecimales, da resto menor a
``PORCENTAJE_RESERVADO`` al dividirlo por 100. La usan la demo (para elegir solicitantes)
y el control de los modelos de ML (que tienen que declarar que la excluyeron al entrenar).
"""

from __future__ import annotations

import hashlib

PORCENTAJE_RESERVADO = 5
REGLA = "md5-8hex-mod100"  # cómo la declara un modelo en su metadata (contrato del modelo de ML)


def es_reservado(cuit: str | int) -> bool:
    """El CUIT queda para la demo y fuera del entrenamiento del modelo de ML."""
    return int(hashlib.md5(str(int(cuit)).encode()).hexdigest()[:8], 16) % 100 < PORCENTAJE_RESERVADO


def sql_reservado(columna: str = "cuit") -> str:
    """La misma regla en SQL (duckdb), para excluir los reservados al entrenar."""
    return f"('0x' || left(md5(CAST({columna} AS VARCHAR)), 8))::UBIGINT % 100 < {PORCENTAJE_RESERVADO}"
