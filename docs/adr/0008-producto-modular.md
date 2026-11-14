---
status: proposed
---

# El producto se vende completo o por módulos, cada uno con API y con pantalla

Relevo se puede comprar entero o por partes. Los módulos vendibles son cuatro:

1. **Motor y backoffice de reglas**: evalúa una solicitud y devuelve veredicto, oferta y traza; el backoffice edita, publica y compara versiones de reglas (ADR 0007).
2. **Modelos de ML**: calculan score/PD a partir de variables. Tres variantes según el origen de los datos: externos (bureau, CENDEU, ARCA), propios del cliente, o mixtos.
3. **Lectura de documentos**: convierte documentos subidos (balances, constancias) en campos con confianza.
4. **Chat con tus datos**: uno o más agentes que responden preguntas sobre cualquier etapa del ciclo (evaluación, oferta, conversión, desembolso), elaboran informes, simulan cambios de política, estiman resultados sobre una base de clientes y emiten alertas. Incluye el **monitoreo** (parte de lo que registran los flujos y de los datos de cartera del cliente; mantiene reportes actualizados y envía informes) y el **simulador de políticas** (backtesting sobre la historia).

Cada módulo se entrega **de las dos formas**: como servicio con API, para que el cliente lo integre en su sistema, y como aplicación con pantalla propia. La pantalla es un cliente de la API del módulo, nunca un camino aparte a los datos: así lo que se ve en pantalla y lo que se integra por API es lo mismo.

## Dependencias entre módulos

```
 documentos ──▶ (variables con confianza) ──▶ motor ◀── modelos
                                                │
                                   (evaluaciones, ofertas, desembolsos)
                                                ▼
                                       chat con tus datos
```

- El **motor** corre solo: sin modelos usa el scorecard de `modelos/*.json` y sin documentos usa solo fuentes. Es el módulo que se vende como base.
- **Modelos** y **documentos** se conectan al motor por el mismo seam que ya existe (`Consultor`): entregan variables con origen y confianza. El motor no sabe de qué módulo vienen.
- El **chat** depende de los otros tres, pero solo en lectura. Nada depende del chat.

## Contratos

Cada módulo expone una interface chica y estable; lo que hay detrás puede cambiar.

| Módulo | Entrada | Salida |
|---|---|---|
| Motor | solicitud + flujo | evaluación: veredicto, rating, oferta, traza, versión de reglas |
| Modelos | variables | score/PD, versión del modelo, variables usadas |
| Documentos | archivo + tipo | campos con valor y confianza, CUIT detectado |
| Chat | pregunta + contexto | respuesta con las evaluaciones o consultas que la respaldan |

## Considered options

- **Monolito único, venta solo completa**: es lo más simple, pero descarta clientes que ya tienen motor o modelos propios y solo quieren una parte.
- **Módulos con API pero sin pantalla**: obliga a todo cliente a integrar. Se descartó porque parte del mercado quiere operar sin desarrollo propio.
- **Módulos con API y pantalla propia** (elegida).

## Consequences

- El agente de IA nunca decide (ADR 0001) y eso vale también para el chat: puede explicar y simular, pero no publica reglas ni cambia un veredicto. **Por ahora el chat es de solo lectura**; ejecutar cambios (por ejemplo publicar una política simulada) queda fuera hasta que haya login, roles y aprobación explícita.
- Una simulación del chat corre el motor sobre datos guardados y no escribe evaluaciones: es una lectura, no una evaluación nueva.
- **Los datos posteriores a la evaluación (conversión, desembolso, pagos, mora) son responsabilidad del cliente y no forman parte del producto.** Relevo registra lo que decide (solicitud, evaluación, oferta, traza); lo que pasa después vive en los sistemas del cliente. El chat **los puede leer**, pero Relevo no los almacena, modela ni valida. Hace falta un **seam de lectura** (por ejemplo `FuenteDeCartera`) con adapters por tipo de origen del cliente (base SQL, API, archivos), de solo lectura, y que el cliente declare cómo se enlaza cada registro con la evaluación (el id de solicitud).
- Hoy hay un solo proceso, `app.py` expone páginas y endpoints juntos. Para entregar por API hay que separar, por módulo, la API de las páginas, sin necesidad de separar procesos todavía.
- Sin autenticación (ADR 0007): vender como servicio a terceros exige login y claves de API por cliente antes del primer despliegue externo.
- Los datos de cada cliente deben quedar aislados. Hoy hay una sola base; la multi-tenencia no está resuelta.

## Modelos entrenados en Relevo o traídos por el cliente

Pueden ser ambas cosas. Para el motor da igual: un modelo es "variables → score/PD" con nombre, versión y lista de variables que usa, sin importar quién lo entrenó. Lo que cambia es el origen que queda registrado junto a cada versión:

- **Entrenado en Relevo**: con datos externos, propios del cliente o mixtos. Hace falta un camino de entrenamiento y validación, que hoy no existe en el repo.
- **Traído por el cliente**: llega ya entrenado y Relevo lo registra y lo ejecuta. Hace falta fijar un formato de entrega (coeficientes del scorecard, o un modelo serializado) y validar que sus variables existan en el catálogo del flujo.

Ambos se versionan igual y las reglas referencian la versión exacta, como hoy con `modelos/*.json`.

## Alcance respecto del brief comercial

- **Cobranzas queda afuera** de este producto: sería un producto aparte, con sus propios módulos y datos.
- **Comportamiento y límites** no es un módulo: es un **flujo más del motor** (aumento o renovación de límite), como los de originación de monotributistas y de PyMEs. Tiene reglas, catálogo de variables y versiones propios (ADR 0007: el dueño de las reglas es el flujo). Lo nuevo es que parte de un cliente existente, así que necesita datos propios de su comportamiento (pagos, mora, uso de la línea) además de las fuentes externas.
- **Monitoreo y gobierno** es parte del chat: lee lo que registran los flujos (evaluaciones, ofertas) y lo que el cliente expone de su cartera (conversiones, desembolsos, pagos) y puede mantener reportes al día, enviar informes y emitir alertas. No necesita un módulo ni un almacenamiento aparte.
- **Simulador de políticas** también es parte del chat: ejecuta el motor sobre datos guardados con reglas candidatas y compara. Es lectura, no escribe evaluaciones ni publica reglas.

### Consecuencias de esta decisión

- Enviar informes y mantener reportes actualizados es una **acción de salida** (un mail, un archivo generado), aunque no cambie datos ni reglas. "Solo lee" se entiende como: nunca modifica evaluaciones, reglas ni modelos. Enviar informes exige definir destinatarios y permisos, y queda para cuando haya login y roles.
- Un flujo de comportamiento necesita datos de comportamiento del cliente (pagos, mora, uso de la línea). Entran como variables propias del cliente a través del mismo seam de lectura que usa el chat, no como tablas de Relevo. Si el cliente no los entrega, el flujo no puede correr: ese requisito es del cliente.

## Datos de prueba de cartera

Aunque los datos posteriores a la evaluación sean del cliente, para **probar y demostrar** el producto (chat, monitoreo, simulador, flujo de comportamiento) hay que tenerlos. Se generan sintéticos: conversión, desembolso, pagos y mora.

- Es una herramienta offline, como `src/relevo/sintetico.py` (hoy genera la cartera de entrenamiento del scorecard con un default a 12 meses): no es parte del runtime ni del producto vendido.
- Se enlazan con las evaluaciones por el id de solicitud, el mismo contrato que se le pide al cliente. Así lo sintético ejercita el seam de lectura real (`FuenteDeCartera`) con un adapter de prueba, y no un camino especial.
- Deben ser coherentes con el modelo: la mora observada por rating tiene que acompañar la PD que el scorecard asigna, con algo de ruido, para que el simulador y el monitoreo muestren resultados verosímiles. Deben incluir también desvíos plantados (por ejemplo un rating que se deteriora) para poder probar las alertas.
- Son determinísticos dada la semilla, y quedan fuera de git si son grandes (como `data/`).

## Preguntas abiertas

- **Precio por módulo**: sin definir. El brief comercial en borrador propone tres formas de empezar (diagnóstico, proyecto, servicio continuo), es decir, un modelo con servicios, no solo licencias. Mientras tanto, el diseño debe poder contar por módulo, sin decidir todavía cómo se cobra: evaluaciones del motor, documentos procesados, consultas del chat y su costo en tokens, y modelos activos por cliente.
