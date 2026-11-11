# DER de la base de trabajo (`data/relevo.sqlite`)

Diagrama entidad-relación del esquema de [`repositorio.py`](../src/relevo/repositorio.py) (constante `ESQUEMA`). Si cambia el esquema, hay que actualizar este diagrama.

```mermaid
erDiagram
    solicitudes ||--o{ evaluaciones : tiene
    solicitudes ||--o{ documentos : adjunta
    evaluaciones ||--o{ snapshots : consulta
    evaluaciones ||--o| informes : redacta
    evaluaciones ||--o{ chat : conversa
    evaluaciones ||--o{ resoluciones : resuelve
    versiones_reglas ||--o{ evaluaciones : "usada por (flujo.vN)"
    versiones_reglas ||--o| borradores_reglas : "parte de (base)"

    solicitudes {
        text id PK
        text cuit
        text segmento
        real monto
        int plazo_meses
        text documentos
        text origen
        int consentimiento
        text creada_en
        text fecha_nacimiento
    }
    evaluaciones {
        int id PK
        text solicitud_id FK
        text creada_en
        text version_reglas
        text veredicto
        text motivos
        real puntos
        real pd
        text rating
        text oferta
        text traza
        text fuentes_no_consultadas
        text denominacion
    }
    snapshots {
        int id PK
        int evaluacion_id FK
        text solicitud_id
        int orden
        text fuente
        text consultado_en
        text crudo
        text resultado
        text tipo_resultado
        text origen
        int intentos
        text nota
    }
    documentos {
        int id PK
        text solicitud_id FK
        text archivo
        text ruta
        text extraido
        text error
        text creado_en
    }
    informes {
        int evaluacion_id PK
        text texto
        text creado_en
    }
    chat {
        int id PK
        int evaluacion_id FK
        text rol
        text texto
        text creado_en
    }
    resoluciones {
        int id PK
        int evaluacion_id FK
        text decision
        text motivo
        text analista
        text creada_en
    }
    versiones_reglas {
        text flujo PK
        int version PK
        text contenido
        text autor
        text motivo
        int base
        text publicada_en
    }
    borradores_reglas {
        text flujo PK
        text contenido
        int base
        text actualizado_en
    }
```

## Notas

- Todas las claves foráneas son `ON DELETE CASCADE`: borrar una solicitud borra sus evaluaciones, documentos, snapshots, informe, chat y resoluciones.
- Una evaluación es inmutable: reevaluar agrega una fila nueva y vale la última.
- `snapshots.solicitud_id` repite el de la solicitud sin ser clave foránea: el almacén de snapshots lo usa para reutilizar consultas pagas entre evaluaciones de la misma solicitud (índice `snapshots_por_fuente`).
- `solicitudes.documentos` guarda las variables cargadas a mano en el alta. Los documentos subidos y lo que extrae el agente están en la tabla `documentos`.
- `evaluaciones.traza`, `motivos`, `oferta` y `snapshots.crudo` / `resultado` son JSON en columnas de texto.
- `versiones_reglas` guarda las reglas de cada flujo (ADR 0007). Es inmutable: publicar agrega una fila y la vigente es la de mayor `version`. `contenido` es el YAML de las reglas; las versiones sembradas desde `rules/` tienen autor `semilla`. No hay clave foránea con `evaluaciones`: la evaluación guarda el id `flujo.vN` como texto en `version_reglas`.
- `borradores_reglas` tiene a lo sumo un borrador por flujo, compartido. `contenido` es JSON todavía sin validar y `base` es la versión vigente de la que parte; publicar lo valida, crea la versión siguiente y borra el borrador.
- Los padrones `data/arca.sqlite` y `data/igj.sqlite` son snapshots de solo lectura, con otro esquema, y no forman parte de este diagrama.
