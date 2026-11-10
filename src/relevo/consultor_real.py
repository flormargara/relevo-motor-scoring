"""``ConsultorReal``: el adapter del seam ``Consultor`` que habla con las fuentes de verdad.

Aplica la configuración de cada fuente (``config/fuentes.yaml``):
- las gratuitas que usan las reglas se precargan en paralelo al construirlo;
- las pagas se consultan recién cuando el motor las pide (ADR 0005);
- reintentos y timeout por fuente; un timeout, 5xx o error de red agota los
  reintentos y queda *no disponible* (ADR 0006);
- reutiliza el snapshot de una fuente paga de una evaluación anterior de la misma
  solicitud si es reciente;
- si una fuente marcada ``si_falla_usar_ultimo`` cae (BADLAR), usa el último valor
  guardado y lo marca ``desactualizada``.

Nunca lanza: cualquier falla de un adapter termina en ``NoDisponible``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

from relevo.consultor import Disponible, NoDisponible, ResultadoFuente, Snapshot
from relevo.fuentes import AdapterFuente, FuenteCaida

CONFIG_POR_DEFECTO = Path(__file__).resolve().parents[2] / "config" / "fuentes.yaml"
ALCANCE_GLOBAL = "*"


@dataclass(frozen=True)
class ConfigFuente:
    costo: str = "gratuita"
    timeout: float = 10.0
    reintentos: int = 1
    espera: float = 1.0
    reutilizar_dias: int = 0
    alcance: str = "solicitud"
    si_falla_usar_ultimo: bool = False

    @property
    def paga(self) -> bool:
        return self.costo == "paga"


def cargar_config_fuentes(ruta: Path = CONFIG_POR_DEFECTO) -> dict[str, ConfigFuente]:
    datos = yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
    config = {}
    for fuente, valores in datos.items():
        try:
            config[fuente] = ConfigFuente(**(valores or {}))
        except TypeError as error:
            raise ValueError(f"{ruta.name}: configuración inválida para {fuente!r}: {error}") from None
        if config[fuente].costo not in ("gratuita", "paga") or config[fuente].alcance not in ("solicitud", "global"):
            raise ValueError(f"{ruta.name}: costo o alcance inválido para {fuente!r}")
    return config


class AlmacenSnapshots(Protocol):
    """Dónde quedan los snapshots entre evaluaciones. En el paso 4 lo implementa el repositorio."""

    def ultimo(self, fuente: str, clave: str) -> Snapshot | None: ...

    def guardar(self, clave: str, snapshot: Snapshot) -> None: ...


class AlmacenEnMemoria:
    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, str], Snapshot] = {}
        self._lock = threading.Lock()

    def ultimo(self, fuente: str, clave: str) -> Snapshot | None:
        with self._lock:
            return self._snapshots.get((fuente, clave))

    def guardar(self, clave: str, snapshot: Snapshot) -> None:
        with self._lock:
            self._snapshots[(snapshot.fuente, clave)] = snapshot


class ConsultorReal:
    def __init__(
        self,
        identificacion: str,
        adapters: Mapping[str, AdapterFuente],
        config: Mapping[str, ConfigFuente],
        almacen: AlmacenSnapshots,
        clave_solicitud: str,
        precargar: Iterable[str] = (),
        ahora: Callable[[], datetime] = datetime.now,
        dormir: Callable[[float], None] = time.sleep,
    ):
        self.identificacion = identificacion
        self.adapters = adapters
        self.config = config
        self.almacen = almacen
        self.clave_solicitud = clave_solicitud
        self.ahora = ahora
        self.dormir = dormir
        self._futuros: dict[str, Future[Snapshot]] = {}
        self._lock = threading.Lock()
        gratuitas = [f for f in dict.fromkeys(precargar) if not self._config(f).paga]
        self._pool = ThreadPoolExecutor(max_workers=max(1, len(gratuitas)), thread_name_prefix="fuente")
        for fuente in gratuitas:
            self._futuros[fuente] = self._pool.submit(self._consultar, fuente)

    def variables(self, fuente: str) -> ResultadoFuente:
        with self._lock:
            futuro = self._futuros.get(fuente)
            if futuro is None:
                futuro = self._futuros[fuente] = Future()
                propio = True
            else:
                propio = False
        if propio:
            futuro.set_result(self._consultar(fuente))
        return futuro.result().resultado

    def snapshots(self) -> list[Snapshot]:
        with self._lock:
            futuros = list(self._futuros.values())
        resultado = [futuro.result() for futuro in futuros]
        self._pool.shutdown(wait=True)
        return resultado

    # --- una fuente -------------------------------------------------------

    def _config(self, fuente: str) -> ConfigFuente:
        return self.config.get(fuente) or ConfigFuente()

    def _consultar(self, fuente: str) -> Snapshot:
        try:
            return self._consultar_sin_proteger(fuente)
        except Exception as error:  # noqa: BLE001 — el consultor nunca lanza
            motivo = f"error inesperado: {type(error).__name__}: {error}"
            return Snapshot(fuente, self.ahora(), {"error": motivo}, NoDisponible(motivo))

    def _consultar_sin_proteger(self, fuente: str) -> Snapshot:
        config = self._config(fuente)
        adapter = self.adapters.get(fuente)
        ahora = self.ahora()
        if adapter is None:
            return Snapshot(fuente, ahora, None, NoDisponible("fuente no configurada"), intentos=0)
        clave = ALCANCE_GLOBAL if config.alcance == "global" else self.clave_solicitud

        if config.reutilizar_dias:
            previo = self.almacen.ultimo(fuente, clave)
            if previo and ahora - previo.consultado_en <= timedelta(days=config.reutilizar_dias):
                return replace(
                    previo, origen="reutilizado", intentos=0, nota=f"snapshot del {previo.consultado_en:%Y-%m-%d %H:%M}"
                )

        intentos, motivo = 0, ""
        while intentos <= config.reintentos:
            intentos += 1
            try:
                crudo = adapter.consultar(self.identificacion, config.timeout)
            except FuenteCaida as error:
                motivo = str(error)
                if intentos <= config.reintentos:
                    self.dormir(config.espera)
                continue
            try:
                resultado = adapter.traducir(crudo)
            except Exception as error:  # noqa: BLE001
                resultado = NoDisponible(f"no se pudo traducir la respuesta: {type(error).__name__}: {error}")
            snapshot = Snapshot(fuente, ahora, crudo, resultado, intentos=intentos)
            if not isinstance(resultado, NoDisponible):
                self.almacen.guardar(clave, snapshot)
            return snapshot

        motivo = f"{motivo} (después de {intentos} intento{'s' if intentos > 1 else ''})"
        if config.si_falla_usar_ultimo:
            previo = self.almacen.ultimo(fuente, clave)
            if previo and isinstance(previo.resultado, Disponible):
                variables = {**previo.resultado.variables, "desactualizada": True}
                return Snapshot(
                    fuente,
                    ahora,
                    previo.crudo,
                    Disponible(variables),
                    origen="ultimo_guardado",
                    intentos=intentos,
                    nota=f"{motivo}; valor del {previo.consultado_en:%Y-%m-%d %H:%M}",
                )
        return Snapshot(fuente, ahora, {"error": motivo}, NoDisponible(motivo), intentos=intentos)


# --- armado -----------------------------------------------------------------


def adapters_por_defecto(
    cliente: httpx.Client,
    documentos: Mapping[str, Any] | None = None,
    juguete: Mapping[str, Any] | None = None,
    igj: Path | None = None,
    hoy: Callable[[], date] = date.today,
    arca: Path | None = None,
    identidad: AdapterFuente | None = None,
) -> dict[str, AdapterFuente]:
    from relevo.fuentes.arca import SNAPSHOT_POR_DEFECTO as SNAPSHOT_ARCA
    from relevo.fuentes.arca import Arca
    from relevo.fuentes.badlar import Badlar
    from relevo.fuentes.cendeu import Cendeu, Cheques
    from relevo.fuentes.documentos import Documentos
    from relevo.fuentes.igj import SNAPSHOT_POR_DEFECTO, Igj
    from relevo.fuentes.juguete import Bureau, Identidad, Propios, cargar_juguete

    datos_juguete = juguete if juguete is not None else cargar_juguete()
    adapters: list[AdapterFuente] = [
        Cendeu(cliente),
        Cheques(cliente),
        Badlar(cliente, hoy=hoy),
        Igj(igj or SNAPSHOT_POR_DEFECTO, hoy=hoy),
        Arca(arca or SNAPSHOT_ARCA),
        Bureau(datos_juguete),
        identidad or Identidad(datos_juguete, hoy=hoy),
        Propios(datos_juguete),
        Documentos(documentos),
    ]
    return {adapter.nombre: adapter for adapter in adapters}
