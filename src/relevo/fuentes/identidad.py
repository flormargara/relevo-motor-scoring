"""Identidad: edad del solicitante a partir de su fecha de nacimiento.

La fuente ``identidad`` expone siempre las mismas variables, venga el dato de donde venga:

- ``edad``: años cumplidos a la fecha de consulta.
- ``validada``: si un organismo oficial confirmó el dato.
- ``origen``: ``declarada`` (la cargó el analista en el alta) o ``renaper``.

Hoy solo existe ``declarada``: ``validada`` es siempre falso. En producción, el adapter de
RENAPER (servicio de validación de identidad, con Convenio Único de Confronte de Datos)
devuelve las mismas variables con ``origen="renaper"`` y ``validada=True``, así que las
reglas y la ficha no cambian.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from relevo.consultor import Disponible, ResultadoFuente, SinAntecedentes
from relevo.fuentes import Variable

ORIGEN_DECLARADA = "declarada"


def calcular_edad(nacimiento: date, hoy: date) -> int:
    return hoy.year - nacimiento.year - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))


VARIABLES_IDENTIDAD = {
    "edad": Variable("entero", "Edad en años"),
    "validada": Variable("booleano", "La fecha de nacimiento está validada (no declarada)"),
    "origen": Variable("texto", "De dónde sale la fecha: declarada, simulada o renaper"),
}


class IdentidadDeclarada:
    nombre = "identidad"
    variables = VARIABLES_IDENTIDAD

    def __init__(self, fecha_nacimiento: date | None, hoy: Callable[[], date] = date.today):
        self.fecha_nacimiento = fecha_nacimiento
        self.hoy = hoy

    def consultar(self, identificacion: str, timeout: float) -> dict[str, Any] | None:
        if self.fecha_nacimiento is None:
            return None
        return {"fecha_nacimiento": self.fecha_nacimiento.isoformat(), "fecha_consulta": self.hoy().isoformat()}

    def traducir(self, crudo: dict[str, Any] | None) -> ResultadoFuente:
        if crudo is None:
            return SinAntecedentes()
        edad = calcular_edad(date.fromisoformat(crudo["fecha_nacimiento"]), date.fromisoformat(crudo["fecha_consulta"]))
        return Disponible({"edad": edad, "validada": False, "origen": ORIGEN_DECLARADA})
