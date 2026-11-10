"""Repositorio SQLite y AlmacenSqlite."""

from datetime import datetime, timedelta

import pytest

from conftest import BASE_PYME, armar_consultor, solicitud_pyme
from relevo.consultor import ConsultorFalso, Disponible, NoDisponible, SinAntecedentes, Snapshot
from relevo.dominio import Evaluacion, Veredicto
from relevo.motor import evaluar
from relevo.repositorio import AlmacenSqlite, Repositorio, SolicitudInexistente

AHORA = datetime(2026, 11, 10, 9, 0)


@pytest.fixture
def repo():
    return Repositorio(":memory:")


def con_snapshots(evaluacion: Evaluacion, *snapshots: Snapshot) -> Evaluacion:
    from dataclasses import replace

    return replace(evaluacion, snapshots=snapshots)


def test_solicitud_ida_y_vuelta(repo):
    id = repo.crear_solicitud(solicitud_pyme(), {"ventas_anuales": 1}, origen="manual", ahora=AHORA)

    guardada = repo.solicitud(id)

    assert guardada.solicitud == solicitud_pyme()
    assert guardada.documentos == {"ventas_anuales": 1}
    assert guardada.creada_en == AHORA
    with pytest.raises(SolicitudInexistente):
        repo.solicitud("no-existe")


def test_evaluacion_ida_y_vuelta_identica(repo, reglas_pyme):
    id = repo.crear_solicitud(solicitud_pyme())
    original = evaluar(solicitud_pyme(), reglas_pyme, armar_consultor(BASE_PYME))

    evaluacion_id = repo.guardar_evaluacion(id, original, AHORA)
    guardada = repo.evaluacion(evaluacion_id)

    assert guardada.evaluacion == original
    assert guardada.solicitud_id == id


def test_la_evaluacion_guardada_se_puede_reproducir(repo, reglas_pyme):
    """Con la misma solicitud, las mismas reglas y los snapshots guardados, el veredicto es el mismo."""
    id = repo.crear_solicitud(solicitud_pyme())
    original = evaluar(solicitud_pyme(), reglas_pyme, armar_consultor(BASE_PYME, igj={"dada_de_baja": True}))
    guardada = repo.evaluacion(repo.guardar_evaluacion(id, original)).evaluacion

    reproducida = evaluar(
        guardada.solicitud, reglas_pyme, ConsultorFalso({s.fuente: s.resultado for s in guardada.snapshots})
    )

    assert reproducida.veredicto is guardada.veredicto is Veredicto.RECHAZADO
    assert reproducida.traza == guardada.traza


def test_bandeja_muestra_la_ultima_evaluacion(repo, reglas_pyme):
    sin_evaluar = repo.crear_solicitud(solicitud_pyme(), ahora=AHORA)
    evaluada = repo.crear_solicitud(solicitud_pyme(), ahora=AHORA + timedelta(minutes=1))
    repo.guardar_evaluacion(evaluada, evaluar(solicitud_pyme(), reglas_pyme, armar_consultor(BASE_PYME)))
    rechazada = evaluar(solicitud_pyme(), reglas_pyme, armar_consultor(BASE_PYME, propios={"lista_negra": True}))
    ultima = repo.guardar_evaluacion(evaluada, rechazada)

    filas = {f.solicitud.id: f for f in repo.bandeja()}

    assert filas[sin_evaluar].veredicto is None
    assert filas[evaluada].veredicto is Veredicto.RECHAZADO
    assert filas[evaluada].evaluacion_id == ultima
    assert [f.solicitud.id for f in repo.bandeja()] == [evaluada, sin_evaluar]  # más nueva primero


def test_la_denominacion_sale_de_cendeu_o_igj(repo, reglas_pyme):
    id = repo.crear_solicitud(solicitud_pyme())
    consultor = armar_consultor(BASE_PYME, cendeu={"denominacion": "YPF SOCIEDAD ANONIMA"})

    evaluacion_id = repo.guardar_evaluacion(id, evaluar(solicitud_pyme(), reglas_pyme, consultor))

    assert repo.evaluacion(evaluacion_id).denominacion == "YPF SOCIEDAD ANONIMA"


def test_borrar_solicitud_borra_todo(repo, reglas_pyme):
    id = repo.crear_solicitud(solicitud_pyme())
    evaluacion_id = repo.guardar_evaluacion(id, evaluar(solicitud_pyme(), reglas_pyme, armar_consultor(BASE_PYME)))
    repo.guardar_informe(evaluacion_id, "informe")
    repo.agregar_mensaje(evaluacion_id, "analista", "¿por qué?")
    repo.guardar_documento(id, "balance.pdf", "/tmp/balance.pdf", {"tipo": "balance"})

    repo.borrar_solicitud(id)

    assert repo.bandeja() == []
    for tabla in ("documentos", "evaluaciones", "snapshots", "informes", "chat"):
        assert repo._ejecutar(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0] == 0


def test_informe_chat_y_resolucion(repo, reglas_pyme):
    id = repo.crear_solicitud(solicitud_pyme())
    evaluacion_id = repo.guardar_evaluacion(id, evaluar(solicitud_pyme(), reglas_pyme, armar_consultor(BASE_PYME)))

    repo.guardar_informe(evaluacion_id, "v1")
    repo.guardar_informe(evaluacion_id, "v2")
    repo.agregar_mensaje(evaluacion_id, "analista", "hola")
    repo.agregar_mensaje(evaluacion_id, "agente", "chau")
    repo.guardar_resolucion(evaluacion_id, "aprobar", "garantía adicional", "Flor")

    assert repo.informe(evaluacion_id) == "v2"
    assert [(m.rol, m.texto) for m in repo.chat(evaluacion_id)] == [("analista", "hola"), ("agente", "chau")]
    assert repo.resolucion(evaluacion_id).decision == "aprobar"
    assert repo.evaluacion(evaluacion_id).evaluacion.veredicto is Veredicto.APROBADO  # no reemplaza el veredicto
    with pytest.raises(ValueError):
        repo.guardar_resolucion(evaluacion_id, "tal vez", "", "")


# --- AlmacenSqlite --------------------------------------------------------


def guardar_con(repo, reglas, solicitud_id, *snapshots):
    evaluacion = evaluar(solicitud_pyme(), reglas, armar_consultor(BASE_PYME))
    repo.guardar_evaluacion(solicitud_id, con_snapshots(evaluacion, *snapshots))


def test_almacen_devuelve_el_ultimo_snapshot_con_respuesta_de_la_solicitud(repo, reglas_pyme):
    a, b = repo.crear_solicitud(solicitud_pyme()), repo.crear_solicitud(solicitud_pyme())
    viejo = Snapshot("bureau", AHORA - timedelta(days=5), {"score": 1}, Disponible({"score": 1}))
    nuevo = Snapshot("bureau", AHORA - timedelta(days=1), {"score": 2}, Disponible({"score": 2}))
    caido = Snapshot("bureau", AHORA, {"error": "x"}, NoDisponible("x"))
    guardar_con(repo, reglas_pyme, a, viejo)
    guardar_con(repo, reglas_pyme, a, nuevo, caido)
    guardar_con(repo, reglas_pyme, b, Snapshot("bureau", AHORA, None, SinAntecedentes()))

    almacen = AlmacenSqlite(repo)

    assert almacen.ultimo("bureau", a) == nuevo
    assert almacen.ultimo("bureau", b).resultado == SinAntecedentes()
    assert almacen.ultimo("bureau", "otra") is None
    assert almacen.ultimo("bureau", "*").resultado == SinAntecedentes()  # global: de cualquier solicitud


def test_almacen_no_reusa_valores_que_ya_eran_de_respaldo(repo, reglas_pyme):
    a = repo.crear_solicitud(solicitud_pyme())
    real = Snapshot("badlar", AHORA - timedelta(days=3), {}, Disponible({"tasa": 21.5}))
    respaldo = Snapshot(
        "badlar", AHORA, {}, Disponible({"tasa": 21.5, "desactualizada": True}), origen="ultimo_guardado"
    )
    guardar_con(repo, reglas_pyme, a, real, respaldo)

    assert AlmacenSqlite(repo).ultimo("badlar", "*") == real


def test_documentos_de_la_solicitud(repo):
    id = repo.crear_solicitud(solicitud_pyme())

    doc_id = repo.guardar_documento(id, "balance.pdf", "/d/balance.pdf", None, "agente caído")
    repo.guardar_documento(id, "carga manual", None, {"tipo": "manual", "campos": {}})
    repo.actualizar_documento(doc_id, {"tipo": "balance", "campos": {}}, None)
    repo.actualizar_documentos(id, {"ventas_anuales": 1.0, "confianza_minima": 1.0})

    docs = repo.documentos(id)
    assert [(d.archivo, d.ruta, d.extraido["tipo"], d.error) for d in docs] == [
        ("balance.pdf", "/d/balance.pdf", "balance", None),
        ("carga manual", None, "manual", None),
    ]
    assert repo.solicitud(id).documentos == {"ventas_anuales": 1.0, "confianza_minima": 1.0}
