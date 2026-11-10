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
   (CENDEU, BADLAR, IGJ, bureau, identidad, propios, documentos, fake)
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

## 3. `reglas`

```python
cargar_reglas(segmento: str, version: str | None = None) -> Reglas
```

Lee `rules/<segmento>.v<N>.yaml` y valida la estructura al cargar: si el YAML está mal, el error aparece en ese momento y no en medio de una evaluación. Las condiciones usan un lenguaje acotado (`cendeu.peor_situacion_12m >= 3 and propios.lista_negra`), interpretado sobre un AST con una lista blanca de operaciones. Nunca se usa `eval`.

El YAML también referencia los coeficientes del scorecard (`modelos/<segmento>.v<N>.json`), que se generan offline con el script de entrenamiento.

## 4. `agentes`

```python
extraer_documento(archivo: Path, cliente: ClienteClaude) -> DocumentoExtraido
redactar_informe(evaluacion: Evaluacion, cliente: ClienteClaude) -> str
responder(evaluacion: Evaluacion, historial: list[Mensaje], pregunta: str, cliente: ClienteClaude) -> str
```

**Seam `ClienteClaude`:** `completar(prompt: str, archivos: list[Path] = []) -> str`. Tiene tres adapters: `ClaudeCLI` (`claude -p`, el default según el ADR 0004), `ClaudeAPI` y `ClaudeFalso` para los tests.

## 5. `repositorio`

Guarda en SQLite: solicitudes, evaluaciones (con snapshots, traza, versión de reglas y veredicto), informes, chat y resoluciones del analista. También lista la bandeja y borra una solicitud. Tiene un solo adapter (SQLite; los tests usan `:memory:`), así que no lleva seam propio.

## 6. `app`

FastAPI más plantillas Jinja y JS sin frameworks. Tiene tres pantallas: Bandeja, Caso y Reglas. Es delgada a propósito: arma la solicitud, llama a `evaluar`, guarda el resultado y lo muestra.

## 7. `sintetico` (offline)

Genera las carteras sintéticas por segmento, entrena el scorecard logístico, exporta los coeficientes y crea los documentos de ejemplo. No es parte del runtime.

## Orden de construcción (TDD, de adentro hacia afuera)

1. `motor` + `reglas`, con `ConsultorFalso`: es donde está el valor.
2. Adapters de fuente: CENDEU y BADLAR reales, más los de juguete; después `ConsultorReal`.
3. `sintetico`: entrenar el scorecard y reemplazar los coeficientes provisionales.
4. `repositorio` + `app`.
5. `agentes` (CLI, después API).
