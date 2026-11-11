# Diseño de módulos (v1)

Vocabulario de diseño: *module*, *interface*, *seam*, *adapter* y *depth*, según la skill codebase-design. Los términos de dominio vienen de [GLOSSARY.md](../GLOSSARY.md).

## Mapa

```
            app (FastAPI + páginas)
             │        │          │
             ▼        ▼          ▼
         ┌───────┐ ┌────────┐ ┌──────────────┐
         │ motor │ │agentes │ │ repositorio  │
         └───┬───┘ └───┬────┘ └──────────────┘
             │         │
   seam ─────┼─────    seam ───────────
   Consultor │         ClienteClaude
             ▼         ▼
   fuentes/*          claude -p | API Anthropic
   (CENDEU, BADLAR, IGJ, ARCA, bureau, identidad, propios, documentos, fake)
```

## 1. `motor`: el módulo profundo

**Interface:**

```python
evaluar(solicitud: Solicitud, reglas: Reglas, consultor: Consultor) -> Evaluacion
```

Es una sola función. Detrás de ella quedan:
- el orden de los killers y el corte en el primero que salta;
- el manejo de reglas no evaluables (`continuar` / `derivar` / `rechazar`);
- la precedencia `RECHAZADO > DERIVAR > APROBADO`;
- el scorecard (puntos → PD), el rating y la política;
- la capacidad de pago, el monto mínimo y la cuota en sistema francés;
- la traza completa.

**Invariantes:**
- Es determinístico: misma solicitud + mismas reglas + mismos snapshots → misma evaluación.
- No lanza excepciones por fallas de fuentes: todo se convierte en veredicto y queda en la traza.
- Nunca pide una fuente paga si la evaluación ya se decidió.

**Tests:** todos pasan por `evaluar`, con un `Consultor` falso que devuelve snapshots fijos. No hay tests de las piezas internas salvo el evaluador de condiciones, que es un *internal seam* con casos propios.

## 2. Seam `Consultor`: acceso a fuentes

```python
class Consultor(Protocol):
    def variables(self, fuente: str) -> ResultadoFuente: ...
    def snapshots(self) -> list[Snapshot]: ...

ResultadoFuente = Disponible(variables: dict) | SinAntecedentes() | NoDisponible(motivo: str)
```

El motor pide **variables** por nombre de fuente; nunca ve HTTP. Se piden de forma perezosa: la primera vez que una regla usa `cendeu.peor_situacion_12m`, el consultor consulta CENDEU.

**Adapters:**
- **`ConsultorReal`**: tiene un registro de *adapters de fuente* y aplica la configuración de cada uno:
  - las fuentes gratuitas se precargan en paralelo al construirlo;
  - las fuentes pagas se consultan solo cuando se piden;
  - aplica reintentos y timeout;
  - reutiliza snapshots pagos de evaluaciones anteriores de la misma solicitud (según los días configurados).
- **`ConsultorFalso`**: devuelve variables fijas y se usa en los tests del motor.

Cada **adapter de fuente** hace dos cosas: `consultar(identificacion) -> crudo` (HTTP, archivo o juguete) y `traducir(crudo) -> variables`. Las traducciones (por ejemplo, el JSON de CENDEU → `peor_situacion_12m`, `entidades_con_deuda`, `deuda_total`) se testean contra snapshots reales guardados como fixtures.

Los documentos extraídos por el agente documental entran como una fuente más (`documentos`, gratuita). Así el motor no distingue entre datos que vienen de una API y datos que vienen de un PDF.

## 3. `reglas`, `versiones_reglas` y `catalogo`

```python
VersionesReglas.reglas(flujo: str, version: int | None = None) -> Reglas   # la vigente o la pedida
VersionesReglas.publicar(flujo, contenido, autor, motivo, base) -> Reglas
VersionesReglas.cambios(flujo, contenido) -> list[Cambio]                  # contra la vigente
catalogo(flujo) -> tuple[VariableCatalogo, ...]
validar_condicion(flujo, texto) -> Validacion
```

Las reglas son de un **flujo** (segmento + tipo de flujo, `config/flujos.yaml`) y viven en la base, versionadas (ADR 0007). Los YAML de `rules/` son la semilla: `importar` trae al arrancar las versiones que faltan, sin pisar ninguna. `reglas_desde_dict` valida la estructura al cargar: si algo está mal, el error aparece en ese momento y no en medio de una evaluación. Las condiciones usan un lenguaje acotado (`cendeu.peor_situacion_12m >= 3 and propios.lista_negra`), interpretado sobre un AST con una lista blanca de operaciones. Nunca se usa `eval`.

Las reglas referencian el modelo del scorecard (`modelos/<segmento>.v<N>.json`, generado offline con el script de entrenamiento), que trae también los ratings y sus topes de PD: son del modelo, no de las reglas.

Las condiciones de un flujo solo pueden usar variables de su **catálogo**: las de la solicitud más las que declara cada fuente que el flujo consulta (cada adapter declara `variables`, con un test de contrato contra lo que devuelve).

## 4. `agentes`

```python
extraer_documento(archivo: Path, cliente: ClienteClaude) -> DocumentoExtraido
variables_documentos(documentos, segmento, cuit, hoy) -> dict | None
redactar_informe(evaluacion: Evaluacion, cliente: ClienteClaude) -> str
responder(evaluacion: Evaluacion, historial: list[Turno], pregunta: str, cliente: ClienteClaude) -> str
```

**Seam `ClienteClaude`:** `completar(prompt: str, archivos: Sequence[Path] = (), sistema: str | None = None) -> str`. Tiene cuatro adapters: `ClaudeCLI` (`claude -p`, el default según el ADR 0004), `ClaudeAPI`, `SinAgente` (no configurado) y `ClaudeFalso` para los tests. Toda falla es `AgenteNoDisponible`.

## 5. `repositorio`

Guarda en SQLite: solicitudes, evaluaciones (con snapshots, traza, versión de reglas y veredicto), informes, chat, resoluciones del analista, y las versiones y el borrador de las reglas de cada flujo. También lista la bandeja y borra una solicitud. Tiene un solo adapter (SQLite; los tests usan `:memory:`), así que no lleva seam propio.

## 6. `app`

FastAPI más plantillas Jinja y JS sin frameworks. Tiene cuatro pantallas: Bandeja, Caso, Reglas (el editor de cada flujo) e Historial de reglas. Es delgada a propósito: arma la solicitud, llama a `evaluar`, guarda el resultado y lo muestra.

## 7. `sintetico` (offline)

Genera las carteras sintéticas por segmento, entrena el scorecard logístico, exporta los coeficientes y crea los documentos de ejemplo. No es parte del runtime.

## Orden de construcción (TDD, de adentro hacia afuera)

1. `motor` + `reglas`, con `ConsultorFalso`: es donde está el valor.
2. Adapters de fuente: CENDEU y BADLAR reales, más los de juguete; después `ConsultorReal`.
3. `sintetico`: entrenar el scorecard y reemplazar los coeficientes provisionales.
4. `repositorio` + `app`.
5. `agentes` (CLI, después API).

## Decisiones tomadas al construir el paso 1

- **`solicitud` es una pseudo-fuente** que resuelve el propio motor: `solicitud.cuit_valido` (prefijo + dígito verificador), `solicitud.tipo_persona` (`humana` / `juridica`), `monto`, `plazo_meses`. Así el killer de identificación inválida se escribe en el YAML como cualquier otro y no consulta nada.
- **Los cheques rechazados son una fuente aparte (`cheques`)**, aunque vengan del mismo servicio que CENDEU. Cada endpoint del BCRA responde 404 por su cuenta, y mezclarlos rompía la semántica de *sin antecedentes* (ADR 0003).
- **Semántica de los datos faltantes** dentro de una condición:
  - fuente *sin antecedentes*, o variable con valor `null` → **sin dato**: el killer o la derivación no salta, y en el scorecard se usan los puntos `sin_dato` de la característica;
  - fuente *no disponible*, variable ausente o tipo de dato inesperado → **no evaluable**: se aplica `si_no_disponible` de la regla (ADR 0006);
  - `fuente.sin_antecedentes` siempre se puede preguntar, y vale `true` o `false`.
- **Derivaciones**: sección propia del YAML. No cortan: marcan la evaluación y siguen, así un rechazo posterior (rating E, monto mínimo) gana por precedencia. Si nada rechaza, la oferta calculada queda como propuesta para el analista.
- **Scorecard**: escala estándar (`puntos_base`, `odds_base`, `pdo`), tramos por característica (`hasta` inclusive, o `valor` exacto) y puntos `sin_dato`. Una característica puede ser una expresión (`cendeu.deuda_total / documentos.ventas_anuales`). Los modelos `v1` están marcados `provisional: true` hasta el paso 3.
- **Política sin datos** (BADLAR caída, sin balance, categoría desconocida) → `DERIVAR` sin oferta (o `RECHAZADO` si `politica.si_no_disponible: rechazar`).

## Decisiones tomadas al construir el paso 2

- **Adapters de fuente** (`src/relevo/fuentes/`): `consultar` trae el crudo (lo que se guarda como snapshot) y `traducir` es una función pura, testeada contra respuestas reales del BCRA en `tests/fixtures/bcra/`. Un timeout, 5xx o error de red es `FuenteCaida` (se reintenta); un 4xx es una respuesta válida.
- **CENDEU** combina `/Deudas` y `/Deudas/Historicas`. Los montos llegan en **miles de pesos** y se exponen en pesos. En la histórica, situación `0` = sin deuda ese mes (se ignora). Variables: `peor_situacion_12m`, `peor_situacion_actual`, `deuda_irregular_12m` (mayor deuda en situación ≥ 3 de un mes), `proporcion_irregular_12m`, `entidades_con_deuda`, `deuda_total`, `en_proceso_judicial`, `identificacion_rechazada` (HTTP 400).
- **Materialidad en el killer de situación BCRA** (datos reales: YPF figura en situación 5 por $86.000 con Telepeaje, sobre más de $1 billón de deuda). El killer pide situación ≥ 3 **y** deuda irregular ≥ $500.000 o ≥ 5 % de la deuda del mes. Los umbrales están en el YAML y son una decisión de riesgo a validar.
- **El 400 de CENDEU** es un killer propio (`identificacion_rechazada_por_bcra`), así el killer de identificación inválida no consulta ninguna fuente.
- **BADLAR** se publica con unos días de demora: se consulta una ventana de 30 días y se toma el último valor. Si cae, se usa el último guardado con `desactualizada: true` (queda en la traza).
- **IGJ** es un snapshot SQLite (`data/igj.sqlite`, no versionado) armado desde el ZIP semestral de datos.jus.gob.ar. Limitaciones: solo sociedades de CABA (otras jurisdicciones → *sin antecedentes*), y no publica la fecha de constitución, así que la antigüedad se aproxima con el primer balance o asamblea registrados (cota inferior; ~74 % de los CUIT no tiene ninguno → *sin dato*).
- **Cheques rechazados**: el fixture es la respuesta real de Celulosa Argentina (`30501062150`, 1652 cheques sin fondos de 2025). Un cheque cuenta como impago si no tiene `fechaPago`. En las respuestas reales `estadoMulta` y `denomJuridica` vienen en `null` y `fechaPagoMulta` viene cargada aunque el cheque siga impago, así que no sirven para decidir si se pagó.
- **`ConsultorReal`**: precarga en paralelo las fuentes gratuitas que usan las reglas; las pagas se consultan solo cuando el motor las pide. La configuración técnica está en `config/fuentes.yaml`. Los snapshots entre evaluaciones viven detrás del seam `AlmacenSnapshots` (hoy en memoria; en el paso 4, SQLite).

## Fuente ARCA (padrón público)

- **Snapshot SQLite** (`data/arca.sqlite`, no versionado) armado desde el archivo público de condición tributaria de ARCA, sin certificado. Se eligió sobre los web services con certificado WSAA para no depender del trámite ni de custodiar una clave privada; los web services quedan para la fase 2 (traen fecha de inicio y actividades, que el padrón no tiene).
- **Formato**: ancho fijo, latin-1, 51 caracteres por línea. La página de ARCA ubica la actividad del monotributo en 51/52, pero en el archivo real está en 50/51.
- **Variables**: `denominacion`, `ganancias` e `iva` (`activo`, `no_inscripto`, `exento`…), `es_monotributista`, `categoria_monotributo` (A–K; las variantes social, promovido, primaria y cooperativa son A, o B), `tipo_monotributo`, `actividad_monotributo`, `empleador`, `inscripcion_vigente` (algún impuesto activo o exento, o monotributo) y `fecha_padron`. Un campo en blanco es *sin dato*; un CUIT que no figura es *sin antecedentes*.
- Dato real: Garbarino (en concurso) figura como no inscripta en Ganancias ni en IVA, así que `inscripcion_vigente` es falso.
- Por ahora ninguna regla usa `arca.*`, así que el consultor no la precarga.

## Decisiones tomadas al construir el paso 3

- **Población de entrenamiento = solicitantes que ya pasaron los killers**, que es la que el scorecard ve en producción. Incluye los mismos faltantes que producen los adapters: CENDEU sin antecedentes (12 % PyME, 20 % monotributistas, algo más riesgosos), IGJ sin dato (65 %) y bureau caído (4 %).
- **Proceso generador**: un factor latente de riesgo, que el modelo no ve, mueve a la vez las características y el default a 12 meses. En monotributistas, la edad tiene además un efecto propio (más riesgo antes de los 25).
- **Tramos fijos definidos por dominio** (no optimizados), así los puntos son monótonos y explicables. El faltante es un tramo más y sus puntos son los `sin_dato`; si una característica no tiene faltantes en la cartera, `sin_dato` vale lo mismo que WoE 0 (neutro).
- **WoE → regresión logística → puntos** con la escala 600 puntos = odds 50:1, PDO 20. Los puntos se redondean a enteros.
- **Versionado**: el modelo entrenado es `v2` (`rules/<segmento>.v2.yaml` → `modelos/<segmento>.v2.json`) y la `v1` provisional queda para reproducir evaluaciones viejas. Los tests del motor usan la `v1` a propósito, porque prueban el motor y no un modelo. Un test verifica que los JSON versionados se regeneran idénticos con su semilla.
- **Métricas en validación** (30 %, guardadas en el JSON): PyME AUC 0,74 / KS 0,37; monotributistas AUC 0,73 / KS 0,35. El default observado por rating coincide con la PD media y crece de A a E.
- **Es una cartera sintética**: los puntos sirven para la demo, no para decidir créditos reales.

## Decisiones tomadas al construir el paso 4

- **`repositorio`**: SQLite con solicitudes, evaluaciones (traza, motivos, oferta, versión de reglas), snapshots (crudo + resultado traducido + origen e intentos), informes, chat y resoluciones. Borrar una solicitud borra todo en cascada. Una evaluación guardada se reconstruye idéntica y se puede **reproducir**: misma solicitud + mismas reglas + sus snapshots → mismo veredicto y misma traza (hay un test).
- **`AlmacenSqlite`** implementa `AlmacenSnapshots` sobre la tabla de snapshots: el bureau se reutiliza entre evaluaciones de la misma solicitud y BADLAR cae al último valor real de cualquier evaluación. Nunca reutiliza un valor que ya era de respaldo.
- **`servicio`** concentra los casos de uso (alta, evaluar, precargados), así la app queda delgada. El alta de un CUIL de persona humana **exige consentimiento** (Ley 25.326). Un CUIT mal formado se acepta y lo rechaza el motor, para que quede en la traza.
- **Casos precargados**: `config/casos_precargados.yaml` (empresas, versionado) + `data/casos_precargados.local.yaml` (personas, fuera de git). Se dan de alta al arrancar pero no se evalúan solos, porque evaluar consulta en vivo.
- **App**: FastAPI + Jinja, formularios con POST-redirect-GET y casi nada de JS. La resolución del analista solo se registra sobre la última evaluación y solo si fue `DERIVAR`; se muestra aparte y nunca reemplaza el veredicto.
- **Documentos**: hasta el paso 5 los campos se cargan a mano en el alta (con `confianza_minima: 1`).

## Decisiones tomadas al construir el paso 5

- **`ClienteClaude`** suma un `sistema` opcional: en la CLI reemplaza el system prompt de Claude Code (`--system-prompt`) y en la API va como `system`. Toda falla (CLI ausente, sesión vencida, timeout, error de la API, modelo que declina) es `AgenteNoDisponible`.
- **`ClaudeCLI`** corre `claude -p --output-format json` en `--safe-mode` (sin CLAUDE.md, hooks ni MCP del usuario), sin guardar la sesión, en un directorio neutro, y solo con la herramienta Read cuando hay archivos (`--add-dir` al directorio del archivo). Quita `ANTHROPIC_API_KEY` del entorno para usar la suscripción.
- **`ClaudeAPI`** usa el SDK oficial (`.[api]`), `claude-haiku-5-5` con esfuerzo `medium`, y `fallbacks: "default"`: si el modelo declina por seguridad, la API reintenta con otro modelo en la misma llamada. PDF e imágenes van como bloques base64; los `.txt`/`.md`, como texto.
- **Agente documental**: identifica el tipo (`constancia_arca`, `balance`, `otro`) y devuelve cada campo con su confianza. El código valida y normaliza (CUIT a 11 dígitos, categoría A–K, fechas ISO, montos en formato argentino); un valor inválido o ausente queda *sin dato* con confianza 0. La antigüedad de la actividad se calcula en código a partir de la fecha de inicio, no la calcula el modelo.
- **De documentos a variables**: por variable gana el valor con mayor confianza entre los documentos. `confianza_minima` es la menor entre las variables que usa el segmento (0 si falta alguna), así la derivación `documentos_baja_confianza` de las reglas cubre también los campos faltantes. Si un documento trae un CUIT distinto al de la solicitud, `cuit_coincide: false` y la confianza baja a 0.
- **Carga manual**: es un documento más (`carga manual`, confianza 1) y por eso pisa a lo extraído. Los casos precargados siguen trayendo las variables armadas.
- **Persistencia**: tabla `documentos` (archivo, ruta, extracción o error), en cascada con la solicitud. Los archivos quedan en `data/documentos/<solicitud>/` y `Servicio.borrar` los elimina. Si el agente falla en el alta, la solicitud se da de alta igual (sin documentos → *sin antecedentes* → deriva) y se puede **volver a extraer** desde el caso; eso no reevalúa.
- **Informe y chat** se piden con un botón y quedan atados a una evaluación. El agente recibe la evaluación como JSON (traza, variables traducidas de cada fuente, oferta), sin las respuestas crudas. El chat se guarda solo si hubo respuesta. Se generan a pedido, no al evaluar, porque `claude -p` tarda y la evaluación no debe esperar al agente.

## Decisiones tomadas al construir el editor de reglas (ADR 0007)

- **Ratings del modelo**: los topes de PD de cada rating pasaron de `rules/*.yaml` a `modelos/*.json`. Unas reglas que declaren `ratings` se rechazan. La política solo decide qué hace con cada rating (aprueba, deriva o rechaza).
- **`versiones_reglas`**: tabla inmutable; publicar crea `vN+1` y la deja vigente. Cada evaluación sigue guardando su `flujo.vN`, así que las viejas se reproducen igual. La clave `segmento:` de las versiones importadas antes se sigue leyendo como `flujo:`.
- **Una sola lista de cambios** (`diff_reglas.diferencias`) sobre reglas ya leídas: compara lo que significan, no el texto. La usan la vista previa del editor (vía `POST /reglas/{flujo}/cambios`), el chequeo de "sin cambios" al publicar y el historial.
- **Borrador compartido**, uno por flujo, guardado sin validar. Publicar exige que parta de la vigente (bloqueo optimista). Si alguien publicó en el medio, el borrador se puede pasar a la vigente conservando su contenido: la lista de cambios muestra también lo que publicarlo desharía. Volver atrás es cargar una versión vieja en el borrador y publicarla.
- **Sin autenticación**: el autor de cada publicación es un nombre declarado, con motivo obligatorio. Dos personas editando a la vez se pisan el borrador (el último que guarda gana).
- **Flujo de entrada**: una solicitud nueva entra por el flujo de originación de su segmento (`flujo_de_originacion`).
