# Contrato del modelo de ML

Cómo Relevo consulta un modelo de ML que vive en otro repositorio (ADR 0010). Cualquier servicio que cumpla este contrato se puede conectar: Relevo no sabe cómo está hecho el modelo.

Relevo lo encuentra en `RELEVO_MODELO_URL`. Si existe `RELEVO_MODELO_TOKEN`, lo manda como `Authorization: Bearer <token>`. El servidor de prueba (`python -m relevo.modelo_prueba`) cumple este contrato con una PD falsa.

## `POST /v1/pd`: la PD de un solicitante

```json
{"cuit": "30712345671", "segmento": "pyme", "periodo": 202605, "modelo": "pyme-gbm", "version": "2026.11.1"}
```

- `segmento`: `pyme` o `monotributista`.
- `periodo` (AAAAMM): **el modelo no puede usar datos posteriores a este período.** Relevo manda el mes anterior a la fecha de evaluación, que es el último de la Central de Deudores. Así una evaluación histórica no ve el futuro.
- `modelo` y `version`: los del descriptor en `modelos/`. Un servicio puede servir varios modelos y versiones a la vez, que es lo que permite tener modelos en sombra y splits.

### Respuestas

**200**: el modelo tiene datos del CUIT.

```json
{
  "pd": 0.034,
  "modelo": "pyme-gbm",
  "version": "2026.11.1",
  "periodo_datos": 202604,
  "factores": [
    {"variable": "peor_situacion_12m", "valor": 2, "aporte": 0.81},
    {"variable": "antiguedad_deuda_meses", "valor": 40, "aporte": -0.35}
  ]
}
```

- **`pd`** es la probabilidad de default a 12 meses, entre 0 y 1. Default es lo mismo que en el scorecard y en el monitoreo: 90 días de mora o más dentro de los primeros 12 meses.
- **`periodo_datos`** es el último período que usó el modelo. No puede ser posterior a `periodo`.
- **`factores`** son opcionales: los valores SHAP de la predicción, ordenados por `|aporte|` de mayor a menor, como mucho 10.
  - `aporte` va en la escala del modelo (log-odds). Positivo sube la PD.
  - `valor` es el valor de la variable para ese solicitante.
  - El agente de informe los usa para explicar el caso.

**404**: el modelo no tiene datos del CUIT hasta ese período. Relevo lo toma como **sin antecedentes**, que es un dato y no una falla (ADR 0003).

**400 o 422**: el pedido no es válido; por ejemplo, un modelo o una versión que el servicio no tiene. Relevo lo toma como **no disponible** y no reintenta.

**5xx, timeout o error de red**: Relevo lo toma como **no disponible**. Reintenta según `config/fuentes.yaml`. Si después sigue sin respuesta, aplica la regla `si_no_disponible` (ADR 0006).

## `GET /v1/modelos`: los modelos que sirve

```json
{
  "modelos": [
    {
      "modelo": "pyme-gbm",
      "version": "2026.11.1",
      "segmento": "pyme",
      "entrenado_en": "2026-11-10",
      "periodos_entrenamiento": [202401, 202512],
      "exclusion_reservados": {"regla": "md5-8hex-mod100", "porcentaje": 5},
      "metricas": {"auc": 0.78, "ks": 0.42}
    }
  ]
}
```

**`exclusion_reservados`** es obligatorio: declara que el entrenamiento excluyó los CUIT reservados para la demo (ADR 0009), con la misma regla y el mismo porcentaje que `relevo.reservados`.

Backoffice muestra un aviso en tres casos:

- La versión que usan las reglas no está publicada en el servicio.
- La versión no declara la exclusión.
- La versión declara otra regla u otro porcentaje.

Para excluirlos en SQL (duckdb), usá `relevo.reservados.sql_reservado()`:

```sql
WHERE NOT (('0x' || left(md5(CAST(cuit AS VARCHAR)), 8))::UBIGINT % 100 < 5)
```

## El descriptor en `modelos/`

```json
{
  "tipo": "externo",
  "modelo": "pyme-gbm",
  "version": "2026.11.1",
  "ratings": [
    {"rating": "A", "pd_hasta": 0.02},
    {"rating": "B", "pd_hasta": 0.05},
    {"rating": "C", "pd_hasta": 0.10},
    {"rating": "D", "pd_hasta": 0.20},
    {"rating": "E", "pd_hasta": 1}
  ]
}
```

Relevo hace un `POST /v1/pd` por cada modelo que nombran las reglas: el que decide, los del split y los de sombra. Si un modelo falla y otro responde, la falla queda anotada solo para ese modelo.

Las reglas lo usan como a cualquier scorecard: `scorecard: {modelo: pyme-gbm.2026-11.json, si_no_disponible: derivar}`. Si el modelo no responde o no tiene datos del CUIT, no hay PD y la evaluación se deriva o se rechaza, según `si_no_disponible`.

Con split y sombra:

```yaml
scorecard:
  split:
    - {modelo: pyme-gbm.2026-11.json, peso: 50}
    - {modelo: pyme-gbm.2026-12.json, peso: 50}
  semilla: experimento-diciembre   # opcional; por defecto sale de los modelos del split
  sombra: [pyme.v2.json]           # se calculan y se guardan, no deciden
  si_no_disponible: derivar
```

Los modelos del split tienen que tener los mismos ratings, porque la política decide por rating. Los de sombra pueden tener otros.
