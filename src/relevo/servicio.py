"""Casos de uso que usa la app: alta de solicitudes, evaluación, casos precargados y agentes.

Arma la solicitud, llama a ``evaluar`` con un ``ConsultorReal`` cuyo almacén de
snapshots es el repositorio, y guarda el resultado. Los documentos subidos pasan por el
agente documental antes de evaluar; el informe y el chat se piden aparte y nunca tocan
el veredicto.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from relevo import agentes, identificacion
from relevo.clientes_claude import ClienteClaude, SinAgente
from relevo.consultor_real import ConfigFuente, ConsultorReal, adapters_por_defecto, cargar_config_fuentes
from relevo.dominio import Solicitud
from relevo.fuentes import AdapterFuente
from relevo.fuentes.identidad import IdentidadDeclarada
from relevo.motor import evaluar
from relevo.reglas import DIRECTORIO_MODELOS, DIRECTORIO_REGLAS, Reglas, cargar_reglas
from relevo.repositorio import AlmacenSqlite, Repositorio, SolicitudGuardada

RAIZ = Path(__file__).resolve().parents[2]
CASOS_PRECARGADOS = (RAIZ / "config" / "casos_precargados.yaml", RAIZ / "data" / "casos_precargados.local.yaml")
SEGMENTOS = ("pyme", "monotributista")
DOCUMENTOS = RAIZ / "data" / "documentos"
EXTENSIONES_DOCUMENTO = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".md")
MANUAL = "carga manual"

FabricaAdapters = Callable[[SolicitudGuardada], Mapping[str, AdapterFuente]]
ArchivoSubido = tuple[str, bytes]  # nombre original y contenido


class SolicitudInvalida(ValueError):
    pass


class ConsentimientoRequerido(SolicitudInvalida):
    """Los CUIL de personas humanas se consultan solo con consentimiento (Ley 25.326)."""


class Servicio:
    def __init__(
        self,
        repositorio: Repositorio,
        adapters: FabricaAdapters | None = None,
        config_fuentes: Mapping[str, ConfigFuente] | None = None,
        ahora: Callable[[], datetime] = datetime.now,
        dormir: Callable[[float], None] = time.sleep,
        directorio_reglas: Path = DIRECTORIO_REGLAS,
        cliente_claude: ClienteClaude | None = None,
        directorio_documentos: Path = DOCUMENTOS,
    ):
        self.repositorio = repositorio
        self.adapters = adapters or self._adapters_reales
        self.config_fuentes = config_fuentes if config_fuentes is not None else cargar_config_fuentes()
        self.ahora = ahora
        self.dormir = dormir
        self.directorio_reglas = directorio_reglas
        self.cliente_claude = cliente_claude or SinAgente()
        self.directorio_documentos = directorio_documentos
        self._cliente: httpx.Client | None = None
        self._lock = threading.Lock()

    def _adapters_reales(self, guardada: SolicitudGuardada) -> Mapping[str, AdapterFuente]:
        with self._lock:
            if self._cliente is None:
                self._cliente = httpx.Client()
        # Los casos precargados usan la identidad simulada; el alta manual usa la fecha declarada.
        # En producción, RENAPER ocupa el lugar de IdentidadDeclarada con las mismas variables.
        identidad = None
        if guardada.origen == "manual":
            identidad = IdentidadDeclarada(guardada.fecha_nacimiento, hoy=self.ahora_fecha)
        return adapters_por_defecto(self._cliente, documentos=guardada.documentos, identidad=identidad)

    def ahora_fecha(self) -> date:
        return self.ahora().date()

    # --- casos de uso -----------------------------------------------------

    def alta(
        self,
        cuit: str,
        segmento: str,
        monto: float,
        plazo_meses: int,
        documentos: Mapping[str, Any] | None = None,
        consentimiento: bool = False,
        origen: str = "manual",
        id: str | None = None,
        archivos: Sequence[ArchivoSubido] = (),
        campos_manuales: Mapping[str, Any] | None = None,
        fecha_nacimiento: date | str | None = None,
    ) -> str:
        """Da de alta una solicitud.

        ``documentos`` son variables ya armadas (casos precargados). ``archivos`` pasan por
        el agente documental y ``campos_manuales`` los carga el analista; de ambos salen
        las variables de la fuente ``documentos``. Si el agente falla, el error queda
        registrado en el documento y la solicitud se da de alta igual."""
        cuit = identificacion.normalizar(cuit)
        if segmento not in SEGMENTOS:
            raise SolicitudInvalida(f"segmento desconocido: {segmento!r}")
        if not cuit:
            raise SolicitudInvalida("falta el CUIT/CUIL")
        if monto <= 0 or plazo_meses <= 0:
            raise SolicitudInvalida("el monto y el plazo tienen que ser positivos")
        if identificacion.tipo_persona(cuit) == "humana" and not consentimiento:
            raise ConsentimientoRequerido(
                "para consultar el CUIL de una persona humana hace falta su consentimiento (Ley 25.326)"
            )
        archivos = [(n, c) for n, c in archivos if n and c]
        for nombre, _ in archivos:
            if Path(nombre).suffix.lower() not in EXTENSIONES_DOCUMENTO:
                raise SolicitudInvalida(
                    f"tipo de documento no soportado: {nombre} ({', '.join(EXTENSIONES_DOCUMENTO)})"
                )
        nacimiento = _fecha_nacimiento(fecha_nacimiento, self.ahora().date())
        documentos = {k: v for k, v in (documentos or {}).items() if v not in (None, "")} or None
        solicitud = Solicitud(cuit=cuit, segmento=segmento, monto=float(monto), plazo_meses=int(plazo_meses))
        id = self.repositorio.crear_solicitud(
            solicitud,
            documentos,
            origen=origen,
            consentimiento=consentimiento,
            id=id,
            ahora=self.ahora(),
            fecha_nacimiento=nacimiento,
        )
        manual = agentes.documento_manual(campos_manuales or {})
        if manual:
            self.repositorio.guardar_documento(id, MANUAL, None, manual.a_dict())
        for orden, (nombre, contenido) in enumerate(archivos, 1):
            ruta = self.directorio_documentos / id / f"{orden:02d}-{_nombre_seguro(nombre)}"
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_bytes(contenido)
            self.repositorio.guardar_documento(id, Path(nombre).name, str(ruta), None)
        if manual or archivos:
            self.extraer_documentos(id)
        return id

    def extraer_documentos(self, solicitud_id: str) -> None:
        """Pasa por el agente documental los archivos de la solicitud y recalcula sus variables.

        No reevalúa: la evaluación anterior queda como estaba."""
        guardada = self.repositorio.solicitud(solicitud_id)
        extraidos = []
        for doc in self.repositorio.documentos(solicitud_id):
            if doc.ruta:
                try:
                    extraido = agentes.extraer_documento(Path(doc.ruta), self.cliente_claude)
                except agentes.AgenteNoDisponible as error:
                    self.repositorio.actualizar_documento(doc.id, None, str(error))
                    continue
                self.repositorio.actualizar_documento(doc.id, extraido.a_dict(), None)
                extraidos.append(extraido)
            elif doc.extraido:
                extraidos.append(agentes.DocumentoExtraido.desde_dict(doc.extraido))
        variables = agentes.variables_documentos(
            extraidos, guardada.solicitud.segmento, guardada.solicitud.cuit, self.ahora().date()
        )
        self.repositorio.actualizar_documentos(solicitud_id, variables)

    def borrar(self, solicitud_id: str) -> None:
        """Borra la solicitud, todo lo asociado y sus archivos."""
        self.repositorio.borrar_solicitud(solicitud_id)
        shutil.rmtree(self.directorio_documentos / solicitud_id, ignore_errors=True)

    # --- agente de informe ------------------------------------------------

    def generar_informe(self, evaluacion_id: int) -> str:
        """Redacta y guarda el informe de riesgo. Lanza ``AgenteNoDisponible`` si el agente falla."""
        evaluacion = self.repositorio.evaluacion(evaluacion_id).evaluacion
        texto = agentes.redactar_informe(evaluacion, self.cliente_claude)
        self.repositorio.guardar_informe(evaluacion_id, texto)
        return texto

    def preguntar(self, evaluacion_id: int, pregunta: str) -> str:
        """Responde una pregunta del analista y guarda los dos mensajes (solo si hubo respuesta)."""
        pregunta = pregunta.strip()
        if not pregunta:
            raise SolicitudInvalida("la pregunta está vacía")
        evaluacion = self.repositorio.evaluacion(evaluacion_id).evaluacion
        historial = [agentes.Turno(m.rol, m.texto) for m in self.repositorio.chat(evaluacion_id)]
        respuesta = agentes.responder(evaluacion, historial, pregunta, self.cliente_claude)
        self.repositorio.agregar_mensaje(evaluacion_id, "analista", pregunta)
        self.repositorio.agregar_mensaje(evaluacion_id, "agente", respuesta)
        return respuesta

    def evaluar(self, solicitud_id: str) -> int:
        guardada = self.repositorio.solicitud(solicitud_id)
        reglas = self.reglas(guardada.solicitud.segmento)
        consultor = ConsultorReal(
            identificacion=guardada.solicitud.cuit,
            adapters=self.adapters(guardada),
            config=self.config_fuentes,
            almacen=AlmacenSqlite(self.repositorio),
            clave_solicitud=solicitud_id,
            precargar=reglas.fuentes,
            ahora=self.ahora,
            dormir=self.dormir,
        )
        evaluacion = evaluar(guardada.solicitud, reglas, consultor)
        return self.repositorio.guardar_evaluacion(solicitud_id, evaluacion, self.ahora())

    def cargar_precargados(self, archivos: Iterable[Path] = CASOS_PRECARGADOS) -> list[str]:
        """Da de alta los casos precargados que todavía no están. No los evalúa (eso consulta en vivo)."""
        nuevos = []
        for archivo in archivos:
            if not archivo.is_file():
                continue
            datos = yaml.safe_load(archivo.read_text(encoding="utf-8")) or {}
            for segmento, casos in datos.items():
                for caso in casos or []:
                    id = f"pre-{segmento}-{identificacion.normalizar(str(caso['cuit']))}"
                    if self.repositorio.existe_solicitud(id):
                        continue
                    # los archivos de casos declaran que hay consentimiento de las personas incluidas
                    self.alta(
                        str(caso["cuit"]),
                        segmento,
                        caso["monto"],
                        caso["plazo_meses"],
                        caso.get("documentos"),
                        consentimiento=True,
                        origen="precargado",
                        id=id,
                    )
                    nuevos.append(id)
        return nuevos

    # --- reglas -----------------------------------------------------------

    def reglas(self, segmento: str, version: int | None = None) -> Reglas:
        return cargar_reglas(segmento, version, directorio_reglas=self.directorio_reglas)

    def texto_reglas(self, segmento: str) -> tuple[Reglas, str, dict[str, Any] | None]:
        """Reglas vigentes, su YAML tal cual y las métricas del modelo del scorecard."""
        reglas = self.reglas(segmento)
        texto = (self.directorio_reglas / f"{segmento}.v{reglas.version}.yaml").read_text(encoding="utf-8")
        modelo = json.loads((DIRECTORIO_MODELOS / reglas.scorecard.modelo).read_text(encoding="utf-8"))
        return reglas, texto, modelo.get("metricas")


def _fecha_nacimiento(valor: date | str | None, hoy: date) -> date | None:
    if valor in (None, ""):
        return None
    try:
        fecha = valor if isinstance(valor, date) else date.fromisoformat(str(valor))
    except ValueError:
        raise SolicitudInvalida(f"fecha de nacimiento inválida: {valor!r} (formato AAAA-MM-DD)") from None
    if not date(1900, 1, 1) <= fecha < hoy:
        raise SolicitudInvalida(f"fecha de nacimiento fuera de rango: {fecha.isoformat()}")
    return fecha


def _nombre_seguro(nombre: str) -> str:
    base = re.sub(r"[^\w.\-]+", "_", Path(nombre).name).strip("._") or "documento"
    return base[-100:]
