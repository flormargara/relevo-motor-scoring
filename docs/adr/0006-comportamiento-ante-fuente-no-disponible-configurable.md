# Qué hace el motor cuando falta una fuente se configura por regla, no se fija en código

Cada fuente declara su política de reintentos (cantidad, espera, timeout) y cuántos días se puede reutilizar su snapshot dentro de una misma solicitud (pensado para fuentes pagas). Cada regla que depende de una fuente declara qué hacer si esa fuente no está disponible después de los reintentos:
- `continuar`: la regla queda "no evaluable", se sigue con las siguientes, y si no hay rechazo el veredicto es `DERIVAR`.
- `derivar`: se corta y el veredicto es `DERIVAR`.
- `rechazar`: se corta y el veredicto es `RECHAZADO`.

Si la regla no dice nada, el default es `continuar`. Así el criterio de riesgo ante datos faltantes queda en manos de la entidad y no de quien programa.
