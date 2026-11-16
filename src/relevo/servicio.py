"""Casos de uso que usa la app: alta de solicitudes, evaluación, casos precargados y agentes.

Arma la solicitud, llama a ``evaluar`` con un ``ConsultorReal`` cuyo almacén de
snapshots es el repositorio, y guarda el resultado. Los documentos subidos pasan por el
agente documental antes de evaluar; el informe y el chat se piden aparte y nunca tocan
el veredicto. El monitoreo cruza las evaluaciones otorgadas con la cartera de la entidad
(``FuenteDeCartera``), que se lee en cada consulta. El servicio es también la fuente de
datos de "Chat with your data" (``chat_datos.DatosDelChat``): monitoreo, simulador y
evaluaciones, siempre de solo lectura.
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

from relevo import chat, chat_datos, identificacion, lectura_documentos, monitoreo, simulador
from relevo.cartera import CarteraInvalida, FuenteDeCartera
from relevo.catalogo import catalogo, flujo_de_originacion, flujos
from relevo.clientes_claude import ClienteClaude, SinAgente
from relevo.consultor_real import ConfigFuente, ConsultorReal, adapters_por_defecto, cargar_config_fuentes
from relevo.dominio import Solicitud
from relevo.fuentes import AdapterFuente, modelo_ml
from relevo.fuentes.documentos import variables_documentos
from relevo.fuentes.identidad import IdentidadDeclarada
from relevo.fuentes.modelo_ml import ConexionModelo, ModeloML, periodo_de
from relevo.motor import evaluar
from relevo.reglas import DIRECTORIO_MODELOS, DIRECTORIO_REGLAS, Reglas, ReglasInvalidas
from relevo.repositorio import AlmacenSqlite, EvaluacionGuardada, Repositorio, SolicitudGuardada
from relevo.versiones_reglas import VersionesReglas

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


class BorradorExistente(ValueError):
    """Ya hay un borrador sin publicar y no se pidió reemplazarlo."""


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
        cartera: FuenteDeCartera | None = None,
        conexion_modelo: ConexionModelo | None = None,
    ):
        self.repositorio = repositorio
        self.adapters = adapters or self._adapters_reales
        self.config_fuentes = config_fuentes if config_fuentes is not None else cargar_config_fuentes()
        self.ahora = ahora
        self.dormir = dormir
        self.directorio_reglas = directorio_reglas
        # Las reglas viven en la base; los YAML son la semilla y solo se traen las versiones nuevas (ADR 0007).
        self.versiones = VersionesReglas(repositorio)
        self.versiones.importar(directorio_reglas)
        self.cliente_claude = cliente_claude or SinAgente()
        self.directorio_documentos = directorio_documentos
        self.cartera = cartera
        self.conexion_modelo = conexion_modelo or ConexionModelo(None)
        self._cliente: httpx.Client | None = None
        self._lock = threading.Lock()

    def _http(self) -> httpx.Client:
        with self._lock:
            if self._cliente is None:
                self._cliente = httpx.Client()
            return self._cliente

    def _adapters_reales(self, guardada: SolicitudGuardada) -> Mapping[str, AdapterFuente]:
        self._http()
        # Los casos precargados usan la identidad simulada; el alta manual y la de la API usan la
        # fecha declarada. En producción, RENAPER ocupa el lugar de IdentidadDeclarada con las mismas variables.
        identidad = None
        if guardada.origen != "precargado":
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
        manual = lectura_documentos.documento_manual(campos_manuales or {})
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
                    extraido = lectura_documentos.extraer_documento(Path(doc.ruta), self.cliente_claude)
                except lectura_documentos.AgenteNoDisponible as error:
                    self.repositorio.actualizar_documento(doc.id, None, str(error))
                    continue
                self.repositorio.actualizar_documento(doc.id, extraido.a_dict(), None)
                extraidos.append(extraido)
            elif doc.extraido:
                extraidos.append(lectura_documentos.DocumentoExtraido.desde_dict(doc.extraido))
        variables = variables_documentos(
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
        texto = chat.redactar_informe(evaluacion, self.cliente_claude)
        self.repositorio.guardar_informe(evaluacion_id, texto)
        return texto

    def preguntar(self, evaluacion_id: int, pregunta: str) -> str:
        """Responde una pregunta del analista y guarda los dos mensajes (solo si hubo respuesta)."""
        pregunta = pregunta.strip()
        if not pregunta:
            raise SolicitudInvalida("la pregunta está vacía")
        evaluacion = self.repositorio.evaluacion(evaluacion_id).evaluacion
        historial = [chat.Turno(m.rol, m.texto) for m in self.repositorio.chat(evaluacion_id)]
        respuesta = chat.responder(evaluacion, historial, pregunta, self.cliente_claude)
        self.repositorio.agregar_mensaje(evaluacion_id, "analista", pregunta)
        self.repositorio.agregar_mensaje(evaluacion_id, "agente", respuesta)
        return respuesta

    # --- monitoreo de cartera (chat) ---------------------------------------

    def monitoreo(self) -> monitoreo.Informe:
        """PD esperada contra default observado. Lanza ``CarteraInvalida`` si no hay cartera o no se puede leer."""
        if self.cartera is None:
            raise CarteraInvalida("no hay cartera configurada")
        return monitoreo.calcular(self.repositorio.otorgadas(), self.cartera.registros())

    # --- Chat with your data (chat_datos.DatosDelChat) ----------------------

    def preguntar_datos(self, historial: Sequence[Mapping[str, Any]], pregunta: str) -> chat_datos.Respuesta:
        """Responde una pregunta sobre la base. ``historial`` son los turnos previos (rol, texto y
        el pedido que resolvió cada respuesta): la conversación no se guarda, vive en la pantalla."""
        pregunta = pregunta.strip()
        if not pregunta:
            raise SolicitudInvalida("la pregunta está vacía")
        turnos = [
            chat_datos.Turno(str(t["rol"]), str(t["texto"]), t.get("plan") if isinstance(t.get("plan"), dict) else None)
            for t in historial
        ]
        return chat_datos.responder(pregunta, turnos, self, self.cliente_claude)

    def flujos(self) -> dict[str, str]:
        return {id: flujo.nombre for id, flujo in flujos().items()}

    def evaluaciones(self) -> list[EvaluacionGuardada]:
        return self.repositorio.ultimas_evaluaciones()

    def contexto_reglas(self, flujo: str) -> str:
        reglas = self.versiones.reglas(flujo)
        contenido = yaml.safe_dump(self.versiones.contenido(flujo), allow_unicode=True, sort_keys=False)
        variables = "\n".join(f"- {v.nombre} ({v.tipo}): {v.descripcion}" for v in catalogo(flujo))
        borrador = "hay un borrador sin publicar" if self.versiones.borrador(flujo) else "no hay borrador"
        return (
            f"Flujo {flujo} ({flujos()[flujo].nombre}), reglas vigentes {reglas.id} ({borrador}):\n{contenido}\n"
            f"Catálogo de variables de {flujo}:\n{variables}"
        )

    def simular(
        self, flujo: str, origen: str, operaciones: Sequence[Mapping], version: int | None = None
    ) -> simulador.Simulacion:
        """El motor con reglas candidatas sobre las evaluaciones guardadas del flujo, sin consultar fuentes."""
        candidata, cambios = self.versiones.candidata(
            flujo, self._contenido_candidato(flujo, origen, operaciones, version)
        )
        if not cambios:
            raise chat_datos.SinCambios("las reglas candidatas son iguales a las vigentes")
        try:
            registros = self.cartera.registros() if self.cartera else []
        except CarteraInvalida:
            registros = []  # sin cartera se simula igual, sin default observado
        return simulador.simular(
            flujo,
            self.repositorio.ultimas_evaluaciones(flujos()[flujo].segmento),
            self.versiones.reglas(flujo),
            candidata,
            cambios,
            self.repositorio.otorgadas(),
            registros,
        )

    def guardar_borrador_simulado(self, flujo: str, operaciones: Sequence[Mapping], reemplazar: bool = False) -> None:
        """Guarda como borrador del editor las reglas vigentes con los cambios simulados. No publica."""
        contenido = simulador.aplicar_cambios(self.versiones.contenido(flujo), operaciones)
        self.versiones.candidata(flujo, contenido)  # valida antes de guardar
        if self.versiones.borrador(flujo) is not None and not reemplazar:
            raise BorradorExistente(f"ya hay un borrador de {flujos()[flujo].nombre} sin publicar")
        self.versiones.guardar_borrador(flujo, contenido, self.versiones.reglas(flujo).version)

    def _contenido_candidato(
        self, flujo: str, origen: str, operaciones: Sequence[Mapping], version: int | None
    ) -> dict[str, Any]:
        if flujo not in flujos():
            raise ReglasInvalidas(f"no hay un flujo {flujo!r}")
        match origen:
            case "cambios":
                return simulador.aplicar_cambios(self.versiones.contenido(flujo), operaciones)
            case "borrador":
                borrador = self.versiones.borrador(flujo)
                if borrador is None:
                    raise ReglasInvalidas(f"no hay un borrador de {flujos()[flujo].nombre}")
                return borrador.contenido
            case "version":
                if version is None:
                    raise ReglasInvalidas("falta la versión a simular")
                return self.versiones.contenido(flujo, version)
        raise ReglasInvalidas(f"origen de reglas desconocido: {origen!r}")

    def evaluar(self, solicitud_id: str) -> int:
        guardada = self.repositorio.solicitud(solicitud_id)
        reglas = self.reglas(flujo_de_originacion(guardada.solicitud.segmento).id)
        adapters = dict(self.adapters(guardada))
        if reglas.scorecard.externo and "modelo_ml" not in adapters:
            adapters["modelo_ml"] = ModeloML(
                self._http(),
                self.conexion_modelo,
                reglas.scorecard.externo.modelo,
                reglas.scorecard.externo.version,
                guardada.solicitud.segmento,
                periodo_de(self.ahora_fecha()),
            )
        consultor = ConsultorReal(
            identificacion=guardada.solicitud.cuit,
            adapters=adapters,
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
        """Da de alta los casos precargados que todavía no están. No los evalúa (eso consulta en vivo).

        En la base de demo no carga nada: ahí solo van CUIT reservados (ADR 0009) y los casos
        precargados no lo son."""
        if self.repositorio.es_base_de_demo():
            return []
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

    def reglas(self, flujo: str, version: int | None = None) -> Reglas:
        return self.versiones.reglas(flujo, version)

    def modelos_disponibles(self, segmento: str) -> list[dict[str, str]]:
        """Los descriptores de ``modelos/`` de un segmento: scorecards de puntos y modelos de ML externos."""
        modelos = []
        for archivo in sorted(DIRECTORIO_MODELOS.glob(f"{segmento}[.-]*.json")):
            descriptor = json.loads(archivo.read_text(encoding="utf-8"))
            modelos.append(
                {
                    "archivo": archivo.name,
                    "tipo": descriptor.get("tipo", "puntos"),
                    "descripcion": str(descriptor.get("descripcion", "")),
                }
            )
        return modelos

    def avisos_modelo(self, flujo: str) -> list[str]:
        """Problemas de los modelos de ML externos que usan las reglas vigentes o el borrador: que el
        servicio no publique la versión o que no declare la exclusión de los CUIT reservados (ADR 0009)."""
        nombres = [self.reglas(flujo).scorecard.modelo]
        if (borrador := self.versiones.borrador(flujo)) is not None:
            nombres.append(str((borrador.contenido.get("scorecard") or {}).get("modelo", "")))
        externos = {}
        for nombre in nombres:
            archivo = DIRECTORIO_MODELOS / nombre
            if nombre and archivo.is_file():
                descriptor = json.loads(archivo.read_text(encoding="utf-8"))
                if descriptor.get("tipo") == "externo":
                    externos[(descriptor.get("modelo"), str(descriptor.get("version")))] = nombre
        if not externos:
            return []
        try:
            publicados = modelo_ml.metadata(self._http(), self.conexion_modelo)
        except modelo_ml.ModeloSinConexion as error:
            return [str(error)]
        return [aviso for modelo, version in externos for aviso in modelo_ml.verificar(publicados, modelo, version)]

    def texto_reglas(self, flujo: str) -> tuple[Reglas, str, dict[str, Any] | None]:
        """Reglas vigentes, su YAML tal cual y las métricas del modelo del scorecard."""
        reglas = self.reglas(flujo)
        texto = self.repositorio.version_reglas(flujo, reglas.version).contenido
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
