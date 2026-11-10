# Acuerdos de la demo v1 — Motor de decisión de riesgo

Resultado de la sesión de *grilling* (2026-11-09). Es la fuente de verdad del alcance de la v1.

## Qué es
Motor de decisión de crédito con el pipeline **inputs → killers → PD → política → veredicto**, operado por un analista de riesgo. Demo para Martín y clientes.

## Segmentos y producto
Préstamo de capital de trabajo, cuotas fijas (sistema francés).
- **PyME** (persona jurídica): se evalúa con balance.
- **Monotributista** (persona humana con actividad): la categoría de monotributo es el proxy de ingreso.

## Fuentes
| Fuente | Modo | Uso |
|---|---|---|
| BCRA Central de Deudores (`/Deudas`, `/Deudas/Historicas`, `/Deudas/ChequesRechazados`) | Real, en vivo | Killers y variables del scorecard |
| BCRA Estadísticas v4 (BADLAR, id 7) | Real, en vivo | Tasa base de la oferta |
| IGJ (datos.jus.gob.ar) | Snapshot precargado | Existencia, baja y antigüedad de la sociedad |
| Bureau comercial | Juguete | Score de bureau |
| Identidad (tipo RENAPER) | Juguete | Validación de identidad |
| Datos propios de la entidad | Juguete | Lista negra, historial |
| ARCA padrón (con certificado WSAA) | **Fase 2** | — |

Cada fuente es un conector intercambiable detrás de una misma interfaz.

## Documentos
Constancia de inscripción ARCA y balance / estados contables. Los extrae el **agente documental** (campos más nivel de confianza). Los casos precargados usan documentos sintéticos coherentes con su CUIT; un prospecto PyME real (ON PyME en CNV) sirve para la demo en vivo.

## Motor (determinístico)
- **Killers** por segmento: identificación inválida (incluye dígito verificador), edad, situación BCRA ≥ 3 en los últimos 12 meses, cheques rechazados impagos, lista negra, sociedad dada de baja, antigüedad < 1 año.
- **PD**: un scorecard logístico por segmento, entrenado sobre una cartera sintética y explicable por puntos.
- **Banda** de riesgo A–E a partir de la PD.
- **Política**:
  - Banda E → rechazo.
  - Monto máximo = % de ventas (PyME) o relación cuota/ingreso (monotributista).
  - Tasa = BADLAR + spread por banda.
  - Oferta por debajo del monto mínimo → rechazo.
- **Veredictos**: `APROBADO` (con oferta) / `DERIVAR` (a analista) / `RECHAZADO`.
- **Reglas como datos**: YAML versionado por segmento (`rules/pyme.v1.yaml`, `rules/monotributista.v1.yaml`). Cada evaluación guarda la versión usada.

## Manejo de errores
- CENDEU **404 = "sin antecedentes"**: es un dato, no un error. Lo resuelve la política.
- 400 o dígito verificador inválido → killer "identificación inválida".
- Timeout (10 s) o 5xx → 1 reintento, después "fuente no disponible" → `DERIVAR`.
- BADLAR caída → se usa el último valor guardado, marcado como desactualizado.
- Agente caído o no configurado → el veredicto sale igual, sin informe.
- Todo error queda en la traza.

## Agentes (asisten, no deciden)
- **Agente documental**: extrae los campos de los documentos.
- **Agente analista**: escribe el informe de riesgo y responde preguntas del analista sobre el caso.
- Cliente de Claude con dos adaptadores: `claude -p` (Claude Code con la suscripción del usuario, **por defecto**) y la API de Anthropic (key en `.env`).

## Stack
Python 3.12, FastAPI, SQLite, frontend web servido por el backend, pytest (TDD). pandas y scikit-learn para los datos sintéticos y el entrenamiento.

## Persistencia (solo local)
Por cada evaluación se guarda:
- la solicitud;
- el snapshot crudo de cada fuente, con fecha y hora;
- la traza;
- la versión de las reglas;
- el veredicto;
- el informe del agente y el chat.

Se puede borrar una evaluación. Los CUIL de personas humanas se consultan solo con consentimiento (Ley 25.326).

## Pantallas
1. **Bandeja**: solicitudes simuladas, más el alta manual por CUIT/CUIL con carga de documentos.
2. **Caso**: traza paso a paso, documentos y campos extraídos, informe del agente y chat.
3. **Reglas**: YAML vigente por segmento (solo lectura).

## Casos precargados
Empresas grandes con datos públicos en CENDEU, consultadas en vivo, más un CUIL con consentimiento para el caso monotributista.

## Fuera de alcance (fase 2)
- Simulador de políticas y backtesting.
- Cobranzas.
- Integración real con Jira o Salesforce.
- ARCA con certificado.
