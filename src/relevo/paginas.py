"""Las pantallas: Bandeja, Caso y Reglas (HTML con Jinja).

Son delgadas a propósito: arman la solicitud, llaman al servicio (que llama a ``evaluar``),
guardan y muestran. Quedan fuera del esquema OpenAPI: el contrato del motor es ``api``.
Los endpoints JSON del editor de reglas (validar, borrador, publicar) son internos de
la pantalla Reglas, no parte de ese contrato.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

import yaml
from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from relevo import presentacion
from relevo.catalogo import Flujo, catalogo, flujos, validar_condicion
from relevo.clientes_claude import AgenteNoDisponible
from relevo.diff_reglas import diferencias
from relevo.presentacion import a_json, cuit_con_guiones, fecha, numero, pesos, porcentaje, tipo_resultado
from relevo.reglas import ReglasInvalidas
from relevo.repositorio import EvaluacionGuardada, Repositorio, SolicitudInexistente
from relevo.servicio import EXTENSIONES_DOCUMENTO, SEGMENTOS, Servicio, SolicitudInvalida
from relevo.versiones_reglas import ConflictoDeVersion

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


def crear_paginas(servicio: Servicio) -> APIRouter:
    repo = servicio.repositorio
    router = APIRouter(include_in_schema=False)

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

    @router.get("/", response_class=HTMLResponse)
    def ver_bandeja(request: Request):
        return bandeja(request)

    @router.post("/solicitudes")
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

    @router.post("/evaluar-pendientes")
    def evaluar_pendientes():
        for fila in repo.bandeja():
            if fila.veredicto is None:
                servicio.evaluar(fila.solicitud.id)
        return RedirectResponse("/", status_code=303)

    @router.get("/solicitudes/{id}", response_class=HTMLResponse)
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

    @router.post("/solicitudes/{id}/evaluar")
    def reevaluar(id: str):
        _existe(repo, id)
        servicio.evaluar(id)
        return RedirectResponse(f"/solicitudes/{id}", status_code=303)

    @router.post("/solicitudes/{id}/borrar")
    def borrar(id: str):
        servicio.borrar(id)
        return RedirectResponse("/", status_code=303)

    @router.post("/solicitudes/{id}/documentos/extraer")
    def reextraer(id: str):
        _existe(repo, id)
        servicio.extraer_documentos(id)
        aviso = "Se volvieron a extraer los documentos. Reevaluá para que el motor use los campos nuevos."
        return _al_caso(id, aviso=aviso, ancla="documentos")

    @router.post("/solicitudes/{id}/informe")
    def informe(id: str, evaluacion_id: Annotated[int, Form()]):
        _evaluacion_de(repo, id, evaluacion_id)
        try:
            servicio.generar_informe(evaluacion_id)
        except AgenteNoDisponible as error:
            return _al_caso(id, evaluacion_id, aviso=f"No se pudo generar el informe: {error}", ancla="informe")
        return _al_caso(id, evaluacion_id, ancla="informe")

    @router.post("/solicitudes/{id}/chat")
    def preguntar(id: str, evaluacion_id: Annotated[int, Form()], pregunta: Annotated[str, Form()]):
        _evaluacion_de(repo, id, evaluacion_id)
        try:
            servicio.preguntar(evaluacion_id, pregunta)
        except (AgenteNoDisponible, SolicitudInvalida) as error:
            return _al_caso(id, evaluacion_id, aviso=f"No se pudo responder: {error}", ancla="chat")
        return _al_caso(id, evaluacion_id, ancla="chat")

    @router.post("/solicitudes/{id}/resolucion")
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

    # --- reglas: editor por flujo (ADR 0007) -----------------------------

    def _flujo(flujo: str) -> Flujo:
        if flujo not in flujos():
            raise HTTPException(404, "flujo desconocido")
        return flujos()[flujo]

    @router.get("/reglas", response_class=HTMLResponse)
    def ver_reglas(request: Request, flujo: str = "pyme"):
        definicion = _flujo(flujo)
        reglas, texto, metricas = servicio.texto_reglas(flujo)
        ratings = [{"rating": r.nombre, "pd_hasta": r.pd_hasta} for r in reglas.ratings]
        borrador = servicio.versiones.borrador(flujo)
        datos = {
            "flujo": flujo,
            "nombre": definicion.nombre,
            "version": reglas.version,
            "vigente": yaml.safe_load(texto) | {"ratings": ratings},
            "borrador": (borrador.contenido | {"ratings": ratings}) if borrador else None,
            "base_borrador": borrador.base if borrador else None,
            "metricas": metricas,
            "catalogo": [
                {"nombre": v.nombre, "tipo": v.tipo, "descripcion": v.descripcion, "fuente": v.fuente}
                for v in catalogo(flujo)
            ],
        }
        return pagina(
            request,
            "reglas.html",
            flujos=flujos().values(),
            flujo=definicion,
            reglas=reglas,
            metricas=metricas,
            borrador=borrador,
            datos=datos,
        )

    @router.get("/reglas/{flujo}/historial", response_class=HTMLResponse)
    def historial_reglas(request: Request, flujo: str):
        definicion = _flujo(flujo)
        versiones = repo.versiones_reglas(flujo)
        filas = []
        for guardada in versiones:
            anterior = guardada.version - 1
            cambios = None
            if any(v.version == anterior for v in versiones):
                cambios = diferencias(servicio.reglas(flujo, anterior), servicio.reglas(flujo, guardada.version))
            filas.append((guardada, cambios))
        return pagina(request, "historial_reglas.html", flujos=flujos().values(), flujo=definicion, filas=filas)

    @router.post("/reglas/validar")
    def validar_regla(cuerpo: Annotated[dict[str, Any], Body()]):
        flujo = _flujo(str(cuerpo.get("flujo", ""))).id
        validacion = validar_condicion(flujo, str(cuerpo.get("condicion", "")))
        return {"ok": validacion.ok, "error": validacion.error, "usa": list(validacion.usa)}

    @router.put("/reglas/{flujo}/borrador", status_code=204)
    def guardar_borrador(flujo: str, cuerpo: Annotated[dict[str, Any], Body()]):
        _flujo(flujo)
        servicio.versiones.guardar_borrador(flujo, _contenido(cuerpo), int(cuerpo["base"]))

    @router.delete("/reglas/{flujo}/borrador", status_code=204)
    def descartar_borrador(flujo: str):
        _flujo(flujo)
        servicio.versiones.descartar_borrador(flujo)

    @router.post("/reglas/{flujo}/borrador/actualizar-base")
    def actualizar_base_del_borrador(flujo: str):
        _flujo(flujo)
        servicio.versiones.actualizar_base_del_borrador(flujo)
        return RedirectResponse(f"/reglas?flujo={flujo}", status_code=303)

    @router.post("/reglas/{flujo}/borrador/desde/{version}")
    def cargar_version_en_el_borrador(flujo: str, version: int):
        _flujo(flujo)
        try:
            servicio.versiones.cargar_version_en_el_borrador(flujo, version)
        except ReglasInvalidas:
            raise HTTPException(404, "no existe esa versión") from None
        return RedirectResponse(f"/reglas?flujo={flujo}", status_code=303)

    @router.post("/reglas/{flujo}/cambios")
    def cambios_del_borrador(flujo: str, cuerpo: Annotated[dict[str, Any], Body()]):
        _flujo(flujo)
        try:
            cambios = servicio.versiones.cambios(flujo, _contenido(cuerpo))
        except ReglasInvalidas as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        return {"cambios": [asdict(c) for c in cambios]}

    @router.post("/reglas/{flujo}/publicar")
    def publicar_reglas(flujo: str, cuerpo: Annotated[dict[str, Any], Body()]):
        _flujo(flujo)
        try:
            reglas = servicio.versiones.publicar(
                flujo,
                _contenido(cuerpo),
                str(cuerpo.get("autor", "")),
                str(cuerpo.get("motivo", "")),
                base=int(cuerpo["base"]),
            )
        except ConflictoDeVersion as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        except ReglasInvalidas as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        return {"version": reglas.id}

    return router


def _contenido(cuerpo: dict[str, Any]) -> dict[str, Any]:
    """Las reglas que manda el editor, sin lo que es solo de la pantalla (los ratings son del modelo)."""
    contenido = cuerpo.get("contenido")
    if not isinstance(contenido, dict):
        raise HTTPException(422, "falta el contenido de las reglas")
    return {k: v for k, v in contenido.items() if k != "ratings"}


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
