"""App web: Bandeja, Caso y Reglas.

Es delgada a propósito: arma la solicitud, llama al servicio (que llama a ``evaluar``),
guarda y muestra. Correr con:

    .venv/bin/uvicorn relevo.app:crear_app --factory --reload
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from relevo.consultor import Disponible, NoDisponible, SinAntecedentes
from relevo.repositorio import Repositorio, SolicitudInexistente
from relevo.servicio import RAIZ, SEGMENTOS, Servicio, SolicitudInvalida

PLANTILLAS = Jinja2Templates(directory=Path(__file__).parent / "plantillas")


# --- formato ----------------------------------------------------------------


def _miles(numero: float, decimales: int = 0) -> str:
    texto = f"{numero:,.{decimales}f}"
    return texto.replace(",", "§").replace(".", ",").replace("§", ".")


def pesos(valor: Any, decimales: int = 0) -> str:
    return "—" if valor is None else f"$ {_miles(float(valor), decimales)}"


def porcentaje(valor: Any, decimales: int = 2) -> str:
    return "—" if valor is None else f"{_miles(float(valor) * 100, decimales)} %"


def numero(valor: Any) -> str:
    if isinstance(valor, bool) or valor is None:
        return {True: "sí", False: "no", None: "—"}[valor]
    if isinstance(valor, float):
        return _miles(valor, 0 if valor.is_integer() else (2 if abs(valor) >= 1 else 4))
    if isinstance(valor, int):
        return _miles(valor)
    return str(valor)


def fecha(valor: Any) -> str:
    return valor.strftime("%d/%m/%Y %H:%M") if valor else "—"


def tipo_resultado(resultado: Any) -> str:
    return {Disponible: "disponible", SinAntecedentes: "sin antecedentes", NoDisponible: "no disponible"}[
        type(resultado)
    ]


def a_json(valor: Any) -> str:
    return json.dumps(valor, ensure_ascii=False, indent=2, default=str)


PLANTILLAS.env.filters.update(
    pesos=pesos, porcentaje=porcentaje, numero=numero, fecha=fecha, tipo_resultado=tipo_resultado, a_json=a_json
)


# --- app --------------------------------------------------------------------


def crear_app(servicio: Servicio | None = None) -> FastAPI:
    if servicio is None:
        repositorio = Repositorio(os.environ.get("RELEVO_DB", RAIZ / "data" / "relevo.sqlite"))
        servicio = Servicio(repositorio)
        servicio.cargar_precargados()
    repo = servicio.repositorio
    app = FastAPI(title="Relevo AI — Motor de decisión de riesgo")

    def pagina(request: Request, plantilla: str, status_code: int = 200, **contexto: Any) -> HTMLResponse:
        return PLANTILLAS.TemplateResponse(request, plantilla, contexto, status_code=status_code)

    def bandeja(request: Request, error: str | None = None, formulario: dict | None = None) -> HTMLResponse:
        return pagina(
            request,
            "bandeja.html",
            status_code=400 if error else 200,
            filas=repo.bandeja(),
            segmentos=SEGMENTOS,
            error=error,
            formulario=formulario or {},
        )

    @app.get("/", response_class=HTMLResponse)
    def ver_bandeja(request: Request):
        return bandeja(request)

    @app.post("/solicitudes")
    def alta(
        request: Request,
        cuit: Annotated[str, Form()],
        segmento: Annotated[str, Form()],
        monto: Annotated[float, Form()],
        plazo_meses: Annotated[int, Form()],
        consentimiento: Annotated[bool, Form()] = False,
        ventas_anuales: Annotated[float | None, Form()] = None,
        categoria_monotributo: Annotated[str | None, Form()] = None,
        antiguedad_actividad_anios: Annotated[float | None, Form()] = None,
    ):
        if segmento == "pyme":
            documentos = {"ventas_anuales": ventas_anuales}
        else:
            documentos = {
                "categoria_monotributo": (categoria_monotributo or "").strip().upper() or None,
                "antiguedad_actividad_anios": antiguedad_actividad_anios,
            }
        if any(v is not None for v in documentos.values()):
            documentos["confianza_minima"] = 1.0  # cargados a mano por el analista
        try:
            id = servicio.alta(cuit, segmento, monto, plazo_meses, documentos, consentimiento=consentimiento)
        except SolicitudInvalida as error:
            formulario = {"cuit": cuit, "segmento": segmento, "monto": monto, "plazo_meses": plazo_meses}
            return bandeja(request, error=str(error), formulario=formulario)
        servicio.evaluar(id)
        return RedirectResponse(f"/solicitudes/{id}", status_code=303)

    @app.post("/evaluar-pendientes")
    def evaluar_pendientes():
        for fila in repo.bandeja():
            if fila.veredicto is None:
                servicio.evaluar(fila.solicitud.id)
        return RedirectResponse("/", status_code=303)

    @app.get("/solicitudes/{id}", response_class=HTMLResponse)
    def ver_caso(request: Request, id: str, evaluacion: int | None = None):
        try:
            guardada = repo.solicitud(id)
        except SolicitudInexistente:
            raise HTTPException(404, "no existe la solicitud") from None
        historial = repo.evaluaciones(id)
        actual = next((e for e in historial if e.id == evaluacion), historial[0] if historial else None)
        if evaluacion is not None and (actual is None or actual.id != evaluacion):
            raise HTTPException(404, "esa evaluación no es de esta solicitud")
        return pagina(
            request,
            "caso.html",
            guardada=guardada,
            actual=actual,
            historial=historial,
            es_ultima=bool(historial) and actual is historial[0],
            resolucion=repo.resolucion(actual.id) if actual else None,
            informe=repo.informe(actual.id) if actual else None,
        )

    @app.post("/solicitudes/{id}/evaluar")
    def reevaluar(id: str):
        _existe(repo, id)
        servicio.evaluar(id)
        return RedirectResponse(f"/solicitudes/{id}", status_code=303)

    @app.post("/solicitudes/{id}/borrar")
    def borrar(id: str):
        repo.borrar_solicitud(id)
        return RedirectResponse("/", status_code=303)

    @app.post("/solicitudes/{id}/resolucion")
    def resolver(
        id: str,
        evaluacion_id: Annotated[int, Form()],
        decision: Annotated[str, Form()],
        motivo: Annotated[str, Form()],
        analista: Annotated[str, Form()],
    ):
        ultima = repo.ultima_evaluacion(id)
        if ultima is None or ultima.id != evaluacion_id or ultima.evaluacion.veredicto.value != "DERIVAR":
            raise HTTPException(400, "solo se resuelve la última evaluación, y solo si fue derivada")
        try:
            repo.guardar_resolucion(evaluacion_id, decision, motivo.strip(), analista.strip())
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        return RedirectResponse(f"/solicitudes/{id}", status_code=303)

    @app.get("/reglas", response_class=HTMLResponse)
    def ver_reglas(request: Request, segmento: str = "pyme"):
        if segmento not in SEGMENTOS:
            raise HTTPException(404, "segmento desconocido")
        reglas, texto, metricas = servicio.texto_reglas(segmento)
        return pagina(
            request,
            "reglas.html",
            segmentos=SEGMENTOS,
            segmento=segmento,
            reglas=reglas,
            texto=texto,
            metricas=metricas,
        )

    return app


def _existe(repo: Repositorio, id: str) -> None:
    if not repo.existe_solicitud(id):
        raise HTTPException(404, "no existe la solicitud")
