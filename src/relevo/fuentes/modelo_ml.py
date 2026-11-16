"""Modelo de ML: la PD de un modelo que vive en otro repositorio (ADR 0010).

Cumple ``docs/contrato-modelo-ml.md``: ``POST /v1/pd`` con CUIT, segmento, período,
modelo y versión. El pedido queda en el crudo junto con la respuesta, así ``traducir``
(pura) puede validar que la respuesta corresponde al pedido y que el modelo no usó datos
posteriores al período.

``metadata`` lee ``GET /v1/modelos`` y ``verificar`` controla que una versión esté
publicada y declare la exclusión de los CUIT reservados (ADR 0009).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from relevo.consultor import Disponible, NoDisponible, ResultadoFuente, SinAntecedentes
from relevo.fuentes import FuenteCaida, Variable
from relevo.reservados import PORCENTAJE_RESERVADO, REGLA

NO_CONFIGURADO = "modelo de ML no configurado (RELEVO_MODELO_URL)"


@dataclass(frozen=True)
class ConexionModelo:
    url: str | None
    token: str | None = None

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}


def periodo_de(fecha: date) -> int:
    """El período (AAAAMM) que se le pide al modelo: el mes anterior a la evaluación, el último
    que publica la Central de Deudores."""
    mes = fecha.year * 12 + fecha.month - 2
    return (mes // 12) * 100 + mes % 12 + 1


class ModeloML:
    nombre = "modelo_ml"
    variables = {
        "pd": Variable("proporción", "PD a 12 meses del modelo de ML que decide (de 0 a 1)"),
        "version": Variable("texto", "Modelo y versión que dio la PD (modelo@versión)"),
        "periodo_datos": Variable("entero", "Último período (AAAAMM) que usó el modelo"),
    }

    def __init__(
        self,
        cliente: httpx.Client,
        conexion: ConexionModelo,
        modelo: str,
        version: str,
        segmento: str,
        periodo: int,
    ):
        self.cliente = cliente
        self.conexion = conexion
        self.pedido = {"segmento": segmento, "periodo": periodo, "modelo": modelo, "version": version}

    def consultar(self, identificacion: str, timeout: float) -> dict[str, Any]:
        if not self.conexion.url:
            raise FuenteCaida(NO_CONFIGURADO)
        pedido = {"cuit": identificacion, **self.pedido}
        try:
            respuesta = self.cliente.post(
                f"{self.conexion.url.rstrip('/')}/v1/pd", json=pedido, headers=self.conexion.headers(), timeout=timeout
            )
        except httpx.TimeoutException:
            raise FuenteCaida(f"timeout ({timeout:g} s)") from None
        except httpx.HTTPError as error:
            raise FuenteCaida(f"error de red: {type(error).__name__}") from None
        if respuesta.status_code >= 500:
            raise FuenteCaida(f"HTTP {respuesta.status_code}")
        try:
            cuerpo = respuesta.json()
        except ValueError:
            cuerpo = {"texto": respuesta.text[:500]}
        return {"pedido": pedido, "status": respuesta.status_code, "body": cuerpo}

    def traducir(self, crudo: Mapping[str, Any]) -> ResultadoFuente:
        status, cuerpo, pedido = crudo["status"], crudo["body"], crudo["pedido"]
        if status == 404:
            return SinAntecedentes()
        if status != 200:
            detalle = cuerpo.get("detail") or cuerpo.get("error") or cuerpo if isinstance(cuerpo, dict) else cuerpo
            return NoDisponible(f"el modelo rechazó el pedido: HTTP {status} ({detalle})")
        try:
            pd = float(cuerpo["pd"])
        except (KeyError, TypeError, ValueError):
            return NoDisponible("respuesta fuera de contrato: falta la pd")
        if not 0 <= pd <= 1:
            return NoDisponible(f"respuesta fuera de contrato: pd {pd} fuera de [0, 1]")
        if (cuerpo.get("modelo"), cuerpo.get("version")) != (pedido["modelo"], pedido["version"]):
            respondido = f"{cuerpo.get('modelo')}@{cuerpo.get('version')}"
            return NoDisponible(f"respondió {respondido} y se pidió {pedido['modelo']}@{pedido['version']}")
        periodo_datos = cuerpo.get("periodo_datos")
        if not isinstance(periodo_datos, int) or periodo_datos > pedido["periodo"]:
            return NoDisponible(f"usa datos de {periodo_datos}, posteriores al período pedido ({pedido['periodo']})")
        return Disponible(
            {
                "pd": pd,
                "version": f"{pedido['modelo']}@{pedido['version']}",
                "periodo_datos": periodo_datos,
                "factores": list(cuerpo.get("factores") or [])[:10],
            }
        )


# --- metadata y control de los CUIT reservados --------------------------------------


class ModeloSinConexion(RuntimeError):
    pass


def metadata(cliente: httpx.Client, conexion: ConexionModelo, timeout: float = 5) -> list[dict[str, Any]]:
    """Los modelos que publica el servicio. Lanza ``ModeloSinConexion`` si no se puede leer."""
    if not conexion.url:
        raise ModeloSinConexion(NO_CONFIGURADO)
    try:
        respuesta = cliente.get(f"{conexion.url.rstrip('/')}/v1/modelos", headers=conexion.headers(), timeout=timeout)
        respuesta.raise_for_status()
        modelos = respuesta.json()["modelos"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        raise ModeloSinConexion(f"no se pudo leer la metadata del modelo: {type(error).__name__}: {error}") from None
    return [m for m in modelos if isinstance(m, dict)]


def verificar(modelos: Sequence[Mapping[str, Any]], modelo: str, version: str) -> list[str]:
    """Los problemas de una versión según la metadata del servicio. Vacía si está todo bien."""
    publicada = next((m for m in modelos if (m.get("modelo"), m.get("version")) == (modelo, version)), None)
    if publicada is None:
        return [f"el servicio no publica {modelo}@{version}"]
    exclusion = publicada.get("exclusion_reservados")
    if not isinstance(exclusion, Mapping):
        return [f"{modelo}@{version} no declara que excluyó los CUIT reservados para la demo (ADR 0009)"]
    if (exclusion.get("regla"), exclusion.get("porcentaje")) != (REGLA, PORCENTAJE_RESERVADO):
        return [
            f"{modelo}@{version} excluyó los CUIT reservados con {exclusion.get('regla')} al "
            f"{exclusion.get('porcentaje')} %, y la demo usa {REGLA} al {PORCENTAJE_RESERVADO} % (ADR 0009)"
        ]
    return []
