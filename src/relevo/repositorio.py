"""Persistencia local en SQLite.

Guarda solicitudes y evaluaciones completas (snapshots, traza, versión de reglas y
veredicto), más informes, chat y resoluciones del analista. Una evaluación es
inmutable: reevaluar agrega una nueva, y vale la última.

También implementa ``AlmacenSnapshots`` para el ``ConsultorReal``: los snapshots se
persisten junto con su evaluación, y ``ultimo`` los busca ahí.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from relevo.consultor import Disponible, NoDisponible, ResultadoFuente, SinAntecedentes, Snapshot
from relevo.dominio import Evaluacion, Oferta, Paso, Solicitud, Veredicto

ESQUEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS solicitudes (
    id TEXT PRIMARY KEY,
    cuit TEXT NOT NULL,
    segmento TEXT NOT NULL,
    monto REAL NOT NULL,
    plazo_meses INTEGER NOT NULL,
    documentos TEXT,             -- JSON con los campos extraídos, o NULL
    origen TEXT NOT NULL,        -- precargado | manual | api | demo
    consentimiento INTEGER NOT NULL DEFAULT 0,
    creada_en TEXT NOT NULL,
    fecha_nacimiento TEXT        -- ISO, declarada en el alta, o NULL
);

CREATE TABLE IF NOT EXISTS evaluaciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    solicitud_id TEXT NOT NULL REFERENCES solicitudes(id) ON DELETE CASCADE,
    creada_en TEXT NOT NULL,
    version_reglas TEXT NOT NULL,
    veredicto TEXT NOT NULL,
    motivos TEXT NOT NULL,
    puntos REAL,
    pd REAL,
    rating TEXT,
    oferta TEXT,
    traza TEXT NOT NULL,
    fuentes_no_consultadas TEXT NOT NULL,
    denominacion TEXT
);
-- la última evaluación de cada solicitud (histórico, monitoreo)
CREATE INDEX IF NOT EXISTS evaluaciones_por_solicitud ON evaluaciones (solicitud_id, id);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluacion_id INTEGER NOT NULL REFERENCES evaluaciones(id) ON DELETE CASCADE,
    solicitud_id TEXT NOT NULL,
    orden INTEGER NOT NULL,
    fuente TEXT NOT NULL,
    consultado_en TEXT NOT NULL,
    crudo TEXT,
    resultado TEXT NOT NULL,
    tipo_resultado TEXT NOT NULL,
    origen TEXT NOT NULL,
    intentos INTEGER NOT NULL,
    nota TEXT
);
CREATE INDEX IF NOT EXISTS snapshots_por_fuente ON snapshots (fuente, solicitud_id, consultado_en);

CREATE TABLE IF NOT EXISTS documentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    solicitud_id TEXT NOT NULL REFERENCES solicitudes(id) ON DELETE CASCADE,
    archivo TEXT NOT NULL,       -- nombre original, o "carga manual"
    ruta TEXT,                   -- dónde quedó guardado (NULL para la carga manual)
    extraido TEXT,               -- JSON del DocumentoExtraido, o NULL si falló
    error TEXT,                  -- por qué no se pudo extraer
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS informes (
    evaluacion_id INTEGER PRIMARY KEY REFERENCES evaluaciones(id) ON DELETE CASCADE,
    texto TEXT NOT NULL,
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluacion_id INTEGER NOT NULL REFERENCES evaluaciones(id) ON DELETE CASCADE,
    rol TEXT NOT NULL,           -- analista | agente
    texto TEXT NOT NULL,
    creado_en TEXT NOT NULL
);

-- Versiones publicadas de las reglas de cada flujo (ADR 0007). Son inmutables:
-- publicar agrega una fila; la vigente es la de mayor versión.
CREATE TABLE IF NOT EXISTS versiones_reglas (
    flujo TEXT NOT NULL,
    version INTEGER NOT NULL,
    contenido TEXT NOT NULL,     -- YAML de las reglas
    autor TEXT NOT NULL,
    motivo TEXT NOT NULL,
    base INTEGER,                -- versión de la que partió, o NULL (semilla)
    publicada_en TEXT NOT NULL,
    PRIMARY KEY (flujo, version)
);

-- El borrador en edición de cada flujo: uno solo y compartido (ADR 0007).
CREATE TABLE IF NOT EXISTS borradores_reglas (
    flujo TEXT PRIMARY KEY,
    contenido TEXT NOT NULL,     -- JSON de las reglas, todavía sin validar
    base INTEGER NOT NULL,       -- versión vigente de la que parte
    actualizado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resoluciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluacion_id INTEGER NOT NULL REFERENCES evaluaciones(id) ON DELETE CASCADE,
    decision TEXT NOT NULL,      -- aprobar | rechazar
    motivo TEXT NOT NULL,
    analista TEXT NOT NULL,
    creada_en TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class SolicitudGuardada:
    id: str
    solicitud: Solicitud
    documentos: dict[str, Any] | None
    origen: str
    consentimiento: bool
    creada_en: datetime
    fecha_nacimiento: date | None = None  # declarada en el alta


@dataclass(frozen=True)
class EvaluacionGuardada:
    id: int
    solicitud_id: str
    creada_en: datetime
    evaluacion: Evaluacion
    denominacion: str | None


@dataclass(frozen=True)
class FilaBandeja:
    solicitud: SolicitudGuardada
    evaluacion_id: int | None
    veredicto: Veredicto | None
    rating: str | None
    evaluada_en: datetime | None
    denominacion: str | None


@dataclass(frozen=True)
class Otorgada:
    """Una solicitud cuya última evaluación terminó en un crédito ofrecido, con lo que el monitoreo
    compara contra la cartera."""

    solicitud_id: str
    segmento: str
    evaluada_en: datetime
    version_reglas: str
    rating: str
    pd: float


@dataclass(frozen=True)
class Resolucion:
    decision: str
    motivo: str
    analista: str
    creada_en: datetime


@dataclass(frozen=True)
class Mensaje:
    rol: str
    texto: str
    creado_en: datetime


@dataclass(frozen=True)
class DocumentoGuardado:
    id: int
    archivo: str
    ruta: str | None
    extraido: dict[str, Any] | None
    error: str | None
    creado_en: datetime


@dataclass(frozen=True)
class VersionReglasGuardada:
    flujo: str
    version: int
    contenido: str
    autor: str
    motivo: str
    base: int | None
    publicada_en: datetime


@dataclass(frozen=True)
class BorradorReglas:
    flujo: str
    contenido: dict[str, Any]
    base: int
    actualizado_en: datetime


class SolicitudInexistente(LookupError):
    pass


class Repositorio:
    def __init__(self, ruta: Path | str = ":memory:"):
        if ruta != ":memory:":
            Path(ruta).parent.mkdir(parents=True, exist_ok=True)
        self._conexion = sqlite3.connect(str(ruta), check_same_thread=False)
        self._conexion.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conexion.executescript(ESQUEMA)
            columnas = {fila[1] for fila in self._conexion.execute("PRAGMA table_info(solicitudes)")}
            if "fecha_nacimiento" not in columnas:  # base creada antes de la fecha declarada
                self._conexion.execute("ALTER TABLE solicitudes ADD COLUMN fecha_nacimiento TEXT")

    def _ejecutar(self, sql: str, parametros: Any = ()) -> sqlite3.Cursor:
        with self._lock, self._conexion:
            return self._conexion.execute(sql, parametros)

    # --- solicitudes ------------------------------------------------------

    def crear_solicitud(
        self,
        solicitud: Solicitud,
        documentos: dict[str, Any] | None = None,
        origen: str = "manual",
        consentimiento: bool = False,
        id: str | None = None,
        ahora: datetime | None = None,
        fecha_nacimiento: date | None = None,
    ) -> str:
        id = id or uuid.uuid4().hex[:12]
        self._ejecutar(
            "INSERT INTO solicitudes (id, cuit, segmento, monto, plazo_meses, documentos, origen, consentimiento, "
            "creada_en, fecha_nacimiento) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                id,
                solicitud.cuit,
                solicitud.segmento,
                solicitud.monto,
                solicitud.plazo_meses,
                _json(documentos) if documentos else None,
                origen,
                int(consentimiento),
                (ahora or datetime.now()).isoformat(),
                fecha_nacimiento.isoformat() if fecha_nacimiento else None,
            ),
        )
        return id

    def existe_solicitud(self, id: str) -> bool:
        return self._ejecutar("SELECT 1 FROM solicitudes WHERE id = ?", (id,)).fetchone() is not None

    def es_base_de_demo(self) -> bool:
        """La armó ``demo_real``: tiene solicitudes de origen ``demo`` (CUIT reservados, ADR 0009)."""
        return self._ejecutar("SELECT 1 FROM solicitudes WHERE origen = 'demo' LIMIT 1").fetchone() is not None

    def solicitud(self, id: str) -> SolicitudGuardada:
        fila = self._ejecutar("SELECT * FROM solicitudes WHERE id = ?", (id,)).fetchone()
        if fila is None:
            raise SolicitudInexistente(id)
        return _solicitud(fila)

    def actualizar_documentos(self, id: str, documentos: dict[str, Any] | None) -> None:
        """Reemplaza las variables de la fuente ``documentos`` de la solicitud."""
        self._ejecutar(
            "UPDATE solicitudes SET documentos = ? WHERE id = ?", (_json(documentos) if documentos else None, id)
        )

    def guardar_documento(
        self,
        solicitud_id: str,
        archivo: str,
        ruta: str | None,
        extraido: dict[str, Any] | None,
        error: str | None = None,
    ) -> int:
        cursor = self._ejecutar(
            "INSERT INTO documentos (solicitud_id, archivo, ruta, extraido, error, creado_en) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (solicitud_id, archivo, ruta, _json(extraido) if extraido else None, error, datetime.now().isoformat()),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def actualizar_documento(self, documento_id: int, extraido: dict[str, Any] | None, error: str | None) -> None:
        self._ejecutar(
            "UPDATE documentos SET extraido = ?, error = ? WHERE id = ?",
            (_json(extraido) if extraido else None, error, documento_id),
        )

    def documentos(self, solicitud_id: str) -> list[DocumentoGuardado]:
        filas = self._ejecutar(
            "SELECT * FROM documentos WHERE solicitud_id = ? ORDER BY id", (solicitud_id,)
        ).fetchall()
        return [
            DocumentoGuardado(
                f["id"],
                f["archivo"],
                f["ruta"],
                json.loads(f["extraido"]) if f["extraido"] else None,
                f["error"],
                datetime.fromisoformat(f["creado_en"]),
            )
            for f in filas
        ]

    def borrar_solicitud(self, id: str) -> None:
        """Borra la solicitud y todo lo asociado (documentos, evaluaciones, snapshots, informes, chat)."""
        self._ejecutar("DELETE FROM solicitudes WHERE id = ?", (id,))

    def bandeja(
        self, buscar: str | None = None, limite: int | None = None, desplazamiento: int = 0
    ) -> list[FilaBandeja]:
        """Las solicitudes con su última evaluación, de la más nueva a la más vieja.

        ``buscar`` filtra por CUIT (con o sin guiones), denominación o id de solicitud."""
        donde, parametros = _filtro_bandeja(buscar)
        paginado = " LIMIT ? OFFSET ?" if limite is not None else ""
        if limite is not None:
            parametros += [limite, desplazamiento]
        filas = self._ejecutar(
            f"""
            SELECT s.*, e.id AS evaluacion_id, e.veredicto, e.rating, e.creada_en AS evaluada_en, e.denominacion
            FROM solicitudes s
            LEFT JOIN evaluaciones e ON e.id = (SELECT MAX(id) FROM evaluaciones WHERE solicitud_id = s.id)
            {donde}
            ORDER BY s.creada_en DESC, s.id{paginado}
            """,
            parametros,
        ).fetchall()
        return [
            FilaBandeja(
                solicitud=_solicitud(f),
                evaluacion_id=f["evaluacion_id"],
                veredicto=Veredicto(f["veredicto"]) if f["veredicto"] else None,
                rating=f["rating"],
                evaluada_en=datetime.fromisoformat(f["evaluada_en"]) if f["evaluada_en"] else None,
                denominacion=f["denominacion"],
            )
            for f in filas
        ]

    def contar_solicitudes(self, buscar: str | None = None) -> int:
        donde, parametros = _filtro_bandeja(buscar)
        sql = (
            "SELECT COUNT(*) FROM solicitudes s LEFT JOIN evaluaciones e "
            f"ON e.id = (SELECT MAX(id) FROM evaluaciones WHERE solicitud_id = s.id) {donde}"
        )
        return self._ejecutar(sql, parametros).fetchone()[0]

    def sin_evaluar(self) -> list[str]:
        """Los ids de las solicitudes que todavía no tienen ninguna evaluación."""
        filas = self._ejecutar(
            "SELECT id FROM solicitudes s WHERE NOT EXISTS (SELECT 1 FROM evaluaciones WHERE solicitud_id = s.id) "
            "ORDER BY creada_en DESC, id"
        ).fetchall()
        return [f["id"] for f in filas]

    # --- evaluaciones -----------------------------------------------------

    def guardar_evaluacion(self, solicitud_id: str, evaluacion: Evaluacion, ahora: datetime | None = None) -> int:
        with self._lock, self._conexion:
            cursor = self._conexion.execute(
                "INSERT INTO evaluaciones (solicitud_id, creada_en, version_reglas, veredicto, motivos, puntos, pd, "
                "rating, oferta, traza, fuentes_no_consultadas, denominacion) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    solicitud_id,
                    (ahora or datetime.now()).isoformat(),
                    evaluacion.version_reglas,
                    evaluacion.veredicto.value,
                    _json(list(evaluacion.motivos)),
                    evaluacion.puntos,
                    evaluacion.pd,
                    evaluacion.rating,
                    _json(asdict(evaluacion.oferta)) if evaluacion.oferta else None,
                    _json([asdict(paso) for paso in evaluacion.traza]),
                    _json(list(evaluacion.fuentes_no_consultadas)),
                    _denominacion(evaluacion.snapshots),
                ),
            )
            evaluacion_id = cursor.lastrowid
            self._conexion.executemany(
                "INSERT INTO snapshots (evaluacion_id, solicitud_id, orden, fuente, consultado_en, crudo, resultado, "
                "tipo_resultado, origen, intentos, nota) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        evaluacion_id,
                        solicitud_id,
                        orden,
                        s.fuente,
                        s.consultado_en.isoformat(),
                        _json(s.crudo),
                        _json(_resultado_a_dict(s.resultado)),
                        _tipo(s.resultado),
                        s.origen,
                        s.intentos,
                        s.nota,
                    )
                    for orden, s in enumerate(evaluacion.snapshots)
                ],
            )
        assert evaluacion_id is not None
        return evaluacion_id

    def evaluacion(self, evaluacion_id: int) -> EvaluacionGuardada:
        fila = self._ejecutar("SELECT * FROM evaluaciones WHERE id = ?", (evaluacion_id,)).fetchone()
        if fila is None:
            raise LookupError(f"no existe la evaluación {evaluacion_id}")
        snapshots = self._ejecutar(
            "SELECT * FROM snapshots WHERE evaluacion_id = ? ORDER BY orden", (evaluacion_id,)
        ).fetchall()
        oferta = json.loads(fila["oferta"]) if fila["oferta"] else None
        evaluacion = Evaluacion(
            solicitud=self.solicitud(fila["solicitud_id"]).solicitud,
            version_reglas=fila["version_reglas"],
            veredicto=Veredicto(fila["veredicto"]),
            motivos=tuple(json.loads(fila["motivos"])),
            traza=tuple(Paso(**paso) for paso in json.loads(fila["traza"])),
            snapshots=tuple(_snapshot(s) for s in snapshots),
            fuentes_no_consultadas=tuple(json.loads(fila["fuentes_no_consultadas"])),
            puntos=fila["puntos"],
            pd=fila["pd"],
            rating=fila["rating"],
            oferta=Oferta(**oferta) if oferta else None,
        )
        return EvaluacionGuardada(
            id=fila["id"],
            solicitud_id=fila["solicitud_id"],
            creada_en=datetime.fromisoformat(fila["creada_en"]),
            evaluacion=evaluacion,
            denominacion=fila["denominacion"],
        )

    def evaluaciones(self, solicitud_id: str) -> list[EvaluacionGuardada]:
        """Todas las evaluaciones de una solicitud, de la más nueva a la más vieja."""
        ids = self._ejecutar(
            "SELECT id FROM evaluaciones WHERE solicitud_id = ? ORDER BY id DESC", (solicitud_id,)
        ).fetchall()
        return [self.evaluacion(f["id"]) for f in ids]

    def otorgadas(self) -> list[Otorgada]:
        """Las solicitudes cuya última evaluación ofreció un crédito: aprobadas por el motor, o derivadas
        y aprobadas por el analista. En una sola consulta, porque el monitoreo las lee todas."""
        filas = self._ejecutar(
            """
            SELECT s.id, s.segmento, e.creada_en, e.version_reglas, e.rating, e.pd
            FROM solicitudes s
            JOIN evaluaciones e ON e.id = (SELECT MAX(id) FROM evaluaciones WHERE solicitud_id = s.id)
            WHERE e.oferta IS NOT NULL AND e.oferta != 'null' AND e.pd IS NOT NULL AND e.rating IS NOT NULL
              AND (e.veredicto = 'APROBADO' OR (e.veredicto = 'DERIVAR' AND (
                  SELECT decision FROM resoluciones WHERE evaluacion_id = e.id ORDER BY id DESC LIMIT 1
              ) = 'aprobar'))
            ORDER BY s.id
            """
        ).fetchall()
        return [
            Otorgada(
                f["id"],
                f["segmento"],
                datetime.fromisoformat(f["creada_en"]),
                f["version_reglas"],
                f["rating"],
                f["pd"],
            )
            for f in filas
        ]

    def ultima_evaluacion(self, solicitud_id: str) -> EvaluacionGuardada | None:
        sql = "SELECT MAX(id) AS id FROM evaluaciones WHERE solicitud_id = ?"
        fila = self._ejecutar(sql, (solicitud_id,)).fetchone()
        return self.evaluacion(fila["id"]) if fila["id"] is not None else None

    # --- informe, chat y resolución (los usan los agentes y el analista) ---

    def guardar_informe(self, evaluacion_id: int, texto: str) -> None:
        self._ejecutar(
            "INSERT OR REPLACE INTO informes VALUES (?, ?, ?)", (evaluacion_id, texto, datetime.now().isoformat())
        )

    def informe(self, evaluacion_id: int) -> str | None:
        fila = self._ejecutar("SELECT texto FROM informes WHERE evaluacion_id = ?", (evaluacion_id,)).fetchone()
        return fila["texto"] if fila else None

    def agregar_mensaje(self, evaluacion_id: int, rol: str, texto: str) -> None:
        self._ejecutar(
            "INSERT INTO chat (evaluacion_id, rol, texto, creado_en) VALUES (?, ?, ?, ?)",
            (evaluacion_id, rol, texto, datetime.now().isoformat()),
        )

    def chat(self, evaluacion_id: int) -> list[Mensaje]:
        filas = self._ejecutar("SELECT * FROM chat WHERE evaluacion_id = ? ORDER BY id", (evaluacion_id,)).fetchall()
        return [Mensaje(f["rol"], f["texto"], datetime.fromisoformat(f["creado_en"])) for f in filas]

    def guardar_resolucion(self, evaluacion_id: int, decision: str, motivo: str, analista: str) -> None:
        if decision not in ("aprobar", "rechazar"):
            raise ValueError(f"decisión inválida: {decision!r}")
        self._ejecutar(
            "INSERT INTO resoluciones (evaluacion_id, decision, motivo, analista, creada_en) VALUES (?, ?, ?, ?, ?)",
            (evaluacion_id, decision, motivo, analista, datetime.now().isoformat()),
        )

    def resolucion(self, evaluacion_id: int) -> Resolucion | None:
        fila = self._ejecutar(
            "SELECT * FROM resoluciones WHERE evaluacion_id = ? ORDER BY id DESC LIMIT 1", (evaluacion_id,)
        ).fetchone()
        if fila is None:
            return None
        return Resolucion(fila["decision"], fila["motivo"], fila["analista"], datetime.fromisoformat(fila["creada_en"]))

    # --- versiones de reglas (ADR 0007) -------------------------------------

    def guardar_version_reglas(
        self,
        flujo: str,
        version: int,
        contenido: str,
        autor: str,
        motivo: str,
        base: int | None = None,
        ahora: datetime | None = None,
    ) -> None:
        self._ejecutar(
            "INSERT INTO versiones_reglas (flujo, version, contenido, autor, motivo, base, publicada_en) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (flujo, version, contenido, autor, motivo, base, (ahora or datetime.now()).isoformat()),
        )

    def guardar_borrador_reglas(
        self, flujo: str, contenido: dict[str, Any], base: int, ahora: datetime | None = None
    ) -> None:
        self._ejecutar(
            "INSERT INTO borradores_reglas (flujo, contenido, base, actualizado_en) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (flujo) DO UPDATE SET contenido = excluded.contenido, base = excluded.base, "
            "actualizado_en = excluded.actualizado_en",
            (flujo, _json(contenido), base, (ahora or datetime.now()).isoformat()),
        )

    def borrador_reglas(self, flujo: str) -> BorradorReglas | None:
        fila = self._ejecutar("SELECT * FROM borradores_reglas WHERE flujo = ?", (flujo,)).fetchone()
        if fila is None:
            return None
        return BorradorReglas(
            fila["flujo"], json.loads(fila["contenido"]), fila["base"], datetime.fromisoformat(fila["actualizado_en"])
        )

    def borrar_borrador_reglas(self, flujo: str) -> None:
        self._ejecutar("DELETE FROM borradores_reglas WHERE flujo = ?", (flujo,))

    def versiones_reglas(self, flujo: str) -> list[VersionReglasGuardada]:
        """Todas las versiones del flujo, de la más nueva a la más vieja."""
        filas = self._ejecutar("SELECT version FROM versiones_reglas WHERE flujo = ? ORDER BY version DESC", (flujo,))
        return [self.version_reglas(flujo, fila["version"]) for fila in filas.fetchall()]

    def version_reglas(self, flujo: str, version: int | None = None) -> VersionReglasGuardada | None:
        """La versión pedida o, si no se pide, la vigente (la de mayor número)."""
        if version is None:
            fila = self._ejecutar(
                "SELECT * FROM versiones_reglas WHERE flujo = ? ORDER BY version DESC LIMIT 1", (flujo,)
            ).fetchone()
        else:
            fila = self._ejecutar(
                "SELECT * FROM versiones_reglas WHERE flujo = ? AND version = ?", (flujo, version)
            ).fetchone()
        if fila is None:
            return None
        return VersionReglasGuardada(
            fila["flujo"],
            fila["version"],
            fila["contenido"],
            fila["autor"],
            fila["motivo"],
            fila["base"],
            datetime.fromisoformat(fila["publicada_en"]),
        )


class AlmacenSqlite:
    """``AlmacenSnapshots`` sobre el repositorio.

    Los snapshots se persisten con su evaluación (``guardar_evaluacion``), así que
    ``guardar`` no hace nada. ``ultimo`` devuelve el snapshot más reciente que vino de
    una consulta real con respuesta: de la misma solicitud, o de cualquiera si la clave
    es global (``*``)."""

    def __init__(self, repositorio: Repositorio):
        self.repositorio = repositorio

    def guardar(self, clave: str, snapshot: Snapshot) -> None:
        pass

    def ultimo(self, fuente: str, clave: str) -> Snapshot | None:
        filtro, parametros = ("", (fuente,)) if clave == "*" else (" AND solicitud_id = ?", (fuente, clave))
        fila = self.repositorio._ejecutar(
            "SELECT * FROM snapshots WHERE fuente = ? AND tipo_resultado != 'no_disponible' "
            f"AND origen IN ('consulta', 'reutilizado'){filtro} ORDER BY consultado_en DESC, id DESC LIMIT 1",
            parametros,
        ).fetchone()
        return _snapshot(fila) if fila else None


# --- serialización ----------------------------------------------------------


def _json(valor: Any) -> str:
    return json.dumps(valor, ensure_ascii=False, default=str)


def _tipo(resultado: ResultadoFuente) -> str:
    return {Disponible: "disponible", SinAntecedentes: "sin_antecedentes", NoDisponible: "no_disponible"}[
        type(resultado)
    ]


def _resultado_a_dict(resultado: ResultadoFuente) -> dict[str, Any]:
    match resultado:
        case Disponible(variables=variables):
            return {"tipo": "disponible", "variables": dict(variables)}
        case NoDisponible(motivo=motivo):
            return {"tipo": "no_disponible", "motivo": motivo}
    return {"tipo": "sin_antecedentes"}


def _resultado_desde_dict(datos: dict[str, Any]) -> ResultadoFuente:
    match datos["tipo"]:
        case "disponible":
            return Disponible(datos["variables"])
        case "no_disponible":
            return NoDisponible(datos["motivo"])
    return SinAntecedentes()


def _snapshot(fila: sqlite3.Row) -> Snapshot:
    return Snapshot(
        fuente=fila["fuente"],
        consultado_en=datetime.fromisoformat(fila["consultado_en"]),
        crudo=json.loads(fila["crudo"]) if fila["crudo"] else None,
        resultado=_resultado_desde_dict(json.loads(fila["resultado"])),
        origen=fila["origen"],
        intentos=fila["intentos"],
        nota=fila["nota"],
    )


def _filtro_bandeja(buscar: str | None) -> tuple[str, list[Any]]:
    """El WHERE de la búsqueda de la bandeja: CUIT (sin guiones), denominación o id."""
    texto = (buscar or "").strip()
    if not texto:
        return "", []
    condiciones = ["e.denominacion LIKE ? COLLATE NOCASE", "s.id LIKE ?"]
    parametros: list[Any] = [f"%{texto}%", f"%{texto}%"]
    if digitos := "".join(c for c in texto if c.isdigit()):
        condiciones.append("s.cuit LIKE ?")
        parametros.append(f"%{digitos}%")
    return "WHERE " + " OR ".join(condiciones), parametros


def _solicitud(fila: sqlite3.Row) -> SolicitudGuardada:
    return SolicitudGuardada(
        id=fila["id"],
        solicitud=Solicitud(fila["cuit"], fila["segmento"], fila["monto"], fila["plazo_meses"]),
        documentos=json.loads(fila["documentos"]) if fila["documentos"] else None,
        origen=fila["origen"],
        consentimiento=bool(fila["consentimiento"]),
        creada_en=datetime.fromisoformat(fila["creada_en"]),
        fecha_nacimiento=date.fromisoformat(fila["fecha_nacimiento"]) if fila["fecha_nacimiento"] else None,
    )


def _denominacion(snapshots: tuple[Snapshot, ...]) -> str | None:
    for fuente, variable in (("cendeu", "denominacion"), ("igj", "razon_social")):
        for snapshot in snapshots:
            if snapshot.fuente == fuente and isinstance(snapshot.resultado, Disponible):
                valor = snapshot.resultado.variables.get(variable)
                if valor:
                    return str(valor)
    return None
