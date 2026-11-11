"""Central de Deudores del BCRA (API pública, sin autenticación).

Dos fuentes:
- ``cendeu``: ``/Deudas`` (último período) + ``/Deudas/Historicas`` (24 meses).
- ``cheques``: ``/Deudas/ChequesRechazados``.

Son fuentes separadas porque cada endpoint responde 404 por su cuenta, y el 404
significa *sin antecedentes* (ADR 0003). Los montos de deuda llegan en miles de pesos;
las variables los exponen en pesos.
"""

from __future__ import annotations

from typing import Any

import httpx

from relevo.consultor import Disponible, NoDisponible, ResultadoFuente, SinAntecedentes
from relevo.fuentes import Variable, get_json

URL_BASE = "https://api.bcra.gob.ar/CentralDeDeudores/v1.0/Deudas"
MESES = 12


class Cendeu:
    nombre = "cendeu"
    variables = {
        "identificacion_rechazada": Variable("booleano", "El BCRA rechazó la identificación (HTTP 400)"),
        "denominacion": Variable("texto", "Nombre informado por la Central de Deudores"),
        "periodo": Variable("texto", "Último período informado (AAAAMM)"),
        "peor_situacion_actual": Variable("entero", "Peor situación (1 a 6) con deuda en el último período"),
        "peor_situacion_12m": Variable("entero", "Peor situación (1 a 6) en los últimos 12 meses"),
        "deuda_irregular_12m": Variable("pesos", "Mayor deuda en situación 3 o peor en los últimos 12 meses"),
        "proporcion_irregular_12m": Variable("proporción", "Mayor proporción de deuda irregular en 12 meses"),
        "entidades_con_deuda": Variable("entero", "Entidades con deuda en el último período"),
        "deuda_total": Variable("pesos", "Deuda total en el último período"),
        "en_proceso_judicial": Variable("booleano", "Alguna deuda del último período está en proceso judicial"),
        "meses_informados": Variable("entero", "Meses con información en el historial"),
    }

    def __init__(self, cliente: httpx.Client, url_base: str = URL_BASE):
        self.cliente = cliente
        self.url_base = url_base

    def consultar(self, identificacion: str, timeout: float) -> dict[str, Any]:
        return {
            "actual": get_json(self.cliente, f"{self.url_base}/{identificacion}", timeout),
            "historicas": get_json(self.cliente, f"{self.url_base}/Historicas/{identificacion}", timeout),
        }

    def traducir(self, crudo: dict[str, Any]) -> ResultadoFuente:
        actual, historicas = crudo["actual"], crudo["historicas"]
        estados = {actual["status"], historicas["status"]}
        if 400 in estados:
            return Disponible({"identificacion_rechazada": True})
        if estados == {404}:
            return SinAntecedentes()
        if not estados <= {200, 404}:
            return NoDisponible(f"respuesta inesperada: HTTP {sorted(estados)}")

        periodos = _periodos(actual) + _periodos(historicas)
        por_periodo: dict[str, list[dict]] = {}
        for periodo in periodos:  # el mismo período puede venir en los dos endpoints
            por_periodo.setdefault(periodo["periodo"], periodo["entidades"])
        ultimos = sorted(por_periodo, reverse=True)[:MESES]
        recientes = [[e for e in por_periodo[p] if e.get("situacion", 0) > 0] for p in ultimos]

        situaciones = [e["situacion"] for entidades in recientes for e in entidades]
        irregular = [sum(_pesos(e) for e in entidades if e["situacion"] >= 3) for entidades in recientes]
        totales = [sum(_pesos(e) for e in entidades) for entidades in recientes]
        proporciones = [i / t for i, t in zip(irregular, totales, strict=True) if t > 0]

        ultimo = _periodos(actual)
        ultimo_periodo = max(ultimo, key=lambda p: p["periodo"]) if ultimo else {"periodo": None, "entidades": []}
        con_deuda = [e for e in ultimo_periodo["entidades"] if _pesos(e) > 0]
        return Disponible(
            {
                "identificacion_rechazada": False,
                "denominacion": _resultados(actual, historicas).get("denominacion"),
                "periodo": ultimo_periodo["periodo"],
                "peor_situacion_actual": max((e["situacion"] for e in con_deuda), default=0),
                "peor_situacion_12m": max(situaciones, default=0),
                "deuda_irregular_12m": max(irregular, default=0.0),
                "proporcion_irregular_12m": max(proporciones, default=0.0),
                "entidades_con_deuda": len({e["entidad"] for e in con_deuda}),
                "deuda_total": sum(_pesos(e) for e in con_deuda),
                "en_proceso_judicial": any(e.get("procesoJud") for e in ultimo_periodo["entidades"]),
                "meses_informados": len(ultimos),
            }
        )


class Cheques:
    nombre = "cheques"
    variables = {
        "rechazados_total": Variable("entero", "Cheques rechazados informados"),
        "rechazados_impagos": Variable("entero", "Cheques rechazados sin pagar"),
        "monto_impago": Variable("pesos", "Monto de los cheques rechazados sin pagar"),
    }

    def __init__(self, cliente: httpx.Client, url_base: str = URL_BASE):
        self.cliente = cliente
        self.url_base = url_base

    def consultar(self, identificacion: str, timeout: float) -> dict[str, Any]:
        return get_json(self.cliente, f"{self.url_base}/ChequesRechazados/{identificacion}", timeout)

    def traducir(self, crudo: dict[str, Any]) -> ResultadoFuente:
        if crudo["status"] == 404:
            return SinAntecedentes()
        if crudo["status"] != 200:
            return NoDisponible(f"respuesta inesperada: HTTP {crudo['status']}")
        cheques = [
            cheque
            for causal in crudo["body"]["results"].get("causales", [])
            for entidad in causal.get("entidades", [])
            for cheque in entidad.get("detalle", [])
        ]
        impagos = [c for c in cheques if not c.get("fechaPago")]
        return Disponible(
            {
                "rechazados_total": len(cheques),
                "rechazados_impagos": len(impagos),
                "monto_impago": sum(c.get("monto", 0) for c in impagos),
            }
        )


def _resultados(*respuestas: dict[str, Any]) -> dict[str, Any]:
    for respuesta in respuestas:
        if respuesta["status"] == 200:
            return respuesta["body"].get("results") or {}
    return {}


def _periodos(respuesta: dict[str, Any]) -> list[dict[str, Any]]:
    if respuesta["status"] != 200:
        return []
    return (respuesta["body"].get("results") or {}).get("periodos") or []


def _pesos(entidad: dict[str, Any]) -> float:
    return (entidad.get("monto") or 0.0) * 1000
