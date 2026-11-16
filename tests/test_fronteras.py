"""Fronteras entre productos (ADR 0008).

La lectura de documentos y el chat se van a vender por separado, cada uno en su
repositorio. Mientras viven acá, estas reglas mantienen la frontera para que extraerlos
después sea mecánico:

- ``lectura_documentos`` no importa nada de Relevo salvo el seam ``clientes_claude``.
- Nadie importa ``chat`` salvo la capa que compone la app (``servicio`` y ``app``).
- ``monitoreo`` es parte del chat: solo lo importan el chat y la composición.
"""

import ast
from pathlib import Path

import relevo

RAIZ = Path(relevo.__file__).parent
COMPOSICION = {"relevo.servicio", "relevo.app"}


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


def test_solo_la_composicion_importa_el_chat():
    quienes = {modulo for modulo, importados in _grafo().items() if _toca(importados, "relevo.chat")}

    assert quienes <= COMPOSICION


def test_solo_el_chat_y_la_composicion_importan_el_monitoreo():
    quienes = {modulo for modulo, importados in _grafo().items() if _toca(importados, "relevo.monitoreo")}

    assert quienes <= COMPOSICION | {"relevo.chat"}


def test_el_test_ve_los_imports_reales():
    """Si el grafo quedara vacío, los otros tests pasarían sin controlar nada."""
    grafo = _grafo()

    assert _toca(grafo["relevo.servicio"], "relevo.chat")
    assert _toca(grafo["relevo.chat"], "relevo.monitoreo")
    assert _toca(grafo["relevo.fuentes.documentos"], "relevo.lectura_documentos")
