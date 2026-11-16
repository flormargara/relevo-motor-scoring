"""Fronteras entre productos (ADR 0008).

La lectura de documentos y el chat se van a vender por separado, cada uno en su
repositorio. Mientras viven acá, estas reglas mantienen la frontera para que extraerlos
después sea mecánico:

- ``lectura_documentos`` no importa nada de Relevo salvo el seam ``clientes_claude``.
- El chat son ``chat``, ``chat_datos``, ``monitoreo`` y ``simulador``. Fuera de ellos, solo
  los importa la capa que compone la app (``servicio`` y ``app``).
"""

import ast
from pathlib import Path

import pytest

import relevo

RAIZ = Path(relevo.__file__).parent
COMPOSICION = {"relevo.servicio", "relevo.app"}
CHAT = {"relevo.chat", "relevo.chat_datos", "relevo.monitoreo", "relevo.simulador"}


def _modulo(archivo: Path) -> str:
    partes = archivo.relative_to(RAIZ.parent).with_suffix("").parts
    return ".".join(partes[:-1] if partes[-1] == "__init__" else partes)


def _imports(archivo: Path) -> set[str]:
    """Los módulos de ``relevo`` que importa el archivo (``from relevo import x`` cuenta como ``relevo.x``)."""
    importados = set()
    for nodo in ast.walk(ast.parse(archivo.read_text())):
        if isinstance(nodo, ast.Import):
            importados |= {alias.name for alias in nodo.names}
        elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            importados.add(nodo.module)
            importados |= {f"{nodo.module}.{alias.name}" for alias in nodo.names}
    return {m for m in importados if m == "relevo" or m.startswith("relevo.")}


def _grafo() -> dict[str, set[str]]:
    return {_modulo(a): _imports(a) for a in RAIZ.rglob("*.py")}


def _toca(importados: set[str], modulo: str) -> bool:
    return any(m == modulo or m.startswith(f"{modulo}.") for m in importados)


def test_lectura_de_documentos_no_depende_del_motor():
    importados = _grafo()["relevo.lectura_documentos"]

    ajenos = {m for m in importados if not m.startswith("relevo.clientes_claude")}
    assert ajenos == set()


@pytest.mark.parametrize("modulo", sorted(CHAT))
def test_solo_la_composicion_y_el_chat_importan_el_chat(modulo):
    quienes = {m for m, importados in _grafo().items() if _toca(importados, modulo)}

    assert quienes <= COMPOSICION | CHAT


def test_el_test_ve_los_imports_reales():
    """Si el grafo quedara vacío, los otros tests pasarían sin controlar nada."""
    grafo = _grafo()

    assert _toca(grafo["relevo.servicio"], "relevo.chat")
    assert _toca(grafo["relevo.chat_datos"], "relevo.simulador")
    assert _toca(grafo["relevo.fuentes.documentos"], "relevo.lectura_documentos")
