"""BADLAR (bancos privados, TNA %) de la API de Estadísticas v4 del BCRA, variable 7.

Se publica con unos días de demora, así que se consulta una ventana y se toma el
último valor disponible.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import httpx

from relevo.consultor import Disponible, NoDisponible, ResultadoFuente
from relevo.fuentes import Variable, get_json

URL = "https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias/7"
VENTANA_DIAS = 30


class Badlar:
    nombre = "badlar"
    variables = {
        "tasa": Variable("número", "Última BADLAR publicada (TNA en %)"),
        "fecha": Variable("fecha", "Fecha de la última BADLAR publicada"),
        "desactualizada": Variable("booleano", "El valor es el último guardado porque el BCRA no respondió"),
    }

    def __init__(self, cliente: httpx.Client, hoy: Callable[[], date] = date.today, url: str = URL):
        self.cliente = cliente
        self.hoy = hoy
        self.url = url

    def consultar(self, identificacion: str, timeout: float) -> dict[str, Any]:
        hasta = self.hoy()
        params = {"desde": (hasta - timedelta(days=VENTANA_DIAS)).isoformat(), "hasta": hasta.isoformat()}
        return get_json(self.cliente, self.url, timeout, params=params)

    def traducir(self, crudo: dict[str, Any]) -> ResultadoFuente:
        if crudo["status"] != 200:
            return NoDisponible(f"respuesta inesperada: HTTP {crudo['status']}")
        detalle = [d for r in crudo["body"].get("results", []) for d in r.get("detalle", [])]
        if not detalle:
            return NoDisponible(f"sin datos publicados en los últimos {VENTANA_DIAS} días")
        ultimo = max(detalle, key=lambda d: d["fecha"])
        return Disponible({"tasa": float(ultimo["valor"]), "fecha": ultimo["fecha"], "desactualizada": False})
