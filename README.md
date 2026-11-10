# Relevo AI — Motor de decisión de riesgo

Prototipo de un motor de decisión de crédito para PyMEs y monotributistas:

**inputs → killers → probabilidad de default → política → veredicto**

Consulta fuentes reales (BCRA Central de Deudores, BADLAR) y fuentes de juguete. Los agentes de IA leen documentos y explican cada decisión al analista.

El alcance acordado está en [docs/acuerdos-demo-v1.md](docs/acuerdos-demo-v1.md).

## Estructura

- `src/relevo/`: el código. `motor.evaluar` es la entrada del motor (ver [docs/diseno-modulos.md](docs/diseno-modulos.md)).
- `rules/`: reglas versionadas por segmento (`pyme.v1.yaml`, `monotributista.v1.yaml`).
- `config/`: configuración técnica de las fuentes (`fuentes.yaml`) y datos de las fuentes de juguete (`juguete.yaml`).
- `modelos/`: puntos del scorecard por segmento (provisionales hasta entrenar con la cartera sintética).
- `tests/`: pytest.

## Correr los tests

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Las pruebas contra la API real del BCRA son opcionales:

```bash
RELEVO_EN_VIVO=1 .venv/bin/pytest -k en_vivo
```

## Snapshot de IGJ

IGJ se consulta desde un snapshot local que se arma una vez a partir del ZIP semestral publicado en [datos.jus.gob.ar](https://datos.jus.gob.ar/dataset/entidades-constituidas-en-la-inspeccion-general-de-justicia-igj) (unos 160 MB):

```bash
.venv/bin/python -m relevo.fuentes.igj igj-2026-semestre-2.zip data/igj.sqlite
```

Sin el snapshot, la fuente IGJ queda *no disponible* y las evaluaciones PyME se derivan.

## Scorecards

Los modelos se entrenan sobre una cartera sintética (ver `src/relevo/sintetico.py`):

```bash
.venv/bin/pip install -e '.[dev,sintetico]'
.venv/bin/python -m relevo.sintetico pyme --version 2
.venv/bin/python -m relevo.sintetico monotributista --version 2
```
