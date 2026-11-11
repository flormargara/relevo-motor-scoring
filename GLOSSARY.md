# Motor de decisión de riesgo

Decide si se le presta a un solicitante PyME o monotributista, en qué condiciones, y explica por qué. Es una decisión automática y reproducible, que asiste a un analista de riesgo humano.

## Actores

**Entidad**:
La organización que presta y usa el motor (mutual, cooperativa, fintech, banco).
_Avoid_: cliente (ambiguo con solicitante)

**Solicitante**:
La PyME o el monotributista que pide el crédito, identificado por su CUIT/CUIL.
_Avoid_: cliente, deudor, usuario

**Analista**:
La persona de la entidad que revisa los casos, sobre todo los derivados.
_Avoid_: operador, usuario

**Segmento**:
El tipo de solicitante: `PyME` (persona jurídica) o `Monotributista` (persona humana con actividad). Es un dato de la solicitud; las reglas no son del segmento sino del flujo.

**Flujo**:
Una variante del motor aplicada a un segmento: la unidad que tiene sus propias reglas, su catálogo de variables y su historial de versiones. Se identifica por segmento más tipo de flujo (hoy solo originación: "PyME · Originación"; a futuro, p. ej., comportamiento). El punto de entrada decide qué flujo corre: una solicitud nueva entra por originación; una cuenta ya otorgada, por comportamiento.
_Avoid_: pipeline (es el recorrido interno de un flujo), segmento (es solo el tipo de solicitante)

## Pipeline de decisión

**Solicitud**:
El pedido de crédito de un solicitante: segmento, monto y plazo solicitados, y documentos.

**Evaluación**:
Una corrida inmutable del motor sobre una solicitud, con sus snapshots, su traza, su versión de reglas y su veredicto. Una solicitud puede tener varias; vale la última.
_Avoid_: corrida, ejecución

**Fuente**:
Un origen de datos externo o interno que el motor consulta para una solicitud (Central de Deudores, BADLAR, IGJ, bureau, identidad, datos propios).
_Avoid_: API, proveedor, integración

**Fuente gratuita / Fuente paga**:
Una fuente según si consultarla le cuesta dinero a la entidad. Las gratuitas se consultan siempre al inicio; las pagas, solo cuando una regla las necesita por primera vez.

**Snapshot**:
La respuesta cruda de una fuente, tal como llegó, con su fecha y hora de consulta.

**Sin antecedentes**:
El resultado de una fuente que no tiene datos sobre el solicitante. Es información válida, no una falla.
_Avoid_: error, not found

**Fuente no disponible**:
El resultado de una fuente que no pudo responder (caída o demora excesiva).

**Killer**:
Una regla que saca al solicitante del flujo antes de estimar su riesgo, por una condición que lo descalifica sin importar la PD. Los killers se evalúan en un orden fijo, y el primero que salta rechaza.
_Avoid_: filtro, regla dura, knock-out

**PD (probabilidad de default)**:
La probabilidad estimada de que el solicitante entre en mora grave dentro del horizonte del modelo.
_Avoid_: score (el score es la escala de puntos, no la probabilidad)

**Scorecard**:
El modelo que convierte las características del solicitante en puntos y, a partir de ellos, en una PD. Cada versión de reglas de un flujo referencia uno.

**Rating**:
La categoría de riesgo (A a E) en la que cae una PD. Los ratings y sus topes de PD son parte del modelo, no de la política: la política solo decide si cada rating aprueba, deriva o rechaza.
_Avoid_: banda, tier, grado

**Pérdida esperada teórica**:
La pérdida que el modelo tiene que garantizar para cada rating. Por eso los topes de PD de los ratings no se editan: solo se informan.

**Política**:
El conjunto de reglas que, dado el rating y la capacidad de pago, construye la oferta o rechaza la solicitud.

**Capacidad de pago**:
El monto de cuota que el solicitante puede afrontar: un % de las ventas en PyME, o la relación cuota/ingreso estimado en monotributista.

**Tasa base**:
La tasa de referencia del mercado sobre la que se fija el precio (BADLAR).

**Spread**:
Los puntos que se suman a la tasa base según el rating.

**Oferta**:
Las condiciones que el motor propone: monto, plazo, tasa y cuota. Puede ser menor que lo solicitado.

**Veredicto**:
La salida del motor para una solicitud: `APROBADO` (con oferta), `DERIVAR` o `RECHAZADO`.
_Avoid_: decisión, resultado, dictamen

**Derivar**:
El veredicto que envía la solicitud a un analista porque el motor no puede o no debe decidir solo.

**Resolución del analista**:
La decisión que toma el analista sobre una evaluación derivada (aprobar o rechazar, con motivo). Se registra aparte y nunca reemplaza el veredicto.
_Avoid_: override, veredicto manual

**Traza**:
El registro paso a paso de cómo el motor llegó al veredicto: fuentes consultadas, reglas evaluadas con sus valores, puntos, PD, rating y oferta.
_Avoid_: log, auditoría

**Versión de reglas**:
Una versión identificada e inmutable del conjunto de killers, derivaciones, ratings y política de un flujo.

## Edición de reglas

**Catálogo de variables**:
Las variables (`fuente.variable`) que un flujo puede usar en sus condiciones, con su tipo y descripción. Es propio de cada flujo: solo incluye las fuentes que ese flujo consulta.

**Borrador**:
La versión de reglas en edición de un flujo, todavía sin efecto sobre las evaluaciones. Hay uno por flujo y parte de la versión vigente.

**Versión vigente**:
La versión de reglas que usan las evaluaciones nuevas de un flujo: la última publicada.

**Publicar**:
Validar un borrador y convertirlo en una nueva versión de reglas, que pasa a ser la vigente. Volver atrás es publicar de nuevo el contenido de una versión anterior; nunca se modifica una versión publicada.
_Avoid_: guardar, desplegar, activar

## Agentes

**Agente documental**:
El agente de IA que lee los documentos de una solicitud y extrae sus campos, con un nivel de confianza por campo.

**Agente de informe**:
El agente de IA que redacta el informe de riesgo de una evaluación y responde preguntas del analista sobre ella.
_Avoid_: agente analista

**Informe de riesgo**:
El texto en lenguaje natural que explica un veredicto: fundamentos, alertas y qué conviene verificar.
