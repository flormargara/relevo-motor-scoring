---
status: proposed
---

# Las reglas se editan desde la app y viven en la base de datos, por flujo

Las reglas dejan de vivir solo en archivos `rules/*.yaml`: pasan a una tabla de versiones en la base, que la app edita (borrador, publicar `vN+1`) y el motor lee. Los YAML quedan como semilla inicial (se importan `v1`–`v3` de cada flujo con su número, para que las evaluaciones viejas sigan resolviendo) y como formato de export de cada versión. Se mantiene de ADR 0002 el lenguaje de condiciones acotado, la validación al cargar y que cada evaluación guarda la versión exacta que usó.

El dueño de las reglas es el **flujo** (segmento + tipo de flujo), no el segmento: un flujo futuro de comportamiento tendría reglas, catálogo y versiones propios. Los ids actuales (`pyme.v3`) no cambian.

## Considered options

- **YAML en git, editor que escribe a git**: conserva el historial legible y la revisión por PR, pero un editor en la app necesitaría credenciales, PRs y un despliegue por cada cambio de política. Se descartó por la fricción para quien opera las reglas.
- **Base de datos con YAML como semilla y export** (elegida): permite publicar en caliente; el historial y el diff entre versiones los da la app.

## Consequences

- Un cambio de reglas ya no pasa por PR ni por tests de CI: la protección es la validación al publicar (sintaxis, variables del catálogo del flujo, ratings que cubren la PD hasta 1, ratings de la política existentes, spread para cada rating aprobado), un motivo obligatorio y el diff contra la versión anterior.
- Sin autenticación por ahora: el autor de cada publicación es un nombre declarado. Roles y cuatro ojos quedan para cuando haya login.
- Un solo borrador por flujo; publicar exige que parta de la versión vigente (bloqueo optimista).
- Sin simulador: el backtest sigue en la fase 2 y la revisión de un cambio es el diff.
- El scorecard (`modelos/*.json`) sigue siendo un archivo versionado que cada versión referencia; no se edita desde la app.
- Los ratings y sus topes de PD tampoco se editan: son del modelo, que garantiza una pérdida esperada teórica por rating, y la app solo los informa. Lo editable es qué hace la política con cada rating (aprueba, deriva o rechaza) y la oferta (spread por rating, capacidad de pago, montos y plazos).
