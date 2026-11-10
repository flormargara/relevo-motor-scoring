"""Adapters de fuente.

Cada adapter hace dos cosas, separadas a propósito:
- ``consultar(identificacion, timeout) -> crudo``: trae la respuesta tal como llegó
  (HTTP, archivo o juguete). Si la fuente no pudo responder (timeout, 5xx, error de
  red) lanza ``FuenteCaida``, que el ``ConsultorReal`` puede reintentar.
- ``traducir(crudo) -> ResultadoFuente``: convierte el crudo en variables del dominio.
  Es una función pura y se testea contra respuestas reales guardadas como fixtures.

El crudo es lo que queda guardado como snapshot, así una evaluación se puede
reproducir traduciendo de nuevo lo mismo.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from relevo.consultor import ResultadoFuente


class FuenteCaida(Exception):
    """La fuente no pudo responder. Se puede reintentar."""


class AdapterFuente(Protocol):
    nombre: str

    def consultar(self, identificacion: str, timeout: float) -> Any: ...

    def traducir(self, crudo: Any) -> ResultadoFuente: ...


def get_json(cliente: httpx.Client, url: str, timeout: float, params: dict | None = None) -> dict[str, Any]:
    """GET que devuelve ``{"status": código, "body": json}``. Los 4xx son respuestas
    válidas (el 404 de CENDEU es un dato); timeout, error de red y 5xx son ``FuenteCaida``."""
    try:
        respuesta = cliente.get(url, params=params, timeout=timeout)
    except httpx.TimeoutException:
        raise FuenteCaida(f"timeout ({timeout:g} s)") from None
    except httpx.HTTPError as error:
        raise FuenteCaida(f"error de red: {type(error).__name__}") from None
    if respuesta.status_code >= 500:
        raise FuenteCaida(f"HTTP {respuesta.status_code}")
    try:
        cuerpo = respuesta.json()
    except ValueError:
        raise FuenteCaida(f"respuesta no es JSON (HTTP {respuesta.status_code})") from None
    return {"status": respuesta.status_code, "body": cuerpo}
