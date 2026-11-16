# Relevo AI — Motor de decisión de riesgo

Prototipo de un motor de decisión de crédito para PyMEs y monotributistas:

**inputs → killers → probabilidad de default → política → veredicto**

Consulta fuentes reales (BCRA Central de Deudores, BADLAR) y fuentes de juguete. Los agentes de IA leen documentos y explican cada decisión al analista.

El alcance acordado está en [docs/acuerdos-demo-v1.md](docs/acuerdos-demo-v1.md).

## Estructura

- `src/relevo/`: el código. `motor.evaluar` es la entrada del motor (ver [docs/diseno-modulos.md](docs/diseno-modulos.md)). El flujo del motor y sus inputs están en [docs/motor-flujo.md](docs/motor-flujo.md) y el esquema de la base en [docs/der.md](docs/der.md).
- `rules/`: semilla de las reglas de cada flujo (`pyme.v3.yaml`, `monotributista.v3.yaml`). Las reglas viven en la base y se editan desde la pestaña **Backoffice**: al arrancar se importan las versiones de `rules/` que falten, sin pisar ninguna (ADR 0007).
- `config/flujos.yaml`: los flujos del motor y las fuentes que consulta cada uno (de ahí sale el catálogo de variables que pueden usar sus reglas).
- `config/`: configuración técnica de las fuentes (`fuentes.yaml`) y datos de las fuentes de juguete (`juguete.yaml`).
- `modelos/`: el scorecard de cada segmento, con sus ratings y topes de PD (los ratings son del modelo, no se editan desde la app).
- `tests/`: pytest.

## Correr la app

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/uvicorn relevo.app:crear_app --factory --reload
```

Abrir http://localhost:8000. La base queda en `data/relevo.sqlite` (cambiarla con `RELEVO_DB`) y la cartera se lee de `data/cartera/` (cambiarla con `RELEVO_CARTERA`). Al arrancar se cargan los casos de `config/casos_precargados.yaml` y, si existe, de `data/casos_precargados.local.yaml` (casos con CUIL de personas, fuera de git), salvo en la base de demo; se evalúan con **Evaluar pendientes**.

## API del motor

La app expone el motor como API JSON en `/api/v1` (ADR 0008). La documentación interactiva está en http://localhost:8000/docs y el contrato en `/openapi.json`. Las pantallas no forman parte de ese contrato.

| Método | Ruta | Qué hace |
|---|---|---|
| `POST` | `/api/v1/evaluaciones` | Da de alta una solicitud, la evalúa (consulta las fuentes en vivo) y devuelve la evaluación |
| `GET` | `/api/v1/evaluaciones/{id}` | Una evaluación guardada |
| `GET` | `/api/v1/solicitudes/{id}/evaluaciones` | El historial de evaluaciones de una solicitud |
| `POST` | `/api/v1/solicitudes/{id}/evaluaciones` | Vuelve a evaluar con las reglas vigentes |
| `GET` | `/api/v1/reglas/{flujo}` | Reglas publicadas, ratings del modelo y catálogo de variables |

```bash
curl -s localhost:8000/api/v1/evaluaciones -H 'content-type: application/json' -d '{
  "cuit": "30-71234567-1", "segmento": "pyme", "monto": 10000000, "plazo_meses": 12,
  "documentos": {"ventas_anuales": 240000000}
}'
```

Las solicitudes de la API aparecen en el Histórico como las demás. **No tiene autenticación**: sirve para uso local y pruebas, no para exponerla a terceros.

## Agentes

Los agentes leen los documentos subidos en el alta, redactan el informe de riesgo, responden preguntas en la pantalla del caso y sobre toda la base en **Chat with your data**. Nunca deciden: si fallan, el veredicto sale igual.

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

Los tests del scorecard (`test_sintetico.py`) se saltean si no está instalado el extra `sintetico` (`pip install -e '.[dev,sintetico]'`).

En GitHub, el workflow de CI (`.github/workflows/ci.yml`) corre `ruff check`, `ruff format --check` y los tests en cada PR y en cada push a `main`. No necesita `data/` ni credenciales. Antes de subir, conviene correr lo mismo localmente:

```bash
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/pytest
```

Las pruebas contra la API real del BCRA son opcionales:

```bash
RELEVO_EN_VIVO=1 .venv/bin/pytest -k en_vivo
```

## Datos en S3

`data/` está fuera de git (unos 560 MB, con padrones y documentos de ejemplo). Se comparte por el bucket privado `s3://relevo-motor-scoring/data/`. Hace falta AWS CLI y un usuario IAM con acceso al bucket, que da quien lo administra:

```bash
brew install awscli
aws configure --profile relevo-s3   # pegar las claves que te dieron; región us-east-1
scripts/datos_s3.sh bajar
```

`bajar` trae lo que falta o cambió; con `--dryrun` muestra qué haría sin tocar nada. Quien actualiza un snapshot (ARCA cada tanto, IGJ cada semestre) lo regenera y corre `scripts/datos_s3.sh subir`, y avisa al equipo para que vuelvan a bajar.

- `relevo.sqlite` no se sincroniza: es la base de trabajo de cada uno (solicitudes y evaluaciones). Tampoco la demo con datos reales (`demo.sqlite` y `demo/`).
- El script nunca borra archivos, ni en el bucket ni en `data/`.
- Las claves van solo en `~/.aws/credentials`: nunca en el repo ni en `.env`.
- Variables opcionales: `RELEVO_S3_BUCKET`, `RELEVO_S3_PREFIJO` y `RELEVO_AWS_PERFIL`.

## Snapshot de IGJ

IGJ se consulta desde un snapshot local que se arma una vez a partir del ZIP semestral publicado en [datos.jus.gob.ar](https://datos.jus.gob.ar/dataset/entidades-constituidas-en-la-inspeccion-general-de-justicia-igj) (unos 160 MB):

```bash
.venv/bin/python -m relevo.fuentes.igj igj-2026-semestre-2.zip data/igj.sqlite
```

Sin el snapshot, la fuente IGJ queda *no disponible* y las evaluaciones PyME se derivan.

## Snapshot de ARCA

La condición tributaria (Ganancias, IVA, categoría de monotributo, empleador) sale del padrón público de ARCA, que se descarga sin autenticación desde [afip.gob.ar](https://www.afip.gob.ar/genericos/cInscripcion/archivoCompleto.asp) ("Archivo condición tributaria con denominación", unos 86 MB, 6,2 millones de CUIT). Se arma en unos 10 segundos y ocupa unos 300 MB:

```bash
curl -L -o data/arca/padron.zip https://www.afip.gob.ar/genericos/cInscripcion/archivos/apellidoNombreDenominacion.zip
.venv/bin/python -m relevo.fuentes.arca data/arca/padron.zip data/arca.sqlite
```

ARCA actualiza el archivo periódicamente; la fecha del padrón queda en la variable `arca.fecha_padron`. Sin el snapshot, la fuente queda *no disponible*.

## Scorecards

Los modelos se entrenan sobre una cartera sintética (ver `src/relevo/sintetico.py`):

```bash
.venv/bin/pip install -e '.[dev,sintetico]'
.venv/bin/python -m relevo.sintetico pyme --version 2
.venv/bin/python -m relevo.sintetico monotributista --version 2
```

## Cartera sintética

La conversión, el desembolso, los pagos y la mora son de la entidad (ADR 0008). Para probar el monitoreo y el simulador se inventan a partir de las solicitudes otorgadas de la base, coherentes con la PD de cada evaluación y con un deterioro plantado en el rating C:

```bash
.venv/bin/python -m relevo.sintetico_cartera --hasta 2026-11-15
```

Escribe tres CSV en `data/cartera/` (`conversiones`, `desembolsos`, `estados`), que se leen con `CarteraEnArchivos`, el mismo adapter que usaría una entidad que entrega archivos.

## Monitoreo

La pestaña **Monitoreo** cruza las solicitudes otorgadas con la cartera (`RELEVO_CARTERA`) y compara el default observado con la PD del scorecard, por rating, cohorte de desembolso, segmento y versión de reglas (`relevo/monitoreo.py`):

- **O/E**: defaults observados (90+ días en los primeros 12 meses) sobre esperados. Como casi ningún crédito tiene 12 meses, cada uno aporta su PD llevada a los meses observados con hazard constante, `1 − (1 − PD)^(m/12)`. Los créditos con menos de 3 meses no entran.
- **Estado**: *desvío* si O/E > 1,2 y *subestima* si O/E > 1,5, solo cuando el piso del intervalo de Poisson al 95% supera 1; con menos de 30 créditos, *pocos datos*.
- **Mora temprana**: 30+ días en los primeros 3 meses. Alerta si alguna de las 3 últimas cohortes de un rating la tiene 1,5 veces mayor que las anteriores (test de dos proporciones).

Los números se calculan sin agente. Cada alerta tiene un enlace para preguntarla en **Chat with your data**.

## Chat with your data

La pestaña **Chat with your data** contesta preguntas sobre las evaluaciones, la cartera y las políticas (`relevo/chat_datos.py`). El agente nunca hace cuentas. Cada pregunta pasa por tres pasos:

1. **Interpretar.** El agente traduce la pregunta a un pedido de un catálogo cerrado: `monitoreo`, `evaluaciones` o `simulacion`. Si no entra en ninguno, lo dice.
2. **Ejecutar.** El código valida el pedido y corre el análisis. Si el pedido falla (un cambio que no se puede aplicar, una variable fuera del catálogo), le pide al agente que lo corrija una vez.
3. **Explicar.** El agente explica el resultado. Las tablas se dibujan desde los datos, no desde su texto.

**Simulador** (`relevo/simulador.py`). Vuelve a correr el motor sobre la última evaluación de cada solicitud del flujo, con las reglas vigentes y con las candidatas:

- **Solo lectura.** Usa los snapshots guardados: no consulta fuentes, así que no tiene costo, y no escribe nada.
- **Fuentes faltantes.** Si una regla candidata necesita una fuente que en su momento no se consultó, la solicitud queda fuera de la comparación y se informa.
- **De dónde salen las candidatas.** Son cambios del agente a una copia de las vigentes (`aplicar_cambios`), el borrador del editor o una versión publicada.
- **Qué compara.**
  - Aprobación y monto.
  - PD del scorecard y PD corregida por el O/E de cada rating.
  - El default real de los que salen.
  - La PD estimada de los que entran, que nunca tuvieron crédito.
- **Guardar como borrador.** Lleva los cambios simulados al editor de Backoffice. Publicarlos sigue siendo una acción del analista.

La conversación no se guarda: vive en la pestaña del navegador.

## Demo con datos reales

Una base aparte con solicitantes y mora reales de la Central de Deudores (ADR 0009). Necesita el extra `demo` y un perfil de AWS con lectura del bucket `data-cendeu-raw`:

```bash
.venv/bin/pip install -e '.[dev,demo]'
.venv/bin/python -m relevo.demo_real --perfil fmargara
RELEVO_DB=data/demo.sqlite RELEVO_CARTERA=data/demo/cartera .venv/bin/uvicorn relevo.app:crear_app --factory
```

Deja la base en `data/demo.sqlite` y la cartera en `data/demo/cartera/`. La app no carga los casos precargados en esta base: solo lleva CUIT reservados. Los CUIT son reales: nada de eso va a git ni al bucket compartido.
