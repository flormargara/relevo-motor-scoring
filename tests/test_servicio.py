"""Servicio: alta, evaluación con persistencia de snapshots y casos precargados."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from adapters_falsos import FabricaFalsa

from conftest import BASE_MONOTRIBUTISTA, BASE_PYME, CUIL_MONOTRIBUTISTA, CUIT_PYME
from relevo.consultor import NoDisponible
from relevo.consultor_real import cargar_config_fuentes
from relevo.dominio import Veredicto
from relevo.repositorio import Repositorio
from relevo.servicio import ConsentimientoRequerido, Servicio, SolicitudInvalida

DOCUMENTOS_PYME = {"ventas_anuales": 240_000_000, "confianza_minima": 0.95}


class Reloj:
    def __init__(self):
        self.ahora = datetime(2026, 11, 10, 9, 0)

    def __call__(self):
        return self.ahora


def armar(fabrica=None, reloj=None):
    fabrica = fabrica or FabricaFalsa({k: v for k, v in BASE_PYME.items() if k != "documentos"})
    servicio = Servicio(
        Repositorio(),
        adapters=fabrica,
        config_fuentes=cargar_config_fuentes(),
        ahora=reloj or Reloj(),
        dormir=lambda s: None,
    )
    return servicio, fabrica


def test_alta_y_evaluacion(reglas_pyme):
    servicio, _ = armar()

    id = servicio.alta("30-71234567-1", "pyme", 10_000_000, 12, DOCUMENTOS_PYME)
    evaluacion_id = servicio.evaluar(id)

    guardada = servicio.repositorio.evaluacion(evaluacion_id)
    assert guardada.evaluacion.solicitud.cuit == CUIT_PYME  # normalizado
    assert guardada.evaluacion.version_reglas == "pyme.v3"
    assert guardada.evaluacion.veredicto is Veredicto.APROBADO
    assert {s.fuente for s in guardada.evaluacion.snapshots} >= {"cendeu", "bureau", "documentos"}


def test_persona_humana_requiere_consentimiento():
    servicio, _ = armar()

    with pytest.raises(ConsentimientoRequerido):
        servicio.alta(CUIL_MONOTRIBUTISTA, "monotributista", 1_000_000, 12)
    assert servicio.alta(CUIL_MONOTRIBUTISTA, "monotributista", 1_000_000, 12, consentimiento=True)


@pytest.mark.parametrize(
    "argumentos",
    [("x", "cooperativa", 1, 1), ("", "pyme", 1, 1), (CUIT_PYME, "pyme", 0, 12), (CUIT_PYME, "pyme", 1, 0)],
)
def test_alta_invalida(argumentos):
    with pytest.raises(SolicitudInvalida):
        armar()[0].alta(*argumentos)


def test_un_cuit_mal_formado_se_carga_y_el_motor_lo_rechaza():
    servicio, _ = armar()

    id = servicio.alta("30712345672", "pyme", 1_000_000, 12)

    veredicto = servicio.repositorio.evaluacion(servicio.evaluar(id)).evaluacion
    assert veredicto.veredicto is Veredicto.RECHAZADO
    assert veredicto.motivos[0].startswith("identificacion_invalida")


def test_reevaluar_reutiliza_el_bureau_de_la_misma_solicitud():
    reloj = Reloj()
    servicio, fabrica = armar(reloj=reloj)
    id = servicio.alta(CUIT_PYME, "pyme", 10_000_000, 12, DOCUMENTOS_PYME)

    servicio.evaluar(id)
    reloj.ahora += timedelta(days=10)
    segunda = servicio.repositorio.evaluacion(servicio.evaluar(id)).evaluacion

    assert fabrica.contador["bureau"] == 1  # paga: se reutilizó
    assert fabrica.contador["cendeu"] == 2  # gratuita: se consulta siempre
    assert next(s for s in segunda.snapshots if s.fuente == "bureau").origen == "reutilizado"

    reloj.ahora += timedelta(days=25)  # 35 días después del primer snapshot
    servicio.evaluar(id)
    assert fabrica.contador["bureau"] == 2


def test_otra_solicitud_no_reutiliza_el_bureau():
    servicio, fabrica = armar()
    for _ in range(2):
        servicio.evaluar(servicio.alta(CUIT_PYME, "pyme", 10_000_000, 12, DOCUMENTOS_PYME))

    assert fabrica.contador["bureau"] == 2


def test_badlar_caida_usa_el_ultimo_valor_de_otra_evaluacion():
    fabrica = FabricaFalsa({k: v for k, v in BASE_PYME.items() if k != "documentos"})
    servicio, _ = armar(fabrica)
    servicio.evaluar(servicio.alta(CUIT_PYME, "pyme", 10_000_000, 12, DOCUMENTOS_PYME))

    fabrica.respuestas["badlar"] = NoDisponible("HTTP 502")
    evaluacion = servicio.repositorio.evaluacion(
        servicio.evaluar(servicio.alta(CUIT_PYME, "pyme", 10_000_000, 12, DOCUMENTOS_PYME))
    ).evaluacion

    assert evaluacion.veredicto is Veredicto.APROBADO
    assert evaluacion.oferta.tasa_nominal_anual == 30.0 + 6  # BADLAR guardada + spread A
    badlar = next(s for s in evaluacion.snapshots if s.fuente == "badlar")
    assert badlar.origen == "ultimo_guardado"


def test_casos_precargados_se_suman_y_no_se_duplican(tmp_path: Path):
    repo_archivo = tmp_path / "casos.yaml"
    repo_archivo.write_text(
        f'pyme:\n  - {{cuit: "{CUIT_PYME}", monto: 1000000, plazo_meses: 12}}\nmonotributista: []\n'
    )
    local = tmp_path / "casos.local.yaml"
    local.write_text(
        f'monotributista:\n  - cuit: "{CUIL_MONOTRIBUTISTA}"\n    monto: 500000\n    plazo_meses: 6\n'
        "    documentos: {categoria_monotributo: D}\n"
    )
    servicio, _ = armar(FabricaFalsa(BASE_MONOTRIBUTISTA))

    nuevos = servicio.cargar_precargados([repo_archivo, local, tmp_path / "no_existe.yaml"])
    assert servicio.cargar_precargados([repo_archivo, local]) == []

    assert nuevos == [f"pre-pyme-{CUIT_PYME}", f"pre-monotributista-{CUIL_MONOTRIBUTISTA}"]
    mono = servicio.repositorio.solicitud(nuevos[1])
    assert mono.origen == "precargado" and mono.consentimiento
    assert mono.documentos == {"categoria_monotributo": "D"}


def test_el_archivo_de_casos_versionado_carga():
    servicio, _ = armar()
    nuevos = servicio.cargar_precargados([Path(__file__).parents[1] / "config" / "casos_precargados.yaml"])

    assert "pre-pyme-30546689979" in nuevos


def test_texto_de_reglas():
    reglas, texto, metricas = armar()[0].texto_reglas("pyme")

    assert reglas.id == "pyme.v3" and "killers:" in texto and metricas["auc"] > 0.6


# --- agentes ---------------------------------------------------------------

import json  # noqa: E402

from adapters_falsos import ClaudeFalso  # noqa: E402

from relevo.clientes_claude import AgenteNoDisponible  # noqa: E402


def balance(ventas=240_000_000, confianza=0.95, cuit=CUIT_PYME):
    return json.dumps(
        {
            "tipo": "balance",
            "campos": {
                "cuit": {"valor": cuit, "confianza": 0.99},
                "ventas_anuales": {"valor": ventas, "confianza": confianza},
            },
        }
    )


def con_agente(tmp_path, *respuestas):
    servicio, fabrica = armar()
    servicio.cliente_claude = ClaudeFalso(*respuestas)
    servicio.directorio_documentos = tmp_path
    return servicio


def test_alta_con_documento_lo_extrae_y_lo_usa_el_motor(tmp_path):
    servicio = con_agente(tmp_path, balance())

    id = servicio.alta(CUIT_PYME, "pyme", 10_000_000, 12, archivos=[("Balance 2025.pdf", b"%PDF-1.4")])
    evaluacion = servicio.repositorio.evaluacion(servicio.evaluar(id)).evaluacion

    docs = servicio.repositorio.documentos(id)
    assert [d.archivo for d in docs] == ["Balance 2025.pdf"]
    assert Path(docs[0].ruta).read_bytes() == b"%PDF-1.4"
    assert Path(docs[0].ruta).parent == tmp_path / id
    assert docs[0].extraido["tipo"] == "balance"
    assert servicio.repositorio.solicitud(id).documentos == {
        "ventas_anuales": 240_000_000.0,
        "cuit_coincide": True,
        "confianza_minima": 0.95,
    }
    assert evaluacion.veredicto is Veredicto.APROBADO


def test_baja_confianza_documental_deriva(tmp_path):
    servicio = con_agente(tmp_path, balance(confianza=0.5))

    id = servicio.alta(CUIT_PYME, "pyme", 10_000_000, 12, archivos=[("b.pdf", b"%PDF")])
    evaluacion = servicio.repositorio.evaluacion(servicio.evaluar(id)).evaluacion

    assert evaluacion.veredicto is Veredicto.DERIVAR
    assert any(p.regla == "documentos_baja_confianza" and p.resultado == "salta" for p in evaluacion.traza)


def test_agente_caido_registra_el_error_y_se_puede_reextraer(tmp_path):
    servicio = con_agente(tmp_path, AgenteNoDisponible("Claude Code no respondió"), balance())

    id = servicio.alta(CUIT_PYME, "pyme", 10_000_000, 12, archivos=[("b.pdf", b"%PDF")])

    doc = servicio.repositorio.documentos(id)[0]
    assert doc.error == "Claude Code no respondió" and doc.extraido is None
    assert servicio.repositorio.solicitud(id).documentos is None  # sin documentos → sin antecedentes
    assert servicio.repositorio.evaluacion(servicio.evaluar(id)).evaluacion.veredicto is Veredicto.DERIVAR

    servicio.extraer_documentos(id)

    assert servicio.repositorio.documentos(id)[0].error is None
    assert servicio.repositorio.solicitud(id).documentos["ventas_anuales"] == 240_000_000.0


def test_la_carga_manual_pisa_al_agente(tmp_path):
    servicio = con_agente(tmp_path, balance(ventas=1))

    id = servicio.alta(
        CUIT_PYME, "pyme", 10_000_000, 12, archivos=[("b.pdf", b"%PDF")], campos_manuales={"ventas_anuales": 5e8}
    )

    assert [d.archivo for d in servicio.repositorio.documentos(id)] == ["carga manual", "b.pdf"]
    assert servicio.repositorio.solicitud(id).documentos["ventas_anuales"] == 5e8


def test_rechaza_tipos_de_archivo_no_soportados(tmp_path):
    servicio = con_agente(tmp_path, balance())

    with pytest.raises(SolicitudInvalida):
        servicio.alta(CUIT_PYME, "pyme", 10_000_000, 12, archivos=[("macro.xlsm", b"x")])
    assert servicio.repositorio.bandeja() == []


def test_borrar_elimina_los_archivos(tmp_path):
    servicio = con_agente(tmp_path, balance())
    id = servicio.alta(CUIT_PYME, "pyme", 10_000_000, 12, archivos=[("../../b.pdf", b"%PDF")])
    assert (tmp_path / id / "01-b.pdf").is_file()  # el nombre no escapa del directorio

    servicio.borrar(id)

    assert not (tmp_path / id).exists()
    assert not servicio.repositorio.existe_solicitud(id)


def test_informe_y_chat(tmp_path):
    servicio = con_agente(tmp_path, "RESUMEN\n- Aprobado.", "Por el rating A.")
    evaluacion_id = servicio.evaluar(servicio.alta(CUIT_PYME, "pyme", 10_000_000, 12, DOCUMENTOS_PYME))

    assert servicio.generar_informe(evaluacion_id) == "RESUMEN\n- Aprobado."
    assert servicio.preguntar(evaluacion_id, "  ¿Por qué?  ") == "Por el rating A."

    assert servicio.repositorio.informe(evaluacion_id) == "RESUMEN\n- Aprobado."
    chat = servicio.repositorio.chat(evaluacion_id)
    assert [(m.rol, m.texto) for m in chat] == [("analista", "¿Por qué?"), ("agente", "Por el rating A.")]


def test_sin_agente_el_informe_falla_sin_guardar_nada():
    servicio, _ = armar()  # sin cliente: SinAgente
    evaluacion_id = servicio.evaluar(servicio.alta(CUIT_PYME, "pyme", 10_000_000, 12, DOCUMENTOS_PYME))

    with pytest.raises(AgenteNoDisponible):
        servicio.generar_informe(evaluacion_id)
    with pytest.raises(AgenteNoDisponible):
        servicio.preguntar(evaluacion_id, "¿Por qué?")
    assert servicio.repositorio.informe(evaluacion_id) is None
    assert servicio.repositorio.chat(evaluacion_id) == []
