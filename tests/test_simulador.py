"""Simulador de políticas: el motor con reglas candidatas sobre los snapshots guardados."""

from dataclasses import replace
from datetime import datetime

import pytest
from test_monitoreo import _otorgada, _registro

from conftest import BASE_PYME, armar_consultor, solicitud_pyme
from relevo.dominio import Veredicto
from relevo.monitoreo import calcular
from relevo.motor import evaluar
from relevo.reglas import DIRECTORIO_REGLAS, ReglasInvalidas
from relevo.repositorio import EvaluacionGuardada, Repositorio
from relevo.simulador import CambioInvalido, aplicar_cambios, factores_de_correccion, simular
from relevo.versiones_reglas import VersionesReglas


@pytest.fixture(scope="module")
def versiones():
    versiones = VersionesReglas(Repositorio())
    versiones.importar(DIRECTORIO_REGLAS)
    return versiones


def _caso(versiones, id: str, **cambios) -> EvaluacionGuardada:
    """Una evaluación con todas las fuentes guardadas, como cuando la app las consulta al inicio."""
    consultor = armar_consultor(BASE_PYME, **cambios)
    for fuente in BASE_PYME:
        consultor.variables(fuente)
    evaluacion = evaluar(solicitud_pyme(), versiones.reglas("pyme"), consultor)
    return EvaluacionGuardada(1, id, datetime(2026, 1, 5), evaluacion, None)


def _simular(versiones, casos, operaciones=(), otorgadas=(), registros=()):
    candidata, cambios = versiones.candidata("pyme", aplicar_cambios(versiones.contenido("pyme"), operaciones))
    return simular("pyme", casos, versiones.reglas("pyme"), candidata, cambios, otorgadas, registros)


def test_con_las_mismas_reglas_reproduce_las_evaluaciones(versiones):
    casos = [_caso(versiones, "a"), _caso(versiones, "b", propios={"lista_negra": True})]

    simulacion = _simular(versiones, casos)

    assert simulacion.matriz == {("APROBADO", "APROBADO"): 1, ("RECHAZADO", "RECHAZADO"): 1}
    assert simulacion.vigente == replace(simulacion.candidata, reglas="pyme.v3")
    assert simulacion.cambios == [] and simulacion.no_evaluables == {}


def test_rechazar_un_rating_saca_sus_aprobadas(versiones):
    caso = _caso(versiones, "a")
    rating = caso.evaluacion.rating
    rechazar = sorted({rating, "E"})

    simulacion = _simular(versiones, [caso], [{"op": "poner", "ruta": "politica.rechazar_ratings", "valor": rechazar}])

    assert simulacion.matriz == {("APROBADO", "RECHAZADO"): 1}
    assert (simulacion.vigente.aprobadas, simulacion.candidata.aprobadas) == (1, 0)
    assert simulacion.salen.solicitudes == 1 and simulacion.entran.solicitudes == 0
    assert [c.que for c in simulacion.cambios] == [f"Rating {rating} · decisión"]


def test_los_que_salen_traen_su_default_real(versiones):
    casos = [_caso(versiones, f"s{i}") for i in range(40)]
    otorgadas = [_otorgada(f"s{i}", casos[0].evaluacion.rating, casos[0].evaluacion.pd) for i in range(40)]
    registros = [_registro(f"s{i}", 8, {6: 90} if i < 4 else None) for i in range(40)]
    rechazar = sorted({casos[0].evaluacion.rating, "E"})

    simulacion = _simular(
        versiones,
        casos,
        [{"op": "poner", "ruta": "politica.rechazar_ratings", "valor": rechazar}],
        otorgadas,
        registros,
    )

    assert simulacion.salen.con_credito.creditos == 40 and simulacion.salen.con_credito.observados == 4


def test_una_fuente_que_no_se_consulto_deja_la_solicitud_afuera(versiones):
    sin_igj = _caso(versiones, "a")
    snapshots = tuple(s for s in sin_igj.evaluacion.snapshots if s.fuente != "igj")
    sin_igj = replace(sin_igj, evaluacion=replace(sin_igj.evaluacion, snapshots=snapshots))

    simulacion = _simular(versiones, [sin_igj, _caso(versiones, "b")])

    assert simulacion.no_evaluables == {"igj": 1}
    assert simulacion.vigente.evaluadas == 1


def test_los_que_entran_solo_tienen_pd_estimada(versiones):
    rechazado = _caso(versiones, "a", propios={"lista_negra": True})
    assert rechazado.evaluacion.veredicto is Veredicto.RECHAZADO

    simulacion = _simular(versiones, [rechazado], [{"op": "quitar_regla", "seccion": "killers", "id": "lista_negra"}])

    assert simulacion.entran.solicitudes == 1 and simulacion.entran.con_credito is None
    assert simulacion.entran.pd_promedio is not None


def test_la_pd_se_corrige_con_el_o_e_del_monitoreo(versiones):
    caso = _caso(versiones, "a")
    rating, pd = caso.evaluacion.rating, caso.evaluacion.pd
    otorgadas = [_otorgada(f"o{i}", rating, 0.05) for i in range(100)]
    registros = [_registro(f"o{i}", 12, {6: 90} if i < 15 else None) for i in range(100)]

    simulacion = _simular(versiones, [caso], otorgadas=otorgadas, registros=registros)

    factor = simulacion.factores[rating]
    assert factor == pytest.approx(3, rel=0.01)
    assert simulacion.vigente.pd_corregida_promedio == pytest.approx(min(1, pd * factor))


def test_factores_para_ratings_sin_datos():
    otorgadas = [_otorgada(f"b{i}", "B", 0.04) for i in range(100)]
    registros = [_registro(f"b{i}", 12, {6: 90} if i < 8 else None) for i in range(100)]

    factores = factores_de_correccion(calcular(otorgadas, registros).por_rating)

    assert set(factores) == {"B", "*"} and factores["B"] == factores["*"] == pytest.approx(2, rel=0.01)


# --- cambios a las reglas ------------------------------------------------------

CONTENIDO = {
    "killers": [{"id": "k1", "condicion": "a"}, {"id": "k2", "condicion": "b"}],
    "politica": {"rechazar_ratings": ["E"]},
}


def test_aplicar_cambios_no_toca_el_original():
    resultado = aplicar_cambios(
        CONTENIDO,
        [
            {"op": "poner", "ruta": "politica.rechazar_ratings", "valor": ["D", "E"]},
            {"op": "cambiar_regla", "seccion": "killers", "id": "k1", "campos": {"condicion": "x"}},
            {"op": "agregar_regla", "seccion": "killers", "regla": {"id": "k3", "condicion": "c"}, "despues_de": "k1"},
            {"op": "quitar_regla", "seccion": "killers", "id": "k2"},
            {"op": "agregar_regla", "seccion": "derivaciones", "regla": {"id": "d1", "condicion": "d"}},
        ],
    )

    assert resultado["politica"]["rechazar_ratings"] == ["D", "E"]
    assert [(k["id"], k["condicion"]) for k in resultado["killers"]] == [("k1", "x"), ("k3", "c")]
    assert resultado["derivaciones"] == [{"id": "d1", "condicion": "d"}]
    assert CONTENIDO["killers"][0]["condicion"] == "a" and len(CONTENIDO["killers"]) == 2


@pytest.mark.parametrize(
    "operacion, error",
    [
        ({"op": "borrar_todo"}, "operación desconocida"),
        ({"op": "poner", "ruta": "killers", "valor": []}, "ruta inválida"),
        ({"op": "poner", "ruta": "scorecard.modelo", "valor": "x"}, "no existe"),
        ({"op": "quitar_regla", "seccion": "killers", "id": "nada"}, "no hay una regla"),
        ({"op": "cambiar_regla", "seccion": "killers", "id": "k1", "campos": {"id": "k9"}}, "no se cambia"),
        ({"op": "agregar_regla", "seccion": "killers", "regla": {"id": "k1"}}, "ya hay una regla"),
        ({"op": "agregar_regla", "seccion": "reglas", "regla": {"id": "k9"}}, "la sección"),
    ],
)
def test_cambios_invalidos(operacion, error):
    with pytest.raises(CambioInvalido, match=error):
        aplicar_cambios(CONTENIDO, [operacion])


def test_la_candidata_se_valida_contra_el_catalogo(versiones):
    contenido = aplicar_cambios(
        versiones.contenido("pyme"),
        [
            {
                "op": "agregar_regla",
                "seccion": "killers",
                "regla": {"id": "x", "descripcion": "x", "condicion": "foo.bar"},
            }
        ],
    )

    with pytest.raises(ReglasInvalidas, match="foo.bar no está en el catálogo"):
        versiones.candidata("pyme", contenido)


def test_el_repositorio_da_la_ultima_evaluacion_de_cada_solicitud(tmp_path, versiones):
    repo = Repositorio(tmp_path / "relevo.sqlite")
    id = repo.crear_solicitud(solicitud_pyme())
    reglas = versiones.reglas("pyme")
    repo.guardar_evaluacion(id, evaluar(solicitud_pyme(), reglas, armar_consultor(BASE_PYME)))
    ultima = evaluar(solicitud_pyme(), reglas, armar_consultor(BASE_PYME, propios={"lista_negra": True}))
    repo.guardar_evaluacion(id, ultima)

    (guardada,) = repo.ultimas_evaluaciones("pyme")

    assert guardada.evaluacion.veredicto is Veredicto.RECHAZADO
    assert {s.fuente for s in guardada.evaluacion.snapshots} >= {"propios", "cendeu"}
    assert repo.ultimas_evaluaciones("monotributista") == []
