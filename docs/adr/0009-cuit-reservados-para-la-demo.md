# Un 5 % fijo de CUIT queda reservado para la demo y fuera del entrenamiento del modelo de ML

La cartera de demo se arma con solicitantes y mora reales de la Central de Deudores (bucket `data-cendeu-raw`). Las variables v2 de ese mismo bucket se crearon para entrenar un modelo de ML que el motor va a consultar por un endpoint. Si la demo mostrara a alguien que el modelo vio al entrenar, el monitoreo y el simulador medirían el modelo dentro de muestra y se verían mejor de lo que son.

Por eso un CUIT es **de la demo** si `md5(str(cuit))`, leído como entero desde sus primeros 8 dígitos hexadecimales, da resto menor a 5 al dividirlo por 100. La regla es la misma en todos lados:

- Python: `relevo.demo_real.es_reservado(cuit)`
- SQL (duckdb): `('0x' || left(md5(CAST(cuit AS VARCHAR)), 8))::UBIGINT % 100 < 5` (`relevo.demo_real.sql_reservado()`)

**El entrenamiento del modelo de ML tiene que excluir esos CUIT**, en todas las fechas de observación.

## Considered Options

- **Reservar CUIT por hash** (elegida). No depende de una lista guardada, es estable al agregar períodos y deja usar todas las fechas para las dos cosas.
- **Separar por tiempo** (el modelo entrena con las primeras fechas y la demo usa las últimas). Se parece más a producción, pero las cohortes recientes tienen pocos meses de historia y la demo queda chica.
- **Las dos cosas a la vez.** Es lo más limpio, pero achica demasiado la demo.

## Consequences

- El porcentaje y la regla no se cambian sin reentrenar: si cambian, el modelo puede haber visto CUIT que pasan a ser de la demo.
- Con el 5 % hay de sobra: en cada período quedan unas 13.000 jurídicas y un millón de personas humanas con deuda informada.
- Los CUIT de la demo son reales. La base (`data/demo.sqlite`) y su cartera (`data/demo/`) quedan fuera de git y no se suben al bucket compartido.
