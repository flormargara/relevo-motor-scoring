"""Versiones de reglas de cada flujo, guardadas en la base (ADR 0007).

Las reglas viven en la base; los YAML de ``rules/`` son la semilla: ``importar`` trae
las versiones que todavía no están y nunca pisa una que ya existe. Cada versión es
inmutable y se valida al guardarla, así que leerla nunca falla por formato.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from relevo.catalogo import catalogo, flujos
from relevo.diff_reglas import Cambio, diferencias
from relevo.reglas import DIRECTORIO_MODELOS, Reglas, ReglasInvalidas, reglas_desde_dict
from relevo.repositorio import BorradorReglas, Repositorio


class ConflictoDeVersion(ReglasInvalidas):
    """Se quiso publicar sobre una versión que ya no es la vigente."""


_ARCHIVO = re.compile(r"(?P<flujo>[a-z_]+)\.v(?P<version>\d+)\.yaml")


class VersionesReglas:
    def __init__(self, repositorio: Repositorio, directorio_modelos: Path = DIRECTORIO_MODELOS):
        self.repositorio = repositorio
        self.directorio_modelos = directorio_modelos
        self._cache: dict[tuple[str, int], Reglas] = {}

    def importar(self, directorio: Path) -> list[str]:
        """Trae de ``directorio`` las versiones que la base no tiene. Devuelve sus ids."""
        archivos = []
        for archivo in directorio.glob("*.yaml"):
            if coincidencia := _ARCHIVO.fullmatch(archivo.name):
                archivos.append((coincidencia["flujo"], int(coincidencia["version"]), archivo))
        importadas = []
        for flujo, version, archivo in sorted(archivos, key=lambda a: (a[0], a[1])):
            if self.repositorio.version_reglas(flujo, version) is not None:
                continue
            texto = archivo.read_text(encoding="utf-8")
            self._parsear(texto, flujo, version, archivo.name)
            self.repositorio.guardar_version_reglas(
                flujo, version, texto, autor="semilla", motivo=f"importada de {archivo.name}"
            )
            importadas.append(f"{flujo}.v{version}")
        return importadas

    def publicar(self, flujo: str, contenido: dict[str, Any], autor: str, motivo: str, base: int) -> Reglas:
        """Valida ``contenido`` y lo guarda como la versión siguiente, que pasa a ser la vigente.

        ``base`` es la versión de la que partió el borrador: si ya no es la vigente, alguien
        publicó en el medio y es un conflicto (bloqueo optimista). Nunca modifica una versión."""
        vigente = self.repositorio.version_reglas(flujo)
        if vigente is None:
            raise ReglasInvalidas(f"no hay reglas para el flujo {flujo!r}")
        if vigente.version != base:
            raise ConflictoDeVersion(
                f"el borrador parte de v{base}, pero la vigente ya es v{vigente.version}: "
                "revisá sus cambios antes de publicar"
            )
        if not autor.strip():
            raise ReglasInvalidas("falta el autor")
        if not motivo.strip():
            raise ReglasInvalidas("falta el motivo del cambio")
        nueva = vigente.version + 1
        datos = {"flujo": flujo, "version": nueva, **_sin_identidad(contenido)}
        texto = yaml.safe_dump(datos, allow_unicode=True, sort_keys=False)
        reglas = self._parsear(texto, flujo, nueva, f"{flujo}.v{nueva}")
        # Se compara lo que las reglas significan, no el texto: completar un valor por defecto no es un cambio.
        if not diferencias(self.reglas(flujo), reglas):
            raise ReglasInvalidas("el borrador no tiene cambios respecto de la versión vigente")
        try:
            self.repositorio.guardar_version_reglas(flujo, nueva, texto, autor.strip(), motivo.strip(), base=base)
        except sqlite3.IntegrityError:
            raise ConflictoDeVersion(f"otra persona acaba de publicar v{nueva}: revisá sus cambios") from None
        self.repositorio.borrar_borrador_reglas(flujo)
        return reglas

    def cambios(self, flujo: str, contenido: dict[str, Any]) -> list[Cambio]:
        """Lo que cambiaría ``contenido`` respecto de la vigente. Es la única fuente del diff:
        la usan el editor (vista previa), publicar y el historial. Si el contenido todavía no se
        puede leer (una condición a medio escribir), lanza ``ReglasInvalidas`` con el motivo."""
        vigente = self.reglas(flujo)
        datos = {"flujo": flujo, "version": vigente.version + 1, **_sin_identidad(contenido)}
        return diferencias(vigente, reglas_desde_dict(datos, self.directorio_modelos))

    def candidata(self, flujo: str, contenido: dict[str, Any]) -> tuple[Reglas, list[Cambio]]:
        """Valida ``contenido`` como lo haría publicar (formato y catálogo) sin guardar nada, y
        devuelve las reglas y sus cambios respecto de la vigente. La usa el simulador."""
        nueva = self.reglas(flujo).version + 1
        datos = {"flujo": flujo, "version": nueva, **_sin_identidad(contenido)}
        texto = yaml.safe_dump(datos, allow_unicode=True, sort_keys=False)
        reglas = self._parsear(texto, flujo, nueva, f"{flujo} (candidata)")
        return reglas, diferencias(self.reglas(flujo), reglas)

    def contenido(self, flujo: str, version: int | None = None) -> dict[str, Any]:
        """El contenido de una versión publicada (la vigente si no se pide), sin flujo ni versión."""
        guardada = self.repositorio.version_reglas(flujo, version)
        if guardada is None:
            raise ReglasInvalidas(f"no hay reglas para el flujo {flujo!r}")
        return _sin_identidad(yaml.safe_load(guardada.contenido))

    # --- borrador: uno por flujo, compartido, sin validar hasta publicar ------

    def guardar_borrador(self, flujo: str, contenido: dict[str, Any], base: int) -> None:
        self.repositorio.guardar_borrador_reglas(flujo, contenido, base)

    def borrador(self, flujo: str) -> BorradorReglas | None:
        return self.repositorio.borrador_reglas(flujo)

    def descartar_borrador(self, flujo: str) -> None:
        self.repositorio.borrar_borrador_reglas(flujo)

    def actualizar_base_del_borrador(self, flujo: str) -> None:
        """Un borrador que parte de una versión vieja pasa a partir de la vigente, con su mismo contenido.

        No mezcla nada: los cambios contra la vigente muestran también lo que publicarlo desharía
        de lo que otra persona publicó en el medio, para revisarlo antes de publicar."""
        borrador = self.repositorio.borrador_reglas(flujo)
        if borrador is not None:
            self.repositorio.guardar_borrador_reglas(flujo, borrador.contenido, self.reglas(flujo).version)

    def cargar_version_en_el_borrador(self, flujo: str, version: int) -> None:
        """Volver atrás: el contenido de una versión publicada pasa a ser el borrador, sobre la vigente.
        Publicarlo crea una versión nueva; ninguna versión se modifica."""
        guardada = self.repositorio.version_reglas(flujo, version)
        if guardada is None:
            raise ReglasInvalidas(f"no hay reglas para el flujo {flujo!r} v{version}")
        contenido = _sin_identidad(yaml.safe_load(guardada.contenido))
        self.repositorio.guardar_borrador_reglas(flujo, contenido, self.reglas(flujo).version)

    def reglas(self, flujo: str, version: int | None = None) -> Reglas:
        """Las reglas de la versión pedida o, si no se pide, de la vigente."""
        guardada = self.repositorio.version_reglas(flujo, version)
        if guardada is None:
            detalle = f" v{version}" if version is not None else ""
            raise ReglasInvalidas(f"no hay reglas para el flujo {flujo!r}{detalle}")
        clave = (guardada.flujo, guardada.version)
        if clave not in self._cache:
            self._cache[clave] = self._parsear(guardada.contenido, *clave, f"{guardada.flujo}.v{guardada.version}")
        return self._cache[clave]

    def _parsear(self, texto: str, flujo: str, version: int, donde: str) -> Reglas:
        try:
            datos = yaml.safe_load(texto)
        except yaml.YAMLError as error:
            raise ReglasInvalidas(f"{donde}: YAML inválido: {error}") from None
        reglas = reglas_desde_dict(datos, self.directorio_modelos)
        if reglas.flujo != flujo or reglas.version != version:
            raise ReglasInvalidas(f"{donde} declara la versión {reglas.version} del flujo {reglas.flujo!r}")
        if flujo not in flujos():
            raise ReglasInvalidas(f"{donde}: el flujo {flujo!r} no está en config/flujos.yaml")
        del_catalogo = {v.nombre for v in catalogo(flujo)}
        for nombre in reglas.variables:
            if nombre not in del_catalogo:
                raise ReglasInvalidas(f"{donde}: {nombre} no está en el catálogo de {flujos()[flujo].nombre}")
        return reglas


def _sin_identidad(contenido: dict[str, Any]) -> dict[str, Any]:
    """El contenido sin flujo ni versión: esos los pone quien guarda, no el borrador."""
    return {k: v for k, v in contenido.items() if k not in ("flujo", "segmento", "version")}
