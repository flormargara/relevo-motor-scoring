# El motor decide de forma determinística; los agentes de IA solo asisten

El veredicto (killers → PD → rating → política) lo calcula código determinístico, a partir de reglas y de un scorecard con coeficientes fijos. Los agentes de IA solo extraen datos de documentos, redactan el informe de riesgo y responden preguntas: nunca producen ni modifican el veredicto. Así la decisión es reproducible, auditable ante un regulador y explicable regla por regla. Pedirle la PD o el veredicto a un LLM no cumple ninguna de las tres cosas.

## Consequences
Si el agente documental extrae un campo con baja confianza, eso no lo resuelve el agente: es una condición que evalúan las reglas, por ejemplo para derivar.
