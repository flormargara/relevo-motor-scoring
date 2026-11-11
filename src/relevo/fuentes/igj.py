"""IGJ (Inspección General de Justicia) desde un snapshot precargado.

El dataset abierto de datos.jus.gob.ar trae las entidades (con CUIT, baja y motivo),
sus balances y sus asambleas. No publica la fecha de constitución, así que la
antigüedad se aproxima con la fecha más vieja registrada en IGJ (primer balance o
primera asamblea): es una cota inferior de la antigüedad real.

IGJ registra solo sociedades con domicilio en CABA. Una sociedad de otra
jurisdicción da *sin antecedentes*, que no es un dato negativo.

Armar el snapshot (una vez, offline):

    python -m relevo.fuentes.igj igj-2026-semestre-2.zip data/igj.sqlite
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import sys
import zipfile
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Any

from relevo.consultor import Disponible, ResultadoFuente, SinAntecedentes
from relevo.fuentes import FuenteCaida, Variable

SNAPSHOT_POR_DEFECTO = Path(__file__).resolve().parents[3] / "data" / "igj.sqlite"


class Igj:
    nombre = "igj"
    variables = {
        "razon_social": Variable("texto", "Razón social registrada en IGJ"),
        "tipo_societario": Variable("texto", "Tipo societario (SA, SRL, SAS...)"),
        "dada_de_baja": Variable("booleano", "La sociedad figura dada de baja"),
        "detalle_baja": Variable("texto", "Detalle de la baja"),
        "fecha_primer_registro": Variable("fecha", "Fecha del primer registro en IGJ"),
        "antiguedad_anios": Variable("número", "Años desde el primer registro en IGJ"),
        "periodo_snapshot": Variable("texto", "Semestre del snapshot de IGJ"),
    }

    def __init__(self, ruta: Path = SNAPSHOT_POR_DEFECTO, hoy: Callable[[], date] = date.today):
        self.ruta = ruta
        self.hoy = hoy

    def consultar(self, identificacion: str, timeout: float) -> dict[str, Any]:
        if not self.ruta.is_file():
            raise FuenteCaida(f"no hay snapshot de IGJ en {self.ruta}")
        with sqlite3.connect(f"file:{self.ruta}?mode=ro", uri=True) as conexion:
            conexion.row_factory = sqlite3.Row
            fila = conexion.execute("SELECT * FROM entidades WHERE cuit = ?", (identificacion,)).fetchone()
            meta = dict(conexion.execute("SELECT clave, valor FROM meta").fetchall())
        return {
            "registro": dict(fila) if fila else None,
            "periodo_snapshot": meta.get("periodo"),
            "fecha_consulta": self.hoy().isoformat(),
        }

    def traducir(self, crudo: dict[str, Any]) -> ResultadoFuente:
        registro = crudo["registro"]
        if registro is None:
            return SinAntecedentes()
        primer_registro = registro["primer_registro"]
        antiguedad = None
        if primer_registro:
            dias = (date.fromisoformat(crudo["fecha_consulta"]) - date.fromisoformat(primer_registro)).days
            antiguedad = round(dias / 365.25, 2)
        return Disponible(
            {
                "razon_social": registro["razon_social"],
                "tipo_societario": registro["tipo_societario"],
                "dada_de_baja": bool(registro["dada_de_baja"]),
                "detalle_baja": registro["detalle_baja"],
                "fecha_primer_registro": primer_registro,
                "antiguedad_anios": antiguedad,
                "periodo_snapshot": crudo["periodo_snapshot"],
            }
        )


# --- armado del snapshot ----------------------------------------------------


def armar_snapshot(zip_igj: Path, destino: Path, hoy: date | None = None) -> int:
    """Lee el ZIP semestral de IGJ y escribe el snapshot SQLite. Devuelve cuántos CUIT quedaron."""
    hoy = hoy or date.today()
    with zipfile.ZipFile(zip_igj) as archivo:
        periodo = _ultimo_periodo(archivo)
        entidades: dict[str, dict[str, Any]] = {}
        for fila in _filas(archivo, f"igj-entidades-{periodo}.csv"):
            cuit = re.sub(r"\D", "", fila.get("cuit") or "")
            if len(cuit) == 11:
                entidades[fila["numero_correlativo"]] = {
                    "cuit": cuit,
                    "razon_social": fila["razon_social"],
                    "tipo_societario": fila["descripcion_tipo_societario"],
                    "dada_de_baja": (fila.get("dada_de_baja") or "").strip().upper() == "S",
                    "detalle_baja": fila.get("detalle_baja") or None,
                    "primer_registro": None,
                }
        for nombre, columna in (("balances", "fecha_balance"), ("asambleas", "fecha_realizacion")):
            for fila in _filas(archivo, f"igj-{nombre}-{periodo}.csv"):
                entidad = entidades.get(fila["numero_correlativo"])
                fecha = _fecha(fila.get(columna), hoy)
                if entidad and fecha and (entidad["primer_registro"] is None or fecha < entidad["primer_registro"]):
                    entidad["primer_registro"] = fecha

    por_cuit: dict[str, dict[str, Any]] = {}
    for entidad in entidades.values():  # un CUIT puede tener varios legajos
        previo = por_cuit.get(entidad["cuit"])
        if previo is None:
            por_cuit[entidad["cuit"]] = dict(entidad)
            continue
        fechas = [f for f in (previo["primer_registro"], entidad["primer_registro"]) if f]
        previo["primer_registro"] = min(fechas) if fechas else None
        if previo["dada_de_baja"] and not entidad["dada_de_baja"]:
            previo.update(
                razon_social=entidad["razon_social"],
                tipo_societario=entidad["tipo_societario"],
                dada_de_baja=False,
                detalle_baja=None,
            )

    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_suffix(".tmp")
    temporal.unlink(missing_ok=True)
    with sqlite3.connect(temporal) as conexion:
        conexion.execute(
            "CREATE TABLE entidades (cuit TEXT PRIMARY KEY, razon_social TEXT, tipo_societario TEXT, "
            "dada_de_baja INTEGER, detalle_baja TEXT, primer_registro TEXT)"
        )
        conexion.execute("CREATE TABLE meta (clave TEXT PRIMARY KEY, valor TEXT)")
        conexion.executemany(
            "INSERT INTO entidades VALUES (:cuit, :razon_social, :tipo_societario, :dada_de_baja, "
            ":detalle_baja, :primer_registro)",
            por_cuit.values(),
        )
        conexion.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [("periodo", periodo), ("origen", zip_igj.name), ("armado", hoy.isoformat())],
        )
    conexion.close()
    temporal.replace(destino)
    return len(por_cuit)


def _ultimo_periodo(archivo: zipfile.ZipFile) -> str:
    periodos = {m.group(1) for n in archivo.namelist() if (m := re.fullmatch(r"igj-entidades-(\d{6})\.csv", n))}
    if not periodos:
        raise ValueError("el ZIP no tiene igj-entidades-AAAAMM.csv")
    return max(periodos)


def _filas(archivo: zipfile.ZipFile, nombre: str) -> Iterator[dict[str, str]]:
    with archivo.open(nombre) as crudo:
        yield from csv.DictReader(io.TextIOWrapper(crudo, encoding="utf-8-sig", newline=""))


def _fecha(texto: str | None, hoy: date) -> str | None:
    if not texto or not re.fullmatch(r"\d{8}", texto):
        return None
    try:
        fecha = date(int(texto[:4]), int(texto[4:6]), int(texto[6:]))
    except ValueError:
        return None
    return fecha.isoformat() if date(1850, 1, 1) <= fecha <= hoy else None


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) != 3:
        sys.exit("uso: python -m relevo.fuentes.igj <zip de IGJ> <destino.sqlite>")
    cantidad = armar_snapshot(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"snapshot de IGJ con {cantidad} CUIT en {sys.argv[2]}")
