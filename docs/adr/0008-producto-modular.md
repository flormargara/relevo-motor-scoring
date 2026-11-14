---
status: proposed
---

# El producto se vende completo o por módulos, cada uno con API y con pantalla

Relevo se puede comprar entero o por partes. Los módulos vendibles son cuatro:

1. **Motor y backoffice de reglas**: evalúa una solicitud y devuelve veredicto, oferta y traza; el backoffice edita, publica y compara versiones de reglas (ADR 0007).
2. **Modelos de ML**: calculan score/PD a partir de variables. Tres variantes según el origen de los datos: externos (bureau, CENDEU, ARCA), propios del cliente, o mixtos.
3. **Lectura de documentos**: convierte documentos subidos (balances, constancias) en campos con confianza.
4. **Chat con tus datos**: uno o más agentes que responden preguntas sobre cualquier etapa del ciclo (evaluación, oferta, conversión, desembolso), elaboran informes, simulan cambios de política, estiman resultados sobre una base de clientes y emiten alertas.

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
- El chat necesita datos que hoy no se guardan: el repositorio llega hasta la evaluación. Conversión, desembolso y comportamiento posterior hay que modelarlos y definir de dónde entran (carga, API del cliente o importación). Es el trabajo más grande de este ADR.
- Hoy hay un solo proceso, `app.py` expone páginas y endpoints juntos. Para entregar por API hay que separar, por módulo, la API de las páginas, sin necesidad de separar procesos todavía.
- Sin autenticación (ADR 0007): vender como servicio a terceros exige login y claves de API por cliente antes del primer despliegue externo.
- Los datos de cada cliente deben quedar aislados. Hoy hay una sola base; la multi-tenencia no está resuelta.

## Preguntas abiertas

- ¿Los modelos de ML con datos propios del cliente se entrenan en Relevo o los trae el cliente ya entrenados? Cambia el contrato (variables → score, o además datos → modelo).
- ¿Cómo se cobra cada módulo? No afecta el diseño de interfaces, pero sí qué métricas de uso hay que registrar.
