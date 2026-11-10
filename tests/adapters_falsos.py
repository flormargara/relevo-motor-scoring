"""Adapters de fuente falsos para probar el servicio y la app sin red."""

from __future__ import annotations

from collections import Counter
from typing import Any

from relevo.consultor import Disponible, NoDisponible, ResultadoFuente, SinAntecedentes
from relevo.fuentes import FuenteCaida


class AdapterFijo:
    def __init__(self, nombre: str, respuesta: ResultadoFuente | dict[str, Any], contador: Counter):
        self.nombre = nombre
        self.respuesta = respuesta
        self.contador = contador

    def consultar(self, identificacion: str, timeout: float) -> Any:
        self.contador[self.nombre] += 1
        if isinstance(self.respuesta, NoDisponible):
            raise FuenteCaida(self.respuesta.motivo)
        if isinstance(self.respuesta, SinAntecedentes):
            return None
        return dict(self.respuesta.variables if isinstance(self.respuesta, Disponible) else self.respuesta)

    def traducir(self, crudo: Any) -> ResultadoFuente:
        return SinAntecedentes() if crudo is None else Disponible(crudo)


class FabricaFalsa:
    """Devuelve las mismas fuentes para cualquier solicitud, salvo `documentos`, que salen
    de la solicitud guardada. Cuenta cuántas veces se consultó cada fuente."""

    def __init__(self, base: dict[str, Any], **cambios: Any):
        self.respuestas = {**base, **cambios}
        self.contador: Counter = Counter()

    def __call__(self, guardada) -> dict[str, AdapterFijo]:
        respuestas = {k: v for k, v in self.respuestas.items() if k != "documentos"}
        respuestas["documentos"] = Disponible(guardada.documentos) if guardada.documentos else SinAntecedentes()
        return {nombre: AdapterFijo(nombre, r, self.contador) for nombre, r in respuestas.items()}


class ClaudeFalso:
    """``ClienteClaude`` que devuelve respuestas fijas (en orden) y registra los pedidos.
    Una respuesta que es una excepción se lanza."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.pedidos: list[dict[str, Any]] = []

    def completar(self, prompt, archivos=(), sistema=None):
        self.pedidos.append({"prompt": prompt, "archivos": list(archivos), "sistema": sistema})
        respuesta = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta
