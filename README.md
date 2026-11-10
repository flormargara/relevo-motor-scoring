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

## Correr la app

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/uvicorn relevo.app:crear_app --factory --reload
```

Abrir http://localhost:8000. La base queda en `data/relevo.sqlite` (cambiarla con `RELEVO_DB`). Al arrancar se cargan los casos de `config/casos_precargados.yaml` y, si existe, de `data/casos_precargados.local.yaml` (casos con CUIL de personas, fuera de git); se evalúan con **Evaluar pendientes**.

## Agentes

Los agentes leen los documentos subidos en el alta, redactan el informe de riesgo y responden preguntas en la pantalla del caso. Nunca deciden: si fallan, el veredicto sale igual.

Por defecto usan `claude -p` (Claude Code con tu suscripción; hace falta haber iniciado sesión con `claude`). Para usar la API de Anthropic:

```bash
.venv/bin/pip install -e '.[dev,api]'
echo 'RELEVO_AGENTE=api' >> .env
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env
```

`RELEVO_AGENTE` acepta `cli` (por defecto), `api` o `ninguno`, y `RELEVO_MODELO` elige el modelo. En `ejemplos/documentos/` hay un balance y una constancia sintéticos para probar el agente documental. Los archivos subidos quedan en `data/documentos/<solicitud>/` y se borran con la solicitud.

## Correr los tests

```bash
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
