"""Casos de uso que usa la app: alta de solicitudes, evaluación y casos precargados.

Arma la solicitud, llama a ``evaluar`` con un ``ConsultorReal`` cuyo almacén de
snapshots es el repositorio, y guarda el resultado.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from relevo import identificacion
from relevo.consultor_real import ConfigFuente, ConsultorReal, adapters_por_defecto, cargar_config_fuentes
from relevo.dominio import Solicitud
from relevo.fuentes import AdapterFuente
from relevo.motor import evaluar
from relevo.reglas import DIRECTORIO_MODELOS, DIRECTORIO_REGLAS, Reglas, cargar_reglas
from relevo.repositorio import AlmacenSqlite, Repositorio, SolicitudGuardada

RAIZ = Path(__file__).resolve().parents[2]
CASOS_PRECARGADOS = (RAIZ / "config" / "casos_precargados.yaml", RAIZ / "data" / "casos_precargados.local.yaml")
SEGMENTOS = ("pyme", "monotributista")

FabricaAdapters = Callable[[SolicitudGuardada], Mapping[str, AdapterFuente]]


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
    ):
        self.repositorio = repositorio
        self.adapters = adapters or self._adapters_reales
        self.config_fuentes = config_fuentes if config_fuentes is not None else cargar_config_fuentes()
        self.ahora = ahora
        self.dormir = dormir
        self.directorio_reglas = directorio_reglas
        self._cliente: httpx.Client | None = None
        self._lock = threading.Lock()

    def _adapters_reales(self, guardada: SolicitudGuardada) -> Mapping[str, AdapterFuente]:
        with self._lock:
            if self._cliente is None:
                self._cliente = httpx.Client()
        return adapters_por_defecto(self._cliente, documentos=guardada.documentos)

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
    ) -> str:
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
        documentos = {k: v for k, v in (documentos or {}).items() if v not in (None, "")} or None
        solicitud = Solicitud(cuit=cuit, segmento=segmento, monto=float(monto), plazo_meses=int(plazo_meses))
        return self.repositorio.crear_solicitud(
            solicitud, documentos, origen=origen, consentimiento=consentimiento, id=id, ahora=self.ahora()
        )

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
