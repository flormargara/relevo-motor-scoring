"""Modelo de ML: la PD de un modelo que vive en otro repositorio (ADR 0010).

Cumple ``docs/contrato-modelo-ml.md``: ``POST /v1/pd`` con CUIT, segmento, período,
modelo y versión, una vez por cada modelo que usan las reglas (el que decide, los del
split y los de sombra). Cada pedido queda en el crudo junto con su respuesta, así
``traducir`` (pura) puede validar que la respuesta corresponde al pedido y que el modelo no
usó datos posteriores al período.

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
    """Una consulta por evaluación que trae la PD de todos los modelos de ML que usan las reglas: el
    que decide, los del split y los de sombra (ADR 0010). Cada respuesta queda en el snapshot.

    Las variables del catálogo (``modelo_ml.pd`` y las demás) son las del modelo que decide; el
    motor las busca en ``modelos`` según a qué modelo le tocó la solicitud."""

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
        modelos: Sequence[tuple[str, str]],
        segmento: str,
        periodo: int,
    ):
        self.cliente = cliente
        self.conexion = conexion
        self.pedidos = [
            {"segmento": segmento, "periodo": periodo, "modelo": modelo, "version": version}
            for modelo, version in dict.fromkeys(modelos)
        ]

    def consultar(self, identificacion: str, timeout: float) -> dict[str, Any]:
        """Pide la PD a cada modelo. Si ninguno respondió (timeout, red, 5xx), la fuente está caída y se
        reintenta; si respondió alguno, la falla de los otros queda anotada en su respuesta."""
        if not self.conexion.url:
            raise FuenteCaida(NO_CONFIGURADO)
        respuestas = [self._pedir({"cuit": identificacion, **pedido}, timeout) for pedido in self.pedidos]
        if respuestas and all("error" in r for r in respuestas):
            raise FuenteCaida("; ".join(sorted({r["error"] for r in respuestas})))
        return {"respuestas": respuestas}

    def _pedir(self, pedido: dict[str, Any], timeout: float) -> dict[str, Any]:
        url = f"{self.conexion.url.rstrip('/')}/v1/pd"  # type: ignore[union-attr]
        try:
            respuesta = self.cliente.post(url, json=pedido, headers=self.conexion.headers(), timeout=timeout)
        except httpx.TimeoutException:
            return {"pedido": pedido, "error": f"timeout ({timeout:g} s)"}
        except httpx.HTTPError as error:
            return {"pedido": pedido, "error": f"error de red: {type(error).__name__}"}
        if respuesta.status_code >= 500:
            return {"pedido": pedido, "error": f"HTTP {respuesta.status_code}"}
        try:
            cuerpo = respuesta.json()
        except ValueError:
            cuerpo = {"texto": respuesta.text[:500]}
        return {"pedido": pedido, "status": respuesta.status_code, "body": cuerpo}

    def traducir(self, crudo: Mapping[str, Any]) -> ResultadoFuente:
        modelos = {}
        for respuesta in crudo["respuestas"]:
            pedido = respuesta["pedido"]
            modelos[f"{pedido['modelo']}@{pedido['version']}"] = _traducir_una(respuesta)
        estados = {m["estado"] for m in modelos.values()}
        if estados == {"no_disponible"}:
            return NoDisponible("; ".join(f"{id}: {m['motivo']}" for id, m in modelos.items()))
        if estados == {"sin_antecedentes"}:
            return SinAntecedentes()
        return Disponible({"modelos": modelos})


def _traducir_una(respuesta: Mapping[str, Any]) -> dict[str, Any]:
    """La respuesta de un modelo, validada contra el contrato."""
    pedido = respuesta["pedido"]

    def no_disponible(motivo: str) -> dict[str, Any]:
        return {"estado": "no_disponible", "motivo": motivo}

    if "error" in respuesta:
        return no_disponible(respuesta["error"])
    status, cuerpo = respuesta["status"], respuesta["body"]
    if status == 404:
        return {"estado": "sin_antecedentes"}
    if status != 200:
        detalle = cuerpo.get("detail") or cuerpo.get("error") or cuerpo if isinstance(cuerpo, dict) else cuerpo
        return no_disponible(f"el modelo rechazó el pedido: HTTP {status} ({detalle})")
    try:
        pd = float(cuerpo["pd"])
    except (KeyError, TypeError, ValueError):
        return no_disponible("respuesta fuera de contrato: falta la pd")
    if not 0 <= pd <= 1:
        return no_disponible(f"respuesta fuera de contrato: pd {pd} fuera de [0, 1]")
    if (cuerpo.get("modelo"), cuerpo.get("version")) != (pedido["modelo"], pedido["version"]):
        respondido = f"{cuerpo.get('modelo')}@{cuerpo.get('version')}"
        return no_disponible(f"respondió {respondido} y se pidió {pedido['modelo']}@{pedido['version']}")
    periodo_datos = cuerpo.get("periodo_datos")
    if not isinstance(periodo_datos, int) or periodo_datos > pedido["periodo"]:
        return no_disponible(f"usa datos de {periodo_datos}, posteriores al período pedido ({pedido['periodo']})")
    return {
        "estado": "disponible",
        "pd": pd,
        "version": f"{pedido['modelo']}@{pedido['version']}",
        "periodo_datos": periodo_datos,
        "factores": list(cuerpo.get("factores") or [])[:10],
    }


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
