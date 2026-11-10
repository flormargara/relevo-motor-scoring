# Relevo AI — Motor de decisión de riesgo

Prototipo de un motor de decisión de crédito para PyMEs y monotributistas:

**inputs → killers → probabilidad de default → política → veredicto**

Consulta fuentes reales (BCRA Central de Deudores, BADLAR) y fuentes de juguete. Los agentes de IA leen documentos y explican cada decisión al analista.

El alcance acordado está en [docs/acuerdos-demo-v1.md](docs/acuerdos-demo-v1.md).

## Estructura

- `src/relevo/`: el código. `motor.evaluar` es la entrada del motor (ver [docs/diseno-modulos.md](docs/diseno-modulos.md)).
- `rules/`: reglas versionadas por segmento (`pyme.v1.yaml`, `monotributista.v1.yaml`).
- `modelos/`: puntos del scorecard por segmento (provisionales hasta entrenar con la cartera sintética).
- `tests/`: pytest.

## Correr los tests

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
