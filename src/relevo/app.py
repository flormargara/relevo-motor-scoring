"""La app: compone la API del motor (``api``) y las pantallas (``paginas``) sobre el mismo servicio.

Las dos son clientes del servicio y comparten la base. ``/docs`` muestra solo la API.
Correr con:

    .venv/bin/uvicorn relevo.app:crear_app --factory --reload
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from relevo.api import crear_api
from relevo.clientes_claude import cliente_desde_entorno
from relevo.paginas import crear_paginas
from relevo.repositorio import Repositorio
from relevo.servicio import RAIZ, Servicio


def crear_app(servicio: Servicio | None = None) -> FastAPI:
    if servicio is None:
        repositorio = Repositorio(os.environ.get("RELEVO_DB", RAIZ / "data" / "relevo.sqlite"))
        servicio = Servicio(repositorio, cliente_claude=cliente_desde_entorno())
        servicio.cargar_precargados()
    app = FastAPI(
        title="Relevo AI — API del motor de decisión de riesgo",
        version="1",
        description="Evalúa solicitudes de crédito y devuelve veredicto, rating, PD, oferta y traza. "
        "Las pantallas de la app no forman parte de este contrato.",
    )
    app.include_router(crear_api(servicio))
    app.include_router(crear_paginas(servicio))
    return app
