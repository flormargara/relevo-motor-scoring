"""Validación de CUIT/CUIL (prefijo y dígito verificador módulo 11)."""

from __future__ import annotations

PREFIJOS = {
    "20": "humana",
    "23": "humana",
    "24": "humana",
    "27": "humana",
    "30": "juridica",
    "33": "juridica",
    "34": "juridica",
}
_PESOS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


def normalizar(cuit: str) -> str:
    return cuit.replace("-", "").replace(" ", "").strip()


def tipo_persona(cuit: str) -> str | None:
    """`humana`, `juridica` o None si el prefijo no es válido."""
    return PREFIJOS.get(normalizar(cuit)[:2])


def es_valido(cuit: str) -> bool:
    numero = normalizar(cuit)
    if len(numero) != 11 or not numero.isascii() or not numero.isdigit() or tipo_persona(numero) is None:
        return False
    resto = 11 - sum(int(d) * p for d, p in zip(numero, _PESOS, strict=False)) % 11
    verificador = {11: 0, 10: None}.get(resto, resto)
    return verificador is not None and verificador == int(numero[-1])
