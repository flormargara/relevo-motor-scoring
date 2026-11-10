"""ARCA (ex AFIP): condición tributaria desde un snapshot del padrón público.

ARCA publica, sin autenticación, un archivo con la condición tributaria de cada CUIT
(Ganancias, IVA, categoría de monotributo, empleador, actividad del monotributo):
https://www.afip.gob.ar/genericos/cInscripcion/archivoCompleto.asp

Es un TXT de ancho fijo en latin-1 dentro de un ZIP de unos 86 MB (6,2 millones de
CUIT). Diseño de registro (posiciones 1-based, verificado contra el archivo; la página
de ARCA dice 51/52 para la actividad, pero las líneas tienen 51 caracteres):

    CUIT 1-11 · denominación 12-41 · Ganancias 42-43 · IVA 44-45 · monotributo 46-47
    · integrante de sociedad 48 · empleador 49 · actividad del monotributo 50-51

Un CUIT que no figura en el padrón da *sin antecedentes*. Un campo en blanco es *sin
dato*: ARCA solo informa un impuesto cuando los datos del contribuyente son consistentes.

No reemplaza a los web services con certificado (que traen fecha de inicio y
actividades): es la alternativa gratuita y sin trámite, con la fecha del padrón.

Armar el snapshot (una vez por actualización del padrón, offline):

    python -m relevo.fuentes.arca apellidoNombreDenominacion.zip data/arca.sqlite
"""

from __future__ import annotations

import io
import re
import sqlite3
import sys
import zipfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

from relevo.consultor import Disponible, ResultadoFuente, SinAntecedentes
from relevo.fuentes import FuenteCaida

SNAPSHOT_POR_DEFECTO = Path(__file__).resolve().parents[3] / "data" / "arca.sqlite"

GANANCIAS = {"NI": "no_inscripto", "AC": "activo", "EX": "exento", "NC": "no_corresponde"}
IVA = {
    "NI": "no_inscripto",
    "AC": "activo",
    "EX": "exento",
    "NA": "no_alcanzado",
    "XN": "exento_no_alcanzado",
    "AN": "activo_no_alcanzado",
}
# Código de monotributo → (categoría, tipo). Las variantes especiales son categoría A (o B).
MONOTRIBUTO = {
    **{letra: (letra, "general") for letra in "ABCDEFGHIJK"},
    "AP": ("A", "actividad_primaria"),
    "AC": ("A", "asociado_cooperativa"),
    "AL": ("A", "social"),
    "AV": ("A", "social"),
    "AT": ("A", "trabajador_promovido"),
    "BT": ("B", "trabajador_promovido"),
}
ACTIVIDAD_MONOTRIBUTO = {
    "01": "comercial",
    "02": "profesional",
    "03": "servicios_oficio",
    "04": "industrial",
    "05": "agropecuaria",
    "06": "otros",
    "07": "eventual",
    "08": "servicios_locacion",
    "09": "otras_actividades",
    "10": "ventas",
    "11": "agricultura_familiar",
}


class Arca:
    nombre = "arca"

    def __init__(self, ruta: Path = SNAPSHOT_POR_DEFECTO):
        self.ruta = ruta

    def consultar(self, identificacion: str, timeout: float) -> dict[str, Any]:
        if not self.ruta.is_file():
            raise FuenteCaida(f"no hay snapshot de ARCA en {self.ruta}")
        cuit = int(identificacion) if re.fullmatch(r"\d{11}", identificacion) else -1
        with sqlite3.connect(f"file:{self.ruta}?mode=ro", uri=True) as conexion:
            conexion.row_factory = sqlite3.Row
            fila = conexion.execute("SELECT * FROM contribuyentes WHERE cuit = ?", (cuit,)).fetchone()
            meta = dict(conexion.execute("SELECT clave, valor FROM meta").fetchall())
        return {
            "registro": {**dict(fila), "cuit": f"{fila['cuit']:011d}"} if fila else None,
            "fecha_padron": meta.get("fecha_padron"),
        }

    def traducir(self, crudo: dict[str, Any]) -> ResultadoFuente:
        registro = crudo["registro"]
        if registro is None:
            return SinAntecedentes()
        ganancias = GANANCIAS.get(registro["ganancias"] or "")
        iva = IVA.get(registro["iva"] or "")
        categoria, tipo = MONOTRIBUTO.get(registro["monotributo"] or "", (None, None))
        return Disponible(
            {
                "denominacion": registro["denominacion"] or None,
                "ganancias": ganancias,
                "iva": iva,
                "es_monotributista": categoria is not None if registro["monotributo"] else None,
                "categoria_monotributo": categoria,
                "tipo_monotributo": tipo,
                "actividad_monotributo": ACTIVIDAD_MONOTRIBUTO.get(registro["actividad_monotributo"] or ""),
                "empleador": _si_no(registro["empleador"]),
                "integrante_sociedad": _si_no(registro["integrante_sociedad"]),
                "inscripcion_vigente": (
                    ganancias in ("activo", "exento")
                    or iva in ("activo", "exento", "no_alcanzado", "exento_no_alcanzado", "activo_no_alcanzado")
                    or categoria is not None
                ),
                "fecha_padron": crudo["fecha_padron"],
            }
        )


def _si_no(codigo: str | None) -> bool | None:
    return {"S": True, "N": False}.get(codigo or "")


# --- armado del snapshot ----------------------------------------------------


def leer_linea(linea: str) -> dict[str, Any] | None:
    """Una línea del padrón → registro con los códigos tal como vienen (blanco → None)."""
    linea = linea.rstrip("\r\n")
    cuit = linea[:11]
    if not re.fullmatch(r"\d{11}", cuit):
        return None

    def campo(desde: int, hasta: int) -> str | None:
        return linea[desde:hasta].strip() or None

    return {
        "cuit": int(cuit),
        "denominacion": campo(11, 41),
        "ganancias": campo(41, 43),
        "iva": campo(43, 45),
        "monotributo": campo(45, 47),
        "integrante_sociedad": campo(47, 48),
        "empleador": campo(48, 49),
        "actividad_monotributo": campo(49, 51),
    }


def armar_snapshot(zip_arca: Path, destino: Path, hoy: date | None = None) -> int:
    """Lee el ZIP del padrón de ARCA y escribe el snapshot SQLite. Devuelve cuántos CUIT quedaron."""
    hoy = hoy or date.today()
    with zipfile.ZipFile(zip_arca) as archivo:
        nombres = [n for n in archivo.namelist() if not n.endswith("/")]
        if len(nombres) != 1:
            raise ValueError(f"se esperaba un único archivo en el ZIP y hay {len(nombres)}")
        nombre = nombres[0]
        fecha = re.search(r"\.(\d{8})(?:\.|$)", nombre)
        fecha_padron = f"{fecha[1][:4]}-{fecha[1][4:6]}-{fecha[1][6:]}" if fecha else None

        destino.parent.mkdir(parents=True, exist_ok=True)
        temporal = destino.with_suffix(".tmp")
        temporal.unlink(missing_ok=True)
        conexion = sqlite3.connect(temporal)
        try:
            conexion.execute(
                "CREATE TABLE contribuyentes (cuit INTEGER PRIMARY KEY, denominacion TEXT, ganancias TEXT, "
                "iva TEXT, monotributo TEXT, integrante_sociedad TEXT, empleador TEXT, actividad_monotributo TEXT)"
            )
            conexion.execute("CREATE TABLE meta (clave TEXT PRIMARY KEY, valor TEXT)")
            with archivo.open(nombre) as crudo:
                conexion.executemany(
                    "INSERT OR REPLACE INTO contribuyentes VALUES (:cuit, :denominacion, :ganancias, :iva, "
                    ":monotributo, :integrante_sociedad, :empleador, :actividad_monotributo)",
                    _registros(io.TextIOWrapper(crudo, encoding="latin-1", newline="")),
                )
            conexion.executemany(
                "INSERT INTO meta VALUES (?, ?)",
                [("fecha_padron", fecha_padron), ("origen", f"{zip_arca.name}:{nombre}"), ("armado", hoy.isoformat())],
            )
            conexion.commit()
            (cantidad,) = conexion.execute("SELECT COUNT(*) FROM contribuyentes").fetchone()
        finally:
            conexion.close()
    temporal.replace(destino)
    return cantidad


def _registros(lineas: Iterator[str]) -> Iterator[dict[str, Any]]:
    for linea in lineas:
        if (registro := leer_linea(linea)) is not None:
            yield registro


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) != 3:
        sys.exit("uso: python -m relevo.fuentes.arca <zip del padrón de ARCA> <destino.sqlite>")
    cantidad = armar_snapshot(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"snapshot de ARCA con {cantidad} CUIT en {sys.argv[2]}")
