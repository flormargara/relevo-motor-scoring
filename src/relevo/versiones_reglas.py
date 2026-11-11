"""Versiones de reglas de cada flujo, guardadas en la base (ADR 0007).

Las reglas viven en la base; los YAML de ``rules/`` son la semilla: ``importar`` trae
las versiones que todavía no están y nunca pisa una que ya existe. Cada versión es
inmutable y se valida al guardarla, así que leerla nunca falla por formato.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from relevo.catalogo import catalogo, flujos
from relevo.reglas import DIRECTORIO_MODELOS, Reglas, ReglasInvalidas, reglas_desde_dict
from relevo.repositorio import Repositorio

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
        if reglas.segmento != flujo or reglas.version != version:
            raise ReglasInvalidas(f"{donde} declara la versión {reglas.version} del flujo {reglas.segmento!r}")
        if flujo not in flujos():
            raise ReglasInvalidas(f"{donde}: el flujo {flujo!r} no está en config/flujos.yaml")
        del_catalogo = {v.nombre for v in catalogo(flujo)}
        for nombre in reglas.variables:
            if nombre not in del_catalogo:
                raise ReglasInvalidas(f"{donde}: {nombre} no está en el catálogo de {flujos()[flujo].nombre}")
        return reglas
