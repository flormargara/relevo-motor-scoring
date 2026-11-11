---
status: superseded in part by ADR-0007 (dónde viven las reglas; el lenguaje acotado y la reproducibilidad siguen vigentes)
---

# Killers, ratings y política viven como YAML versionado, no como código

Las reglas de cada segmento se declaran en archivos YAML versionados (`rules/<segmento>.v<N>.yaml`) que el motor interpreta, y cada evaluación guarda qué versión usó. Así se puede reproducir cualquier decisión pasada y queda preparado el simulador de políticas de la fase 2 (editar las reglas y correr un backtest sin tocar código). El costo es un intérprete de reglas con un lenguaje de condiciones deliberadamente acotado, en lugar de escribir las reglas en Python.
