"""Fuentes de juguete: bureau comercial, identidad (tipo RENAPER) y datos propios.

Los valores salen de ``config/juguete.yaml``. Un CUIT que no figura ahí recibe valores
deterministas derivados del propio CUIT, así el alta manual también funciona. Para la
demo, un CUIT listado en ``caidas`` simula que la fuente no responde.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from relevo.consultor import Disponible, ResultadoFuente
from relevo.fuentes import FuenteCaida

CONFIG_POR_DEFECTO = Path(__file__).resolve().parents[3] / "config" / "juguete.yaml"


def cargar_juguete(ruta: Path = CONFIG_POR_DEFECTO) -> dict[str, Any]:
    return yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}


def _semilla(cuit: str, sal: str) -> int:
    return int(hashlib.sha256(f"{sal}:{cuit}".encode()).hexdigest(), 16)


class _Juguete:
    nombre = ""

    def __init__(self, datos: Mapping[str, Any]):
        seccion = datos.get(self.nombre) or {}
        self.casos: Mapping[str, Any] = seccion.get("casos") or {}
        self.caidas = set(seccion.get("caidas") or [])

    def consultar(self, identificacion: str, timeout: float) -> dict[str, Any]:
        if identificacion in self.caidas:
            raise FuenteCaida("caída simulada")
        return {"cuit": identificacion, **self._datos(identificacion)}

    def _datos(self, cuit: str) -> dict[str, Any]:
        raise NotImplementedError


class Bureau(_Juguete):
    nombre = "bureau"

    def _datos(self, cuit: str) -> dict[str, Any]:
        caso = self.casos.get(cuit) or {}
        return {"score": caso.get("score", 300 + _semilla(cuit, "bureau") % 651)}

    def traducir(self, crudo: dict[str, Any]) -> ResultadoFuente:
        return Disponible({"score": crudo["score"]})


class Propios(_Juguete):
    nombre = "propios"

    def __init__(self, datos: Mapping[str, Any]):
        super().__init__(datos)
        self.lista_negra = set((datos.get(self.nombre) or {}).get("lista_negra") or [])

    def _datos(self, cuit: str) -> dict[str, Any]:
        caso = self.casos.get(cuit) or {}
        return {"lista_negra": cuit in self.lista_negra, "creditos_previos": caso.get("creditos_previos", 0)}

    def traducir(self, crudo: dict[str, Any]) -> ResultadoFuente:
        return Disponible({"lista_negra": crudo["lista_negra"], "creditos_previos": crudo["creditos_previos"]})


class Identidad(_Juguete):
    nombre = "identidad"

    def __init__(self, datos: Mapping[str, Any], hoy: Callable[[], date] = date.today):
        super().__init__(datos)
        self.hoy = hoy

    def _datos(self, cuit: str) -> dict[str, Any]:
        caso = self.casos.get(cuit) or {}
        nacimiento = caso.get("fecha_nacimiento")
        if nacimiento is None:  # entre 25 y 70 años, fijo por CUIT
            dias = 365 * 25 + _semilla(cuit, "identidad") % (365 * 45)
            nacimiento = (date(2026, 1, 1) - timedelta(days=dias)).isoformat()
        return {
            "fecha_nacimiento": str(nacimiento),
            "validada": caso.get("validada", True),
            "fecha_consulta": self.hoy().isoformat(),
        }

    def traducir(self, crudo: dict[str, Any]) -> ResultadoFuente:
        nacimiento = date.fromisoformat(crudo["fecha_nacimiento"])
        hoy = date.fromisoformat(crudo["fecha_consulta"])
        edad = hoy.year - nacimiento.year - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))
        return Disponible({"edad": edad, "validada": crudo["validada"], "origen": "simulada"})
