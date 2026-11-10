"""App web: Bandeja, Caso y Reglas.

Es delgada a propósito: arma la solicitud, llama al servicio (que llama a ``evaluar``),
guarda y muestra. Correr con:

    .venv/bin/uvicorn relevo.app:crear_app --factory --reload
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from relevo import presentacion
from relevo.clientes_claude import AgenteNoDisponible, cliente_desde_entorno
from relevo.presentacion import a_json, cuit_con_guiones, fecha, numero, pesos, porcentaje, tipo_resultado
from relevo.repositorio import EvaluacionGuardada, Repositorio, SolicitudInexistente
from relevo.servicio import EXTENSIONES_DOCUMENTO, RAIZ, SEGMENTOS, Servicio, SolicitudInvalida

PLANTILLAS = Jinja2Templates(directory=Path(__file__).parent / "plantillas")


PLANTILLAS.env.filters.update(
    pesos=pesos,
    porcentaje=porcentaje,
    numero=numero,
    fecha=fecha,
    tipo_resultado=tipo_resultado,
    a_json=a_json,
    cuit=cuit_con_guiones,
)


# --- app --------------------------------------------------------------------


def crear_app(servicio: Servicio | None = None) -> FastAPI:
    if servicio is None:
        repositorio = Repositorio(os.environ.get("RELEVO_DB", RAIZ / "data" / "relevo.sqlite"))
        servicio = Servicio(repositorio, cliente_claude=cliente_desde_entorno())
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
            extensiones=",".join(EXTENSIONES_DOCUMENTO),
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
        fecha_nacimiento: Annotated[str | None, Form()] = None,
        archivos: Annotated[list[UploadFile] | None, File()] = None,
    ):
        if segmento == "pyme":
            manuales: dict[str, Any] = {"ventas_anuales": ventas_anuales}
        else:
            manuales = {
                "categoria_monotributo": categoria_monotributo,
                "antiguedad_actividad_anios": antiguedad_actividad_anios,
            }
        subidos = [(a.filename or "", a.file.read()) for a in archivos or [] if a.filename]
        try:
            id = servicio.alta(
                cuit,
                segmento,
                monto,
                plazo_meses,
                consentimiento=consentimiento,
                archivos=subidos,
                campos_manuales=manuales,
                fecha_nacimiento=fecha_nacimiento if segmento == "monotributista" else None,
            )
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
    def ver_caso(request: Request, id: str, evaluacion: int | None = None, aviso: str | None = None):
        try:
            guardada = repo.solicitud(id)
        except SolicitudInexistente:
            raise HTTPException(404, "no existe la solicitud") from None
        historial = repo.evaluaciones(id)
        actual = next((e for e in historial if e.id == evaluacion), historial[0] if historial else None)
        if evaluacion is not None and (actual is None or actual.id != evaluacion):
            raise HTTPException(404, "esa evaluación no es de esta solicitud")
        documentos = repo.documentos(id)
        s = guardada.solicitud
        ficha = presentacion.ficha_solicitante(
            s.cuit,
            s.segmento,
            s.monto,
            s.plazo_meses,
            actual.evaluacion if actual else None,
            [d.extraido for d in documentos if d.extraido],
        )
        informe = repo.informe(actual.id) if actual else None
        return pagina(
            request,
            "caso.html",
            ficha=ficha,
            flujo=presentacion.flujo(actual.evaluacion, _reglas_de(servicio, actual.evaluacion)) if actual else [],
            secciones=presentacion.secciones_informe(informe) if informe else [],
            guardada=guardada,
            actual=actual,
            historial=historial,
            es_ultima=bool(historial) and actual is historial[0],
            resolucion=repo.resolucion(actual.id) if actual else None,
            informe=informe,
            chat=repo.chat(actual.id) if actual else [],
            documentos=documentos,
            aviso=aviso,
        )

    @app.post("/solicitudes/{id}/evaluar")
    def reevaluar(id: str):
        _existe(repo, id)
        servicio.evaluar(id)
        return RedirectResponse(f"/solicitudes/{id}", status_code=303)

    @app.post("/solicitudes/{id}/borrar")
    def borrar(id: str):
        servicio.borrar(id)
        return RedirectResponse("/", status_code=303)

    @app.post("/solicitudes/{id}/documentos/extraer")
    def reextraer(id: str):
        _existe(repo, id)
        servicio.extraer_documentos(id)
        aviso = "Se volvieron a extraer los documentos. Reevaluá para que el motor use los campos nuevos."
        return _al_caso(id, aviso=aviso, ancla="documentos")

    @app.post("/solicitudes/{id}/informe")
    def informe(id: str, evaluacion_id: Annotated[int, Form()]):
        _evaluacion_de(repo, id, evaluacion_id)
        try:
            servicio.generar_informe(evaluacion_id)
        except AgenteNoDisponible as error:
            return _al_caso(id, evaluacion_id, aviso=f"No se pudo generar el informe: {error}", ancla="informe")
        return _al_caso(id, evaluacion_id, ancla="informe")

    @app.post("/solicitudes/{id}/chat")
    def preguntar(id: str, evaluacion_id: Annotated[int, Form()], pregunta: Annotated[str, Form()]):
        _evaluacion_de(repo, id, evaluacion_id)
        try:
            servicio.preguntar(evaluacion_id, pregunta)
        except (AgenteNoDisponible, SolicitudInvalida) as error:
            return _al_caso(id, evaluacion_id, aviso=f"No se pudo responder: {error}", ancla="chat")
        return _al_caso(id, evaluacion_id, ancla="chat")

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


def _reglas_de(servicio: Servicio, evaluacion) -> Any:
    """Las reglas con las que se hizo la evaluación, para mostrar las descripciones. Si ya no están, None."""
    try:
        segmento, version = evaluacion.version_reglas.rsplit(".v", 1)
        return servicio.reglas(segmento, int(version))
    except (ValueError, OSError, LookupError):
        return None


def _existe(repo: Repositorio, id: str) -> None:
    if not repo.existe_solicitud(id):
        raise HTTPException(404, "no existe la solicitud")


def _evaluacion_de(repo: Repositorio, id: str, evaluacion_id: int) -> EvaluacionGuardada:
    try:
        guardada = repo.evaluacion(evaluacion_id)
    except LookupError:
        guardada = None
    if guardada is None or guardada.solicitud_id != id:
        raise HTTPException(404, "esa evaluación no es de esta solicitud")
    return guardada


def _al_caso(id: str, evaluacion_id: int | None = None, aviso: str | None = None, ancla: str = "") -> RedirectResponse:
    parametros = {k: v for k, v in {"evaluacion": evaluacion_id, "aviso": aviso}.items() if v is not None}
    consulta = f"?{urlencode(parametros)}" if parametros else ""
    return RedirectResponse(f"/solicitudes/{id}{consulta}" + (f"#{ancla}" if ancla else ""), status_code=303)
