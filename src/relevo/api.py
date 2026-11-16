"""API del motor: el contrato JSON que se integra desde otro sistema (ADR 0008).

Entra una solicitud y sale la evaluación: veredicto, rating, PD, oferta, traza y versión
de reglas. Los esquemas de este módulo son el contrato; FastAPI los publica en
``/openapi.json`` y los documenta en ``/docs``. Va versionada en la ruta (``/api/v1``):
un cambio incompatible es una ``v2``, no una edición de la ``v1``.

Sin autenticación todavía (ADR 0007 y 0008): sirve para uso local y pruebas, no para
exponer a terceros.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Literal

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from relevo.catalogo import catalogo, flujos
from relevo.consultor import Disponible, NoDisponible, SinAntecedentes, Snapshot
from relevo.repositorio import EvaluacionGuardada, SolicitudInexistente
from relevo.servicio import Servicio, SolicitudInvalida

PREFIJO = "/api/v1"

# --- esquemas: entrada -------------------------------------------------------


class DocumentosDeclarados(BaseModel):
    """Datos que en la pantalla vienen de los documentos o de la carga manual.

    Por API se declaran directamente (confianza 1). Cada flujo usa los suyos: PyME las
    ventas; monotributista la categoría y la antigüedad."""

    ventas_anuales: float | None = Field(None, ge=0, description="Ventas anuales en pesos")
    categoria_monotributo: str | None = Field(None, description="Letra de la A a la K")
    antiguedad_actividad_anios: float | None = Field(None, ge=0, description="Años desde el inicio de actividades")


class SolicitudEntrada(BaseModel):
    cuit: str = Field(description="CUIT o CUIL, con o sin guiones", examples=["30-71234567-1"])
    segmento: Literal["pyme", "monotributista"]
    monto: float = Field(gt=0, description="Monto pedido, en pesos")
    plazo_meses: int = Field(gt=0)
    consentimiento: bool = Field(False, description="Obligatorio para el CUIL de una persona humana (Ley 25.326)")
    fecha_nacimiento: date | None = Field(None, description="Solo monotributistas: la usa la identidad declarada")
    documentos: DocumentosDeclarados | None = None


# --- esquemas: salida --------------------------------------------------------


class SolicitudSalida(BaseModel):
    cuit: str
    segmento: str
    monto: float
    plazo_meses: int


class OfertaSalida(BaseModel):
    monto: float
    plazo_meses: int
    tasa_nominal_anual: float = Field(description="En %")
    cuota: float


class PasoSalida(BaseModel):
    etapa: str
    regla: str
    resultado: str
    detalle: dict[str, Any]


class FuenteSalida(BaseModel):
    fuente: str
    resultado: Literal["disponible", "sin_antecedentes", "no_disponible"]
    variables: dict[str, Any] | None = None
    motivo: str | None = Field(None, description="Solo si no estuvo disponible")
    origen: str = Field(description="consulta | reutilizado | ultimo_guardado")
    consultado_en: datetime
    nota: str | None = None


class EvaluacionSalida(BaseModel):
    id: int
    solicitud_id: str
    creada_en: datetime
    solicitud: SolicitudSalida
    version_reglas: str = Field(examples=["pyme.v3"])
    veredicto: Literal["APROBADO", "DERIVAR", "RECHAZADO"]
    motivos: list[str]
    puntos: float | None
    pd: float | None
    rating: str | None
    oferta: OfertaSalida | None
    traza: list[PasoSalida]
    fuentes: list[FuenteSalida]
    fuentes_no_consultadas: list[str]


class RatingSalida(BaseModel):
    rating: str
    pd_hasta: float


class VariableSalida(BaseModel):
    nombre: str = Field(examples=["cendeu.peor_situacion_24m"])
    tipo: str
    descripcion: str
    fuente: str


class ReglasSalida(BaseModel):
    flujo: str
    nombre: str
    version: int
    id: str = Field(examples=["pyme.v3"])
    contenido: dict[str, Any] = Field(description="Las reglas publicadas, como en el YAML")
    ratings: list[RatingSalida] = Field(description="Del modelo del scorecard, no de las reglas")
    catalogo: list[VariableSalida] = Field(description="Variables que pueden usar las condiciones del flujo")


# --- endpoints ---------------------------------------------------------------


def crear_api(servicio: Servicio) -> APIRouter:
    repo = servicio.repositorio
    router = APIRouter(prefix=PREFIJO, tags=["motor"])

    @router.post("/evaluaciones", status_code=201, response_model=EvaluacionSalida)
    def evaluar_solicitud(entrada: SolicitudEntrada) -> EvaluacionSalida:
        """Da de alta la solicitud y la evalúa. Consulta las fuentes en vivo."""
        documentos = entrada.documentos.model_dump(exclude_none=True) if entrada.documentos else {}
        try:
            solicitud_id = servicio.alta(
                entrada.cuit,
                entrada.segmento,
                entrada.monto,
                entrada.plazo_meses,
                consentimiento=entrada.consentimiento,
                campos_manuales=documentos,
                fecha_nacimiento=entrada.fecha_nacimiento if entrada.segmento == "monotributista" else None,
                origen="api",
            )
        except SolicitudInvalida as error:
            raise HTTPException(422, str(error)) from None
        return _salida(repo.evaluacion(servicio.evaluar(solicitud_id)))

    @router.get("/evaluaciones/{evaluacion_id}", response_model=EvaluacionSalida)
    def ver_evaluacion(evaluacion_id: int) -> EvaluacionSalida:
        try:
            return _salida(repo.evaluacion(evaluacion_id))
        except LookupError:
            raise HTTPException(404, "no existe la evaluación") from None

    @router.get("/solicitudes/{solicitud_id}/evaluaciones", response_model=list[EvaluacionSalida])
    def historial(solicitud_id: str) -> list[EvaluacionSalida]:
        """Todas las evaluaciones de la solicitud, de la más nueva a la más vieja."""
        if not repo.existe_solicitud(solicitud_id):
            raise HTTPException(404, "no existe la solicitud")
        return [_salida(e) for e in repo.evaluaciones(solicitud_id)]

    @router.post("/solicitudes/{solicitud_id}/evaluaciones", status_code=201, response_model=EvaluacionSalida)
    def reevaluar(solicitud_id: str) -> EvaluacionSalida:
        """Vuelve a evaluar una solicitud existente con las reglas vigentes."""
        try:
            evaluacion_id = servicio.evaluar(solicitud_id)
        except SolicitudInexistente:
            raise HTTPException(404, "no existe la solicitud") from None
        return _salida(repo.evaluacion(evaluacion_id))

    @router.get("/reglas/{flujo}", response_model=ReglasSalida)
    def reglas_vigentes(flujo: str) -> ReglasSalida:
        """Las reglas publicadas del flujo, con los ratings del modelo y el catálogo de variables."""
        if flujo not in flujos():
            raise HTTPException(404, f"flujo desconocido; los flujos son: {', '.join(flujos())}")
        reglas, texto, _ = servicio.texto_reglas(flujo)
        return ReglasSalida(
            flujo=flujo,
            nombre=flujos()[flujo].nombre,
            version=reglas.version,
            id=reglas.id,
            contenido=yaml.safe_load(texto),
            ratings=[RatingSalida(rating=r.nombre, pd_hasta=r.pd_hasta) for r in reglas.ratings],
            catalogo=[
                VariableSalida(nombre=v.nombre, tipo=v.tipo, descripcion=v.descripcion, fuente=v.fuente)
                for v in catalogo(flujo)
            ],
        )

    return router


def _salida(guardada: EvaluacionGuardada) -> EvaluacionSalida:
    e = guardada.evaluacion
    return EvaluacionSalida(
        id=guardada.id,
        solicitud_id=guardada.solicitud_id,
        creada_en=guardada.creada_en,
        solicitud=SolicitudSalida(**asdict(e.solicitud)),
        version_reglas=e.version_reglas,
        veredicto=e.veredicto.value,
        motivos=list(e.motivos),
        puntos=e.puntos,
        pd=e.pd,
        rating=e.rating,
        oferta=OfertaSalida(**asdict(e.oferta)) if e.oferta else None,
        traza=[PasoSalida(**asdict(p)) for p in e.traza],
        fuentes=[_fuente(s) for s in e.snapshots],
        fuentes_no_consultadas=list(e.fuentes_no_consultadas),
    )


def _fuente(snapshot: Snapshot) -> FuenteSalida:
    comun = {"fuente": snapshot.fuente, "origen": snapshot.origen, "consultado_en": snapshot.consultado_en}
    comun["nota"] = snapshot.nota
    match snapshot.resultado:
        case Disponible(variables=variables):
            return FuenteSalida(resultado="disponible", variables=dict(variables), **comun)
        case NoDisponible(motivo=motivo):
            return FuenteSalida(resultado="no_disponible", motivo=motivo, **comun)
        case SinAntecedentes():
            return FuenteSalida(resultado="sin_antecedentes", **comun)
    raise TypeError(f"resultado de fuente desconocido: {snapshot.resultado!r}")
