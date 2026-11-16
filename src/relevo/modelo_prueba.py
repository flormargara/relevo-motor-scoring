"""Servidor de prueba del contrato del modelo de ML (``docs/contrato-modelo-ml.md``).

    python -m relevo.modelo_prueba --puerto 8100
    RELEVO_MODELO_URL=http://localhost:8100 .venv/bin/uvicorn relevo.app:crear_app --factory

**La PD es falsa.** Sale de un hash del CUIT, el período y la versión, alrededor de una
tasa base por segmento: no mira ningún dato del solicitante. Sirve para probar la
integración (fuente, snapshot, rating, sombra, split) sin el repositorio del modelo, nunca
para medir nada. Uno de cada 20 CUIT no tiene datos (404), y los factores SHAP también
son inventados.

No es parte de la app: nadie lo importa salvo los tests.
"""

from __future__ import annotations

import argparse
import hashlib
import math
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from relevo.reservados import PORCENTAJE_RESERVADO, REGLA

TASA_BASE = {"pyme": 0.035, "monotributista": 0.06}
MODELOS = [
    {"modelo": "pyme-prueba", "version": "1", "segmento": "pyme"},
    {"modelo": "pyme-prueba", "version": "2", "segmento": "pyme"},
    {"modelo": "monotributista-prueba", "version": "1", "segmento": "monotributista"},
]
VARIABLES = ("peor_situacion_12m", "deuda_total", "meses_con_deuda", "cantidad_entidades", "antiguedad_deuda_meses")


class PedidoPD(BaseModel):
    cuit: str
    segmento: str
    periodo: int
    modelo: str
    version: str


def _azar(*partes: Any) -> float:
    """Un número en [0, 1) determinístico para esas partes."""
    return int(hashlib.sha256(":".join(map(str, partes)).encode()).hexdigest()[:12], 16) / 16**12


def pd_falsa(cuit: str, segmento: str, periodo: int, version: str) -> float:
    # lognormal de media 1 alrededor de la tasa base; la versión mueve un poco la PD de cada CUIT
    u = min(max(_azar(cuit, periodo, "pd"), 1e-9), 1 - 1e-9)
    z = math.sqrt(2) * _erfinv(2 * u - 1) + 0.3 * (_azar(cuit, version) - 0.5)
    return round(min(0.95, TASA_BASE.get(segmento, 0.05) * math.exp(0.9 * z - 0.405)), 6)


def _erfinv(y: float) -> float:
    a = 0.147
    ln = math.log(1 - y * y)
    t = 2 / (math.pi * a) + ln / 2
    return math.copysign(math.sqrt(math.sqrt(t * t - ln / a) - t), y)


def crear_app(modelos: list[dict[str, Any]] | None = None, exclusion: dict[str, Any] | None = None) -> FastAPI:
    modelos = modelos if modelos is not None else MODELOS
    exclusion = exclusion if exclusion is not None else {"regla": REGLA, "porcentaje": PORCENTAJE_RESERVADO}
    app = FastAPI(title="Modelo de ML de prueba (PD falsa)", version="1")

    @app.get("/v1/modelos")
    def ver_modelos():
        return {
            "modelos": [
                {
                    **m,
                    "entrenado_en": "2026-11-01",
                    "periodos_entrenamiento": [202401, 202512],
                    **({"exclusion_reservados": exclusion} if exclusion else {}),
                    "metricas": {"auc": 0.5, "nota": "PD falsa: no mide nada"},
                }
                for m in modelos
            ]
        }

    @app.post("/v1/pd")
    def pd(pedido: PedidoPD):
        if not any((m["modelo"], m["version"]) == (pedido.modelo, pedido.version) for m in modelos):
            raise HTTPException(400, f"no sirvo {pedido.modelo}@{pedido.version}")
        if _azar(pedido.cuit, "sin-datos") < 0.05:
            raise HTTPException(404, "sin datos del CUIT hasta ese período")
        valor = pd_falsa(pedido.cuit, pedido.segmento, pedido.periodo, pedido.version)
        factores = sorted(
            (
                {
                    "variable": v,
                    "valor": round(_azar(pedido.cuit, v) * 10, 1),
                    "aporte": round(_azar(pedido.cuit, v, "a") - 0.5, 3),
                }
                for v in VARIABLES
            ),
            key=lambda f: -abs(f["aporte"]),
        )
        return {
            "pd": valor,
            "modelo": pedido.modelo,
            "version": pedido.version,
            "periodo_datos": pedido.periodo,
            "factores": factores,
        }

    return app


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - levanta un servidor
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--puerto", type=int, default=8100)
    args = parser.parse_args(argv)
    print("Modelo de ML de PRUEBA: la PD es falsa, sirve solo para probar la integración.")
    uvicorn.run(crear_app(), host="127.0.0.1", port=args.puerto)


if __name__ == "__main__":  # pragma: no cover
    main()
