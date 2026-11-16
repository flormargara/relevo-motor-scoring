"""Base de demo con solicitantes y mora reales de la Central de Deudores (offline).

    python -m relevo.demo_real --db data/demo.sqlite --destino data/demo/cartera

Arma una base de evaluaciones y su cartera a partir del bucket ``data-cendeu-raw``:

1. **Solicitantes reales.** Por cada período de observación se toma una muestra de CUIT
   *reservados para la demo* que figuran en la central: jurídicas para PyME y personas
   humanas adheridas al monotributo (según el padrón de ARCA) para monotributistas.
2. **Evaluación con el motor real**, por ``Servicio``, fechada el mes siguiente al
   período observado. CENDEU recibe la central hasta ese período en el formato de la
   API del BCRA, así que la traducen los adapters de siempre y no hay datos del futuro.
   IGJ, ARCA, BADLAR y la fecha de nacimiento son reales. Bureau, ventas, antigüedad de
   la actividad y el monto pedido son sintéticos. El bucket no tiene cheques rechazados:
   se asume *sin antecedentes* (lo más frecuente), porque una fuente no disponible deriva.
   ARCA e IGJ son los snapshots actuales, posteriores a la evaluación: es el único dato
   que mira el futuro.
3. **Cartera con mora real.** La situación de cada mes en la central después del
   desembolso se traduce a días de mora. La conversión, el desembolso y los pagos son
   sintéticos (``sintetico_cartera``).

**CUIT reservados.** Las variables del bucket se usan para entrenar el modelo de ML. Para
que la demo nunca muestre a alguien que el modelo vio, una fracción fija de CUIT,
elegida por hash, queda solo para la demo. El modelo tiene que excluir esos mismos CUIT
(``sql_reservado`` da el filtro en SQL).

Los CUIT son reales: la base y los CSV quedan en ``data/`` (fuera de git) y no se suben.
Requiere el extra ``demo`` (duckdb) y credenciales de AWS con lectura del bucket.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from relevo.cartera import escribir_en_archivos
from relevo.consultor import Disponible
from relevo.fuentes import AdapterFuente, FuenteCaida
from relevo.fuentes.arca import SNAPSHOT_POR_DEFECTO as SNAPSHOT_ARCA
from relevo.fuentes.arca import Arca
from relevo.fuentes.badlar import Badlar
from relevo.fuentes.cendeu import Cendeu, Cheques
from relevo.fuentes.documentos import Documentos
from relevo.fuentes.identidad import IdentidadDeclarada
from relevo.fuentes.igj import SNAPSHOT_POR_DEFECTO as SNAPSHOT_IGJ
from relevo.fuentes.igj import Igj
from relevo.fuentes.juguete import Bureau, Propios
from relevo.repositorio import Repositorio
from relevo.servicio import Servicio
from relevo.sintetico_cartera import generar, otorgadas

PORCENTAJE_RESERVADO = 5
BUCKET = "s3://data-cendeu-raw"
PERIODO_CORTE = 202609  # último período del bucket; en deudores/ está incompleto, sale de deudores_mensual/
CORTE_PADRON = 20261030
REINTENTOS_DESCARGA = 5
PERIODOS_OBS = (202509, 202510, 202511, 202512, 202601, 202602, 202603, 202604, 202605, 202606)
MESES_HISTORIA = 24  # lo que devuelve /Deudas/Historicas
SEMILLA = 2026
# Situación del BCRA → días de mora (punto medio del rango de cada situación).
DIAS_POR_SITUACION = {1: 0, 2: 60, 3: 120, 4: 270, 5: 365, 6: 365}
PREFIJOS = {"pyme": ("30", "33", "34"), "monotributista": ("20", "23", "24", "27")}
INGRESO_POR_CATEGORIA = dict(zip("ABCDEFGHIJK", (8, 12, 17, 21, 26, 32, 38, 58, 65, 75, 90), strict=True))  # millones


# --- CUIT reservados ----------------------------------------------------------


def es_reservado(cuit: str | int) -> bool:
    """El CUIT queda para la demo y fuera del entrenamiento del modelo de ML."""
    return int(hashlib.md5(str(int(cuit)).encode()).hexdigest()[:8], 16) % 100 < PORCENTAJE_RESERVADO


def sql_reservado(columna: str = "cuit") -> str:
    """La misma regla en SQL (duckdb), para excluir los reservados al entrenar."""
    return f"('0x' || left(md5(CAST({columna} AS VARCHAR)), 8))::UBIGINT % 100 < {PORCENTAJE_RESERVADO}"


# --- central → formato de la API del BCRA --------------------------------------


@dataclass(frozen=True)
class Deuda:
    """Una fila de la central: lo que una entidad informa de un CUIT en un período."""

    periodo: int  # AAAAMM
    entidad: str
    situacion: int
    monto: float  # miles de pesos, como la API
    proceso_judicial: bool = False


def crudo_cendeu(cuit: str, deudas: Iterable[Deuda], periodo_obs: int) -> dict[str, Any]:
    """Lo que habrían devuelto ``/Deudas`` y ``/Deudas/Historicas`` en ``periodo_obs``:
    nada posterior, y los últimos 24 meses como mucho."""
    por_periodo: dict[int, list[Deuda]] = {}
    for d in deudas:
        if d.periodo <= periodo_obs:
            por_periodo.setdefault(d.periodo, []).append(d)
    periodos = sorted(por_periodo, reverse=True)[:MESES_HISTORIA]

    def respuesta(seleccion: list[int]) -> dict[str, Any]:
        if not seleccion:
            return {"status": 404, "body": {"status": 404, "errorMessages": ["No se encontraron datos"]}}
        return {
            "status": 200,
            "body": {
                "status": 200,
                "results": {
                    "identificacion": int(cuit),
                    "denominacion": None,
                    "periodos": [
                        {
                            "periodo": str(p),
                            "entidades": [
                                {
                                    "entidad": d.entidad,
                                    "situacion": d.situacion,
                                    "monto": d.monto,
                                    "procesoJud": d.proceso_judicial,
                                }
                                for d in por_periodo[p]
                            ],
                        }
                        for p in seleccion
                    ],
                },
            },
        }

    return {"actual": respuesta([p for p in periodos[:1] if p == periodo_obs]), "historicas": respuesta(periodos)}


def mora_observada(deudas: Iterable[Deuda], despues_de: int) -> dict[date, int]:
    """Días de mora por mes posterior a ``despues_de``, según la peor situación informada.
    Un mes sin deuda informada no aparece (mora 0)."""
    peor: dict[int, int] = {}
    for d in deudas:
        if d.periodo > despues_de and d.situacion > 0:
            peor[d.periodo] = max(peor.get(d.periodo, 0), d.situacion)
    return {_fecha(p): DIAS_POR_SITUACION.get(s, 365) for p, s in peor.items()}


# --- datos extraídos del bucket ---------------------------------------------------


@dataclass
class Extraccion:
    """Lo que la demo necesita del bucket, ya filtrado a la muestra."""

    candidatos: dict[int, list[str]]  # por período de observación, CUIT en orden de muestreo
    deudas: dict[str, list[Deuda]]
    nacimientos: dict[str, date] = field(default_factory=dict)

    def guardar(self, archivo: Path) -> None:
        archivo.parent.mkdir(parents=True, exist_ok=True)
        datos = {
            "candidatos": {str(p): c for p, c in self.candidatos.items()},
            "deudas": {
                c: [[d.periodo, d.entidad, d.situacion, d.monto, d.proceso_judicial] for d in ds]
                for c, ds in self.deudas.items()
            },
            "nacimientos": {c: f.isoformat() for c, f in self.nacimientos.items()},
        }
        archivo.write_text(json.dumps(datos), encoding="utf-8")

    @classmethod
    def cargar(cls, archivo: Path) -> Extraccion:
        datos = json.loads(archivo.read_text(encoding="utf-8"))
        return cls(
            candidatos={int(p): c for p, c in datos["candidatos"].items()},
            deudas={c: [Deuda(*fila) for fila in ds] for c, ds in datos["deudas"].items()},
            nacimientos={c: date.fromisoformat(f) for c, f in datos["nacimientos"].items()},
        )


def extraer_del_bucket(
    periodos: Sequence[int],
    por_segmento: Mapping[str, int],
    perfil: str,
    semilla: int = SEMILLA,
    cache_candidatos: Path | None = None,
    copia_local: Path | None = None,
) -> Extraccion:  # pragma: no cover - necesita S3
    """Muestrea CUIT reservados de la central, por segmento según el prefijo, y trae su
    historia completa y su fecha de nacimiento. El muestreo lee de S3 solo la columna de
    CUIT y se guarda en ``cache_candidatos``. Buscar la muestra en los 24 meses por S3 es
    muy lento (muchos pedidos chicos), así que la historia y el padrón se leen de
    ``copia_local`` si se pasa (ver ``bajar_copia_local``)."""
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL aws; LOAD aws;")
    con.execute(f"CREATE SECRET (TYPE s3, PROVIDER credential_chain, PROFILE '{perfil}', REGION 'us-east-1')")

    candidatos: dict[int, list[str]] = {}
    if cache_candidatos and cache_candidatos.exists():
        candidatos = {int(p): c for p, c in json.loads(cache_candidatos.read_text()).items()}
    for periodo in (p for p in periodos if p not in candidatos):
        candidatos[periodo] = []
        for segmento, cantidad in por_segmento.items():
            prefijos = ", ".join(f"'{p}'" for p in PREFIJOS[segmento])
            filas = con.execute(
                f"""
                WITH c AS (
                    SELECT DISTINCT cuit FROM '{BUCKET}/deudores/periodo={periodo}/data.parquet'
                    WHERE left(CAST(cuit AS VARCHAR), 2) IN ({prefijos}) AND {sql_reservado()}
                )
                SELECT cuit FROM c
                ORDER BY md5(CAST(cuit AS VARCHAR) || '{semilla}:{periodo}')
                LIMIT {cantidad}
                """
            ).fetchall()
            candidatos[periodo] += [f"{cuit:011d}" for (cuit,) in filas]
        print(f"  {periodo}: {len(candidatos[periodo])} candidatos", flush=True)
        if cache_candidatos:
            cache_candidatos.parent.mkdir(parents=True, exist_ok=True)
            cache_candidatos.write_text(json.dumps({str(p): c for p, c in candidatos.items()}))

    origen = str(copia_local) if copia_local else BUCKET
    cuits = sorted({int(c) for cs in candidatos.values() for c in cs})
    con.execute("CREATE TEMP TABLE muestra (cuit BIGINT)")
    con.executemany("INSERT INTO muestra VALUES (?)", [(c,) for c in cuits])
    nombres = dict(
        con.execute(
            f"SELECT entidad, nombre FROM '{BUCKET}/entidades/periodo={PERIODO_CORTE}/maeent.parquet'"
        ).fetchall()
    )
    filas = con.execute(
        f"""
        SELECT d.cuit, CAST(d.periodo AS BIGINT), d.entidad, d.situacion, CAST(d.monto AS DOUBLE), d.flag = '1'
        FROM read_parquet('{origen}/deudores/periodo=*/data.parquet', hive_partitioning = true) d
        JOIN muestra USING (cuit)
        WHERE CAST(d.periodo AS BIGINT) < {PERIODO_CORTE}  -- el período sale de la carpeta, no del archivo
        UNION ALL
        SELECT m.cuit, {PERIODO_CORTE}, m.entidad, m.situacion,
               CAST(m.prestamos + m.garantias_otorgadas + m.otros_conceptos AS DOUBLE), m.proceso_judicial = 1
        FROM '{origen}/deudores_mensual/periodo={PERIODO_CORTE}/data.parquet' m
        JOIN muestra USING (cuit)
        """
    ).fetchall()
    deudas: dict[str, list[Deuda]] = {}
    for cuit, periodo, entidad, situacion, monto, judicial in filas:
        nombre = nombres.get(entidad, f"ENTIDAD {entidad}")
        deudas.setdefault(f"{cuit:011d}", []).append(Deuda(periodo, nombre, situacion, monto, bool(judicial)))
    nacimientos = {
        f"{cuit:011d}": date.fromisoformat(fecha)
        for cuit, fecha in con.execute(
            f"""
            SELECT p.cuit, p.fecha_nac_contrato FROM '{origen}/padron/corte={CORTE_PADRON}/padron.parquet' p
            JOIN muestra USING (cuit) WHERE p.fecha_nac_contrato IS NOT NULL
            """
        ).fetchall()
        if _fecha_valida(fecha)
    }
    print(f"  historia de {len(deudas)} CUIT, {len(nacimientos)} fechas de nacimiento", flush=True)
    return Extraccion(candidatos, deudas, nacimientos)


def bajar_copia_local(destino: Path, perfil: str) -> Path:  # pragma: no cover - necesita S3
    """Baja a ``destino`` lo que la extracción lee de los 24 meses: la central de cada mes,
    la mensual del corte y el padrón (unos 7 GB). ``aws s3 sync`` transfiere en paralelo y
    solo baja lo que falta, así que ante un corte de conexión se reintenta y retoma."""
    import subprocess

    def sync(origen: str, *filtro: str) -> None:
        for intento in range(1, REINTENTOS_DESCARGA + 1):
            comando = ["aws", "s3", "sync", f"{BUCKET}/{origen}", str(destino / origen), *filtro]
            if subprocess.run([*comando, "--profile", perfil, "--only-show-errors"]).returncode == 0:
                return
            print(f"  la descarga de {origen} se cortó (intento {intento}); se retoma", flush=True)
        raise RuntimeError(f"no se pudo bajar {origen} después de {REINTENTOS_DESCARGA} intentos")

    sync("deudores/", "--exclude", "*", "--include", "*/data.parquet")
    sync(f"deudores_mensual/periodo={PERIODO_CORTE}/")
    sync(f"padron/corte={CORTE_PADRON}/", "--exclude", "*", "--include", "padron.parquet")
    return destino


# --- armado de la base --------------------------------------------------------------


@dataclass(frozen=True)
class _Caso:
    cuit: str
    periodo_obs: int
    evaluada_en: datetime
    bureau: int


class _AdapterFijo:
    """Devuelve un crudo armado de antemano y lo traduce con el adapter real de la fuente."""

    def __init__(self, real: AdapterFuente, crudo: Any):
        self.nombre, self.variables, self._real, self._crudo = real.nombre, real.variables, real, crudo

    def consultar(self, identificacion: str, timeout: float) -> Any:
        return self._crudo

    def traducir(self, crudo: Any) -> Any:
        return self._real.traducir(crudo)


# El bucket no tiene cheques rechazados. Una fuente no disponible deriva el caso (ADR 0006),
# así que se asume lo más frecuente: sin cheques rechazados. El crudo lo dice.
SIN_CHEQUES = {"status": 404, "body": {"status": 404, "nota": "demo: el bucket no tiene cheques rechazados"}}


class _Reloj:
    def __init__(self) -> None:
        self.ahora = datetime(2000, 1, 1)

    def __call__(self) -> datetime:
        return self.ahora


def construir(
    repositorio: Repositorio,
    extraccion: Extraccion,
    badlar: Mapping[int, dict[str, Any]],
    pyme_por_periodo: int,
    mono_por_periodo: int,
    hasta: date,
    destino_cartera: Path,
    semilla: int = SEMILLA,
    arca: Path = SNAPSHOT_ARCA,
    igj: Path = SNAPSHOT_IGJ,
) -> dict[str, int]:
    """Da de alta y evalúa las solicitudes de la muestra, y escribe su cartera."""
    reloj = _Reloj()
    casos: dict[str, _Caso] = {}
    adapter_arca = Arca(arca)
    nulo = httpx.Client()

    def adapters(guardada) -> Mapping[str, AdapterFuente]:
        caso = casos[guardada.id]
        hoy = caso.evaluada_en.date
        crudo = crudo_cendeu(caso.cuit, extraccion.deudas.get(caso.cuit, []), caso.periodo_obs)
        lista: list[AdapterFuente] = [
            _AdapterFijo(Cendeu(nulo), crudo),
            _AdapterFijo(Cheques(nulo), SIN_CHEQUES),
            _AdapterFijo(Badlar(nulo), badlar[caso.periodo_obs]),
            Igj(igj, hoy=hoy),
            adapter_arca,
            _AdapterFijo(Bureau({}), {"score": caso.bureau, "sintetico": True}),
            IdentidadDeclarada(guardada.fecha_nacimiento, hoy=hoy),
            _AdapterFijo(Propios({}), {"lista_negra": False, "creditos_previos": 0}),
            Documentos(guardada.documentos),
        ]
        return {a.nombre: a for a in lista}

    servicio = Servicio(repositorio, adapters=adapters, ahora=reloj, dormir=lambda _: None)
    conteo = {"pyme": 0, "monotributista": 0}
    for periodo in sorted(extraccion.candidatos):
        cupo = {"pyme": pyme_por_periodo, "monotributista": mono_por_periodo}
        for cuit in extraccion.candidatos[periodo]:
            segmento = _segmento(cuit, adapter_arca)
            if segmento is None or cupo[segmento] == 0:
                continue
            rng = random.Random(f"{semilla}:{periodo}:{cuit}")
            deudas = [d for d in extraccion.deudas.get(cuit, []) if d.periodo <= periodo]
            deuda_pesos = sum(d.monto for d in deudas if d.periodo == periodo) * 1000
            peor_12m = max((d.situacion for d in deudas if d.periodo > periodo - 100), default=0)
            evaluada_en = datetime.combine(_sumar_meses(_fecha(periodo), 1), datetime.min.time()).replace(
                day=rng.randint(1, 28), hour=rng.randint(9, 18)
            )
            documentos, monto, plazo = _pedido(
                segmento, cuit, deuda_pesos, extraccion.nacimientos.get(cuit), evaluada_en.date(), adapter_arca, rng
            )
            reloj.ahora = evaluada_en
            id = f"demo-{periodo}-{cuit}"
            casos[id] = _Caso(cuit, periodo, evaluada_en, _bureau(peor_12m, bool(deudas), rng))
            servicio.alta(
                cuit,
                segmento,
                monto,
                plazo,
                documentos=documentos,
                consentimiento=True,
                origen="demo",
                id=id,
                fecha_nacimiento=extraccion.nacimientos.get(cuit) if segmento == "monotributista" else None,
            )
            servicio.evaluar(id)
            cupo[segmento] -= 1
            conteo[segmento] += 1

    base = otorgadas(repositorio)
    mora = {
        o.solicitud_id: mora_observada(
            extraccion.deudas.get(casos[o.solicitud_id].cuit, []), casos[o.solicitud_id].periodo_obs
        )
        for o in base
        if o.solicitud_id in casos
    }
    registros = generar(base, hasta, semilla, mora=mora)
    escribir_en_archivos(registros, destino_cartera)
    return conteo | {
        "otorgadas": len(base),
        "convertidas": sum(r.convertida for r in registros),
        "en_default": sum(r.en_default() for r in registros),
    }


def _segmento(cuit: str, arca: Arca) -> str | None:
    if cuit[:2] in PREFIJOS["pyme"]:
        return "pyme"
    if cuit[:2] in PREFIJOS["monotributista"]:
        resultado = arca.traducir(arca.consultar(cuit, 0))
        if isinstance(resultado, Disponible) and resultado.variables.get("es_monotributista"):
            return "monotributista"
    return None


def _pedido(
    segmento: str, cuit: str, deuda: float, nacimiento: date | None, hoy: date, arca: Arca, rng: random.Random
) -> tuple[dict[str, Any], float, int]:
    """Documentos (sintéticos salvo la categoría de ARCA), monto y plazo pedidos."""
    if segmento == "pyme":
        endeudamiento = math.exp(-1.9 + 0.7 * rng.gauss(0, 1))
        ventas = deuda / endeudamiento if deuda > 0 else math.exp(rng.gauss(math.log(300e6), 1.0))
        ventas = round(max(ventas, 12e6), -3)
        monto = round(ventas * rng.uniform(0.03, 0.15), -3)
        return (
            {"ventas_anuales": ventas, "cuit_coincide": True, "confianza_minima": 1.0},
            monto,
            rng.choice((12, 18, 24, 36)),
        )
    resultado = arca.traducir(arca.consultar(cuit, 0))
    categoria = resultado.variables.get("categoria_monotributo") if isinstance(resultado, Disponible) else None
    edad = (hoy - nacimiento).days / 365.25 if nacimiento else 40.0
    antiguedad = round(min(max(edad - 18, 0.5), math.exp(1.5 + 0.7 * rng.gauss(0, 1))), 1)
    ingreso = INGRESO_POR_CATEGORIA.get(categoria or "", 12) * 1e6
    monto = round(ingreso * rng.uniform(0.05, 0.30), -3)
    documentos = {
        "categoria_monotributo": categoria,
        "antiguedad_actividad_anios": antiguedad,
        "cuit_coincide": True,
        "confianza_minima": 1.0,
    }
    return documentos, monto, rng.choice((6, 12, 18, 24))


def _bureau(peor_situacion_12m: int, con_antecedentes: bool, rng: random.Random) -> int:
    """Score sintético, correlacionado con la peor situación real en la central."""
    base = {0: 640, 1: 690, 2: 570, 3: 480, 4: 430}.get(peor_situacion_12m, 400) if con_antecedentes else 610
    return int(min(999, max(300, round(base + rng.gauss(0, 70)))))


def badlar_historica(periodos: Iterable[int]) -> dict[int, dict[str, Any]]:  # pragma: no cover - red
    """El crudo de BADLAR que se habría consultado al evaluar cada período (el mes siguiente)."""
    with httpx.Client() as cliente:
        resultado = {}
        for periodo in periodos:
            hoy = _sumar_meses(_fecha(periodo), 1).replace(day=15)
            try:
                resultado[periodo] = Badlar(cliente, hoy=lambda h=hoy: h).consultar("", 20)
            except FuenteCaida as error:
                resultado[periodo] = {"status": 503, "body": {"error": str(error)}}
        return resultado


def _fecha(periodo: int) -> date:
    return date(periodo // 100, periodo % 100, 1)


def _sumar_meses(mes: date, meses: int) -> date:
    indice = mes.year * 12 + mes.month - 1 + meses
    return date(indice // 12, indice % 12 + 1, 1)


def _fecha_valida(texto: str) -> bool:
    try:
        return date.fromisoformat(texto).year >= 1900
    except ValueError:
        return False


# --- CLI ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - necesita S3
    parser = argparse.ArgumentParser(description="Base de demo con solicitantes y mora reales de la central.")
    parser.add_argument("--db", type=Path, default=Path("data/demo.sqlite"))
    parser.add_argument("--destino", type=Path, default=Path("data/demo/cartera"))
    parser.add_argument("--cache", type=Path, default=Path("data/demo/extraccion.json"))
    parser.add_argument("--perfil", default="fmargara", help="perfil de AWS con lectura del bucket")
    parser.add_argument("--pyme", type=int, default=150, help="solicitudes PyME por período")
    parser.add_argument("--mono", type=int, default=300, help="solicitudes de monotributistas por período")
    parser.add_argument("--semilla", type=int, default=SEMILLA)
    parser.add_argument("--rehacer", action="store_true", help="vuelve a leer el bucket aunque haya cache")
    args = parser.parse_args(argv)

    if args.db.exists():
        raise SystemExit(f"{args.db} ya existe: borrala antes de regenerar la demo")
    if args.cache.exists() and not args.rehacer:
        extraccion = Extraccion.cargar(args.cache)
    else:
        # pocas personas humanas son monotributistas: se muestrea de más y se filtra con ARCA
        por_segmento = {"pyme": args.pyme * 2, "monotributista": args.mono * 12}
        # copia temporal fuera del repo, en una carpeta fija para retomar si algo falla;
        # se borra recién cuando la extracción quedó guardada
        copia = Path(tempfile.gettempdir()) / "relevo-cendeu"
        print(f"bajando la central a {copia}", flush=True)
        bajar_copia_local(copia, args.perfil)
        extraccion = extraer_del_bucket(
            PERIODOS_OBS, por_segmento, args.perfil, args.semilla, args.cache.with_suffix(".candidatos.json"), copia
        )
        extraccion.guardar(args.cache)
        shutil.rmtree(copia, ignore_errors=True)
    conteo = construir(
        Repositorio(args.db),
        extraccion,
        badlar_historica(extraccion.candidatos),
        args.pyme,
        args.mono,
        hasta=date(PERIODO_CORTE // 100, PERIODO_CORTE % 100, 30),
        destino_cartera=args.destino,
        semilla=args.semilla,
    )
    print(f"{args.db}: {conteo}")
    print(f"cartera en {args.destino}")


if __name__ == "__main__":  # pragma: no cover
    main()
