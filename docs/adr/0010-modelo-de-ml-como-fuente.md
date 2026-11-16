---
status: accepted
---

# El modelo de ML es una fuente que reemplaza al scorecard, con sombra y split

El modelo de ML se desarrolla y se despliega en otro repositorio (módulo "Modelos de ML", ADR 0008). Relevo lo consulta por un endpoint HTTP con un contrato fijo (`docs/contrato-modelo-ml.md`) y no sabe cómo está hecho.

**Es una fuente.** El adapter `modelo_ml` lo consulta como a CENDEU o al bureau: su respuesta queda como snapshot de la evaluación, que así se puede reproducir y auditar. El simulador también la puede volver a correr sin llamar al endpoint. Tiene timeout y reintentos en `config/fuentes.yaml` y es gratuita: se consulta siempre al inicio (ADR 0005).

**Reemplaza al scorecard: decide un solo modelo.** Un descriptor en `modelos/` de tipo `externo` dice qué modelo y qué versión pedirle al endpoint, y define los cortes de rating, que siguen siendo del modelo (ADR 0007). Las reglas lo eligen igual que a un scorecard de puntos: `scorecard.modelo: pyme-gbm.2026-11.json`. Su PD da el rating y la oferta. Si el modelo no responde, la regla `scorecard.si_no_disponible` dice si se deriva o se rechaza; seguir no es una opción, porque sin PD no hay rating. Las reglas también pueden usar `modelo_ml.pd` en sus condiciones.

**Relevo manda CUIT, segmento y período; el modelo calcula sus variables.** Así Relevo no replica la ingeniería de variables con que se entrenó. El período es obligatorio: el modelo no puede usar datos posteriores a él, para que una evaluación histórica no vea el futuro.

**Los CUIT reservados se controlan, no solo se documentan.** El endpoint publica la metadata de cada versión con la regla de exclusión que usó al entrenar. Relevo la compara con la del ADR 0009 (`relevo.reservados`) y avisa en Backoffice si una versión que usan las reglas no la declara o declara otra.

**Sombra y split.** Siempre decide un solo modelo, pero las reglas pueden declarar:

- **Modelos en sombra:** se consultan y se guardan en cada evaluación, pero no deciden ni aparecen en las condiciones.
- **Un split:** varios modelos con pesos, por ejemplo 50/50. La asignación es determinística por CUIT, con una semilla por experimento: un solicitante cae siempre en el mismo modelo y la evaluación es reproducible. Todos los modelos del split corren también en sombra, así que cada solicitud tiene la PD de todos y se comparan sobre la misma población. El split sirve además para ver cómo pagan los créditos que aprobó cada modelo.

El split es parte de la versión de reglas: se publica desde Backoffice como cualquier otro cambio.

## Considered Options

- **Relevo calcula las variables y se las manda al modelo.** Acopla a Relevo con la ingeniería de variables del modelo y obliga a mantenerla igual en los dos repositorios.
- **El modelo como un paso aparte del motor, no como fuente.** Habría que repetir lo que la fuente ya resuelve: snapshot, reintentos, no disponible y reproducción en el simulador.
- **Scorecard y modelo en paralelo, combinando las dos PD.** Dos modelos que deciden juntos son difíciles de calibrar y de explicar. Se prefiere uno que decide y otros en sombra.

## Consequences

- Una evaluación hecha antes de conectar un modelo no tiene su snapshot. El simulador no puede promover ese modelo sobre esas evaluaciones: quedan como no evaluables.
- Sin `RELEVO_MODELO_URL` la fuente está no disponible. Las reglas que no usan un modelo externo no cambian.
- El servidor de prueba (`relevo.modelo_prueba`) cumple el contrato con una PD falsa, para probar la integración sin el repositorio del modelo.
