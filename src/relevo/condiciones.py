"""Lenguaje acotado de condiciones de las reglas.

Una condición es una expresión de Python restringida, por ejemplo
``cendeu.peor_situacion_12m >= 3 and propios.lista_negra``. Se parsea con
``ast`` y se valida contra una lista blanca de nodos al construirla; nunca se
usa ``eval``. Las variables siempre tienen la forma ``fuente.variable``, y sus
valores los provee quien evalúa, a través de un *resolver*.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from typing import Any

Resolver = Callable[[str, str], Any]


class CondicionInvalida(ValueError):
    """La expresión no respeta el lenguaje de condiciones."""


class SinDato(Exception):
    """La expresión no se puede calcular porque falta un dato (no es una falla)."""


_COMPARACIONES = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_ARITMETICA = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


class Expresion:
    """Una condición (o expresión numérica) validada y lista para evaluar."""

    def __init__(self, texto: str):
        self.texto = texto
        try:
            arbol = ast.parse(texto.strip(), mode="eval")
        except SyntaxError as error:
            raise CondicionInvalida(f"sintaxis inválida en {texto!r}: {error.msg}") from None
        self._raiz = arbol.body
        referencias: list[tuple[str, str]] = []
        self._validar(self._raiz, referencias)
        self.referencias: tuple[tuple[str, str], ...] = tuple(dict.fromkeys(referencias))

    def __repr__(self) -> str:
        return f"Expresion({self.texto!r})"

    @property
    def fuentes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(fuente for fuente, _ in self.referencias))

    def evaluar(self, resolver: Resolver, valores_usados: dict[str, Any] | None = None) -> Any:
        """Evalúa la expresión. Resuelve las variables de izquierda a derecha y en
        cortocircuito, así una fuente que no hace falta no se consulta."""
        usados = valores_usados if valores_usados is not None else {}
        return self._evaluar(self._raiz, resolver, usados)

    # --- validación -------------------------------------------------------

    def _validar(self, nodo: ast.AST, referencias: list[tuple[str, str]]) -> None:
        match nodo:
            case ast.BoolOp(values=valores):
                for valor in valores:
                    self._validar(valor, referencias)
            case ast.UnaryOp(op=ast.Not() | ast.USub(), operand=operando):
                self._validar(operando, referencias)
            case ast.BinOp(left=izq, op=op, right=der) if type(op) in _ARITMETICA:
                self._validar(izq, referencias)
                self._validar(der, referencias)
            case ast.Compare(left=izq, ops=ops, comparators=comparados):
                if not all(type(op) in _COMPARACIONES for op in ops):
                    raise self._invalida("comparador no permitido")
                self._validar(izq, referencias)
                for comparado in comparados:
                    self._validar(comparado, referencias)
            case ast.Constant(value=valor):
                if not isinstance(valor, (int, float, str, bool, type(None))):
                    raise self._invalida(f"constante no permitida: {valor!r}")
            case ast.List(elts=elementos) | ast.Tuple(elts=elementos):
                for elemento in elementos:
                    if not isinstance(elemento, ast.Constant):
                        raise self._invalida("las listas solo pueden tener constantes")
                    self._validar(elemento, referencias)
            case ast.Attribute(value=ast.Name(id=fuente), attr=variable):
                if fuente.startswith("_") or variable.startswith("_"):
                    raise self._invalida(f"nombre no permitido: {fuente}.{variable}")
                referencias.append((fuente, variable))
            case ast.Name(id=nombre):
                raise self._invalida(f"variable sin fuente: {nombre!r} (usar fuente.variable)")
            case _:
                raise self._invalida(f"construcción no permitida: {type(nodo).__name__}")

    def _invalida(self, motivo: str) -> CondicionInvalida:
        return CondicionInvalida(f"{motivo} en {self.texto!r}")

    # --- evaluación -------------------------------------------------------

    def _evaluar(self, nodo: ast.AST, resolver: Resolver, usados: dict[str, Any]) -> Any:
        match nodo:
            case ast.BoolOp(op=ast.And(), values=valores):
                resultado: Any = True
                for valor in valores:
                    resultado = self._evaluar(valor, resolver, usados)
                    if not resultado:
                        return False
                return bool(resultado)
            case ast.BoolOp(op=ast.Or(), values=valores):
                for valor in valores:
                    if self._evaluar(valor, resolver, usados):
                        return True
                return False
            case ast.UnaryOp(op=ast.Not(), operand=operando):
                return not self._evaluar(operando, resolver, usados)
            case ast.UnaryOp(op=ast.USub(), operand=operando):
                return -self._evaluar(operando, resolver, usados)
            case ast.BinOp(left=izq, op=op, right=der):
                a = self._evaluar(izq, resolver, usados)
                b = self._evaluar(der, resolver, usados)
                try:
                    return _ARITMETICA[type(op)](a, b)
                except ZeroDivisionError:
                    raise SinDato(f"división por cero en {self.texto!r}") from None
            case ast.Compare(left=izq, ops=ops, comparators=comparados):
                a = self._evaluar(izq, resolver, usados)
                for op, comparado in zip(ops, comparados, strict=False):
                    b = self._evaluar(comparado, resolver, usados)
                    if not _COMPARACIONES[type(op)](a, b):
                        return False
                    a = b
                return True
            case ast.Constant(value=valor):
                return valor
            case ast.List(elts=elementos) | ast.Tuple(elts=elementos):
                return tuple(elemento.value for elemento in elementos)
            case ast.Attribute(value=ast.Name(id=fuente), attr=variable):
                valor = resolver(fuente, variable)
                usados[f"{fuente}.{variable}"] = valor
                return valor
        raise AssertionError(f"nodo no validado: {ast.dump(nodo)}")  # pragma: no cover
