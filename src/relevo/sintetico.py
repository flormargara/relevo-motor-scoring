"""Cartera sintética y entrenamiento del scorecard (offline, no es parte del runtime).

    python -m relevo.sintetico pyme --version 2
    python -m relevo.sintetico monotributista --version 2

1. ``generar_cartera``: solicitantes que ya pasaron los killers, con las mismas
   variables que producen los adapters de fuente (incluidos los faltantes: CENDEU
   sin antecedentes, IGJ fuera de CABA, bureau caído) y un default a 12 meses.
   Detrás hay un factor latente de riesgo, que el modelo no ve, que mueve a la vez
   las características y la probabilidad de default.
2. ``entrenar``: tramos fijos por característica (definidos por dominio, así los
   puntos son monótonos y explicables) → WoE → regresión logística → puntos con la
   escala estándar. El faltante es un tramo más: sus puntos son los ``sin_dato``.
3. Se mide en una muestra de validación (AUC, Gini, KS y default observado por
   rating) y se exporta en el formato de ``modelos/<segmento>.v<N>.json``.

Todo es determinístico dada la semilla.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from relevo.reglas import DIRECTORIO_MODELOS, cargar_reglas

SEMILLA = 2026
FILAS = 40_000
ESCALA = {"puntos_base": 600, "odds_base": 50, "pdo": 20}


@dataclass(frozen=True)
class Caracteristica:
    nombre: str
    expresion: str  # cómo la calcula el motor
    columna: str  # columna de la cartera
    cortes: tuple[float, ...]  # límites superiores inclusive; después queda un tramo abierto
    monotona: bool = True  # el riesgo crece o decrece a lo largo de los tramos


ESPECIFICACION: dict[str, tuple[Caracteristica, ...]] = {
    "pyme": (
        Caracteristica("peor_situacion_12m", "cendeu.peor_situacion_12m", "peor_situacion_12m", (0, 1, 2)),
        Caracteristica("entidades_con_deuda", "cendeu.entidades_con_deuda", "entidades_con_deuda", (1, 3, 5)),
        Caracteristica("antiguedad_anios", "igj.antiguedad_anios", "antiguedad_anios", (3, 7, 15)),
        Caracteristica("score_bureau", "bureau.score", "score_bureau", (450, 550, 650, 750)),
        Caracteristica(
            "endeudamiento_sobre_ventas",
            "cendeu.deuda_total / documentos.ventas_anuales",
            "endeudamiento",
            (0.05, 0.15, 0.30, 0.50),
        ),
    ),
    "monotributista": (
        Caracteristica("peor_situacion_12m", "cendeu.peor_situacion_12m", "peor_situacion_12m", (0, 1, 2)),
        Caracteristica("entidades_con_deuda", "cendeu.entidades_con_deuda", "entidades_con_deuda", (1, 2, 4)),
        Caracteristica("score_bureau", "bureau.score", "score_bureau", (450, 550, 650, 750)),
        Caracteristica(
            "antiguedad_actividad_anios",
            "documentos.antiguedad_actividad_anios",
            "antiguedad_actividad_anios",
            (2, 5, 10),
        ),
        Caracteristica("edad", "identidad.edad", "edad", (25, 35, 55, 65), monotona=False),
    ),
}


# --- cartera ----------------------------------------------------------------


def generar_cartera(segmento: str, filas: int = FILAS, semilla: int = SEMILLA) -> pd.DataFrame:
    rng = np.random.default_rng(semilla)
    z = rng.normal(size=filas)  # riesgo latente: más alto, más riesgoso
    ruido = lambda: rng.normal(size=filas)  # noqa: E731

    sin_antecedentes = rng.random(filas) < (0.12 if segmento == "pyme" else 0.20)
    z = z + 0.35 * sin_antecedentes  # sin historial es algo más riesgoso

    situacion_latente = 0.8 * z + 0.6 * ruido()
    peor_situacion = np.digitize(situacion_latente, [-1.0, 0.85, 1.9, 2.4, 2.8]).astype(float)
    bureau = np.clip(np.round(640 - 115 * (0.7 * z + 0.7 * ruido())), 300, 999)
    bureau[rng.random(filas) < 0.04] = np.nan  # bureau caído o sin dato

    datos: dict[str, Any] = {"peor_situacion_12m": peor_situacion, "score_bureau": bureau}

    if segmento == "pyme":
        entidades = 1 + rng.poisson(np.exp(0.6 + 0.25 * z + 0.3 * ruido()))
        antiguedad = np.maximum(1.0, np.exp(1.9 - 0.25 * z + 0.7 * ruido()))
        antiguedad[rng.random(filas) < 0.65] = np.nan  # fuera de CABA o sin registros en IGJ
        ventas = np.exp(rng.normal(math.log(300e6), 1.0, size=filas))
        endeudamiento = np.exp(-1.9 + 0.45 * z + 0.7 * ruido())
        datos |= {
            "entidades_con_deuda": entidades.astype(float),
            "antiguedad_anios": np.round(antiguedad, 2),
            "ventas_anuales": np.round(ventas, -3),
            "deuda_total": np.round(ventas * endeudamiento, -3),
            "endeudamiento": endeudamiento,
        }
        logit = -3.45 + 1.15 * z
    else:
        entidades = 1 + rng.poisson(np.exp(-0.2 + 0.3 * z + 0.3 * ruido()))
        antiguedad = np.maximum(1.0, np.exp(1.5 - 0.2 * z + 0.7 * ruido()))
        edad = np.clip(np.round(rng.normal(42, 12, size=filas)), 18, 75)
        datos |= {
            "entidades_con_deuda": entidades.astype(float),
            "antiguedad_actividad_anios": np.round(antiguedad, 2),
            "edad": edad,
        }
        logit = -3.1 + 1.15 * z + 0.55 * (edad < 25) + 0.25 * (edad > 65)

    cartera = pd.DataFrame(datos)
    for columna in ("peor_situacion_12m", "entidades_con_deuda", "deuda_total", "endeudamiento"):
        if columna in cartera:
            cartera.loc[sin_antecedentes, columna] = np.nan
    cartera["sin_antecedentes_bcra"] = sin_antecedentes
    cartera["default"] = (rng.random(filas) < 1 / (1 + np.exp(-logit))).astype(int)
    return cartera


# --- entrenamiento --------------------------------------------------------


def _tramo(valores: pd.Series, cortes: tuple[float, ...]) -> pd.Series:
    """Índice de tramo (0..len(cortes)) o -1 para el faltante."""
    indices = np.searchsorted(np.asarray(cortes), valores.to_numpy(), side="left")
    return pd.Series(np.where(valores.isna(), -1, indices), index=valores.index)


def _woe(tramos: pd.Series, default: pd.Series) -> dict[int, float]:
    malos_total, buenos_total = default.sum(), (1 - default).sum()
    woe = {}
    for tramo in sorted(tramos.unique()):
        en_tramo = tramos == tramo
        malos = default[en_tramo].sum() + 0.5  # suavizado para tramos chicos
        buenos = (1 - default[en_tramo]).sum() + 0.5
        woe[int(tramo)] = math.log((buenos / buenos_total) / (malos / malos_total))
    return woe


def entrenar(segmento: str, cartera: pd.DataFrame, version: int, semilla: int = SEMILLA) -> dict[str, Any]:
    especificacion = ESPECIFICACION[segmento]
    rng = np.random.default_rng(semilla + 1)
    validacion = rng.random(len(cartera)) < 0.3
    entrena, valida = cartera[~validacion], cartera[validacion]

    woes, columnas = {}, {}
    for c in especificacion:
        woes[c.nombre] = _woe(_tramo(entrena[c.columna], c.cortes), entrena["default"])
        columnas[c.nombre] = _tramo(entrena[c.columna], c.cortes).map(woes[c.nombre])
    modelo = LogisticRegression(C=1e6, max_iter=1000).fit(pd.DataFrame(columnas), entrena["default"])
    intercepto, coeficientes = float(modelo.intercept_[0]), dict(zip(woes, modelo.coef_[0], strict=True))

    factor = ESCALA["pdo"] / math.log(2)
    offset = ESCALA["puntos_base"] - factor * math.log(ESCALA["odds_base"])
    n = len(especificacion)

    def puntos(nombre: str, woe: float) -> int:
        # logit(PD) = b0 + Σ b·woe  y  score = offset + factor·ln(odds buenos/malos) = offset − factor·logit
        return round(-(coeficientes[nombre] * woe + intercepto / n) * factor + offset / n)

    caracteristicas = []
    for c in especificacion:
        tabla = woes[c.nombre]
        tramos = [{"hasta": corte, "puntos": puntos(c.nombre, tabla.get(i, 0.0))} for i, corte in enumerate(c.cortes)]
        tramos.append({"puntos": puntos(c.nombre, tabla.get(len(c.cortes), 0.0))})
        caracteristicas.append(
            {
                "nombre": c.nombre,
                "expresion": c.expresion,
                "tramos": tramos,
                # sin faltantes en la cartera: puntos neutros (WoE 0)
                "sin_dato": puntos(c.nombre, tabla.get(-1, 0.0)),
                "coeficiente": round(float(coeficientes[c.nombre]), 4),
            }
        )

    resultado = {
        "segmento": segmento,
        "version": version,
        "provisional": False,
        "nota": "Entrenado sobre una cartera SINTÉTICA. Sirve para la demo, no para decidir créditos reales.",
        "generado_con": f"python -m relevo.sintetico {segmento} --version {version} --semilla {semilla}",
        "escala": ESCALA,
        "caracteristicas": caracteristicas,
    }
    resultado["metricas"] = medir(resultado, valida, segmento)
    resultado["metricas"]["filas_entrenamiento"] = int(len(entrena))
    resultado["metricas"]["tasa_default_entrenamiento"] = round(float(entrena["default"].mean()), 4)
    return resultado


# --- medición ---------------------------------------------------------------


def puntuar(modelo: dict[str, Any], cartera: pd.DataFrame, segmento: str) -> pd.Series:
    """Puntos de cada fila con los tramos exportados (igual que el motor)."""
    especificacion = {c.nombre: c for c in ESPECIFICACION[segmento]}
    total = pd.Series(0.0, index=cartera.index)
    for caracteristica in modelo["caracteristicas"]:
        valores = cartera[especificacion[caracteristica["nombre"]].columna]
        cortes = tuple(t["hasta"] for t in caracteristica["tramos"] if "hasta" in t)
        tabla = {i: t["puntos"] for i, t in enumerate(caracteristica["tramos"])} | {-1: caracteristica["sin_dato"]}
        total += _tramo(valores, cortes).map(tabla)
    return total


def pd_desde_puntos(puntos: pd.Series, escala: dict[str, float]) -> pd.Series:
    odds = escala["odds_base"] * 2 ** ((puntos - escala["puntos_base"]) / escala["pdo"])
    return 1 / (1 + odds)


def medir(modelo: dict[str, Any], valida: pd.DataFrame, segmento: str) -> dict[str, Any]:
    puntos = puntuar(modelo, valida, segmento)
    pd_estimada = pd_desde_puntos(puntos, modelo["escala"])
    default = valida["default"]
    auc = roc_auc_score(default, pd_estimada)
    orden = np.argsort(-pd_estimada.to_numpy())
    malos = np.cumsum(default.to_numpy()[orden]) / default.sum()
    buenos = np.cumsum(1 - default.to_numpy()[orden]) / (1 - default).sum()

    ratings = cargar_reglas(segmento, version=1).ratings
    por_rating = []
    desde = 0.0
    for rating in ratings:
        en_rating = (pd_estimada > desde) & (pd_estimada <= rating.pd_hasta)
        por_rating.append(
            {
                "rating": rating.nombre,
                "participacion": round(float(en_rating.mean()), 4),
                "pd_media": round(float(pd_estimada[en_rating].mean()), 4) if en_rating.any() else None,
                "default_observado": round(float(default[en_rating].mean()), 4) if en_rating.any() else None,
            }
        )
        desde = rating.pd_hasta
    return {
        "filas_validacion": int(len(valida)),
        "tasa_default_validacion": round(float(default.mean()), 4),
        "pd_media_validacion": round(float(pd_estimada.mean()), 4),
        "auc": round(float(auc), 4),
        "gini": round(float(2 * auc - 1), 4),
        "ks": round(float(np.max(malos - buenos)), 4),
        "por_rating": por_rating,
    }


# --- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description="Genera la cartera sintética y entrena el scorecard.")
    parser.add_argument("segmento", choices=sorted(ESPECIFICACION))
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--semilla", type=int, default=SEMILLA)
    parser.add_argument("--filas", type=int, default=FILAS)
    parser.add_argument("--destino", type=Path, default=DIRECTORIO_MODELOS)
    args = parser.parse_args(argv)

    cartera = generar_cartera(args.segmento, args.filas, args.semilla)
    modelo = entrenar(args.segmento, cartera, args.version, args.semilla)
    archivo = args.destino / f"{args.segmento}.v{args.version}.json"
    archivo.write_text(json.dumps(modelo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    m = modelo["metricas"]
    print(f"{archivo}: AUC {m['auc']}  Gini {m['gini']}  KS {m['ks']}  default {m['tasa_default_validacion']}")
    for fila in m["por_rating"]:
        print(f"  {fila}")
    return archivo


if __name__ == "__main__":  # pragma: no cover
    main()
