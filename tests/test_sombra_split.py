"""Modelos en sombra y split entre modelos (ADR 0010)."""

import json
import shutil
from collections import Counter
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from test_modelo_ml import CUIT_CON_DATOS, _modelos, _ok, _servicio

from conftest import BASE_PYME, armar_consultor, solicitud_pyme
from relevo.consultor import NoDisponible
from relevo.consultor_real import AlmacenEnMemoria, ConfigFuente, ConsultorReal
from relevo.diff_reglas import diferencias
from relevo.dominio import Solicitud, Veredicto
from relevo.identificacion import es_valido
from relevo.modelo_prueba import crear_app
from relevo.motor import evaluar
from relevo.presentacion import flujo
from relevo.reglas import DIRECTORIO_MODELOS, DIRECTORIO_REGLAS, ReglasInvalidas, reglas_desde_dict
from relevo.repositorio import Repositorio
from relevo.versiones_reglas import VersionesReglas

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the .timeout. argument")

V1, V2, PUNTOS = "pyme-prueba.v1.json", "pyme-prueba.v2.json", "pyme.v2.json"
CUITS = [c for c in (f"30{i:09d}" for i in range(7000)) if es_valido(c)][:600]


@pytest.fixture(scope="module")
def versiones():
    versiones = VersionesReglas(Repositorio())
    versiones.importar(DIRECTORIO_REGLAS)
    return versiones


def _reglas(versiones, **scorecard):
    contenido = versiones.contenido("pyme")
    contenido["scorecard"] = {"si_no_disponible": "derivar", **scorecard}
    return versiones.candidata("pyme", contenido)[0]


def _split(versiones, a=V1, b=V2, peso_a=50, **extra):
    return _reglas(versiones, split=[{"modelo": a, "peso": peso_a}, {"modelo": b, "peso": 100 - peso_a}], **extra)


# --- reglas ----------------------------------------------------------------------------


def test_un_split_reparte_entre_sus_modelos(versiones):
    reglas = _split(versiones)

    assert [b.scorecard.id for b in reglas.split] == ["pyme-prueba@1", "pyme-prueba@2"]
    assert reglas.scorecard.id == "pyme-prueba@1"
    assert reglas.semilla == f"split:{V1},{V2}"
    assert {m.id for m in reglas.modelos_externos} == {"pyme-prueba@1", "pyme-prueba@2"}


def test_la_asignacion_es_por_cuit_estable_y_respeta_los_pesos(versiones):
    reglas = _split(versiones, peso_a=70)

    asignados = Counter(reglas.asignar(c).id for c in CUITS)

    assert asignados["pyme-prueba@1"] / len(CUITS) == pytest.approx(0.7, abs=0.06)
    assert all(reglas.asignar(c) is reglas.asignar(c) for c in CUITS[:50])
    assert reglas.asignar("30-00000001-9") is reglas.asignar("30000000019")


def test_otra_semilla_reparte_distinto(versiones):
    a, b = _split(versiones), _split(versiones, semilla="experimento-2")

    assert sum(a.asignar(c).id != b.asignar(c).id for c in CUITS) > len(CUITS) / 4


def test_sombra_con_modelos_de_puntos_y_de_ml(versiones):
    reglas = _reglas(versiones, modelo=V1, sombra=[V2, PUNTOS])

    assert [s.id for s in reglas.sombra] == ["pyme-prueba@2", PUNTOS]
    assert "bureau" in reglas.fuentes  # las del scorecard de puntos en sombra se precargan si son gratuitas


@pytest.mark.parametrize(
    "scorecard, error",
    [
        ({"modelo": V1, "split": [{"modelo": V2, "peso": 1}]}, "modelo o split"),
        ({"split": [{"modelo": V1, "peso": 1}]}, "al menos dos"),
        ({"split": [{"modelo": V1, "peso": 0}, {"modelo": V2, "peso": 1}]}, "mayores que cero"),
        ({"modelo": V1, "sombra": [V1]}, "más de una vez"),
        ({"modelo": V1, "sombra": [{"modelo": V2}]}, "lista de modelos"),
        (
            {"split": [{"modelo": V1, "peso": 1}, {"modelo": PUNTOS, "peso": 1}], "si_no_disponible": "continuar"},
            "derivar o rechazar",
        ),
    ],
)
def test_splits_y_sombras_invalidos(versiones, scorecard, error):
    contenido = versiones.contenido("pyme")
    contenido["scorecard"] = scorecard
    with pytest.raises(ReglasInvalidas, match=error):
        versiones.candidata("pyme", contenido)


def test_los_modelos_del_split_tienen_que_tener_los_mismos_ratings(tmp_path, versiones):
    shutil.copytree(DIRECTORIO_MODELOS, tmp_path, dirs_exist_ok=True)
    otro = json.loads((tmp_path / V2).read_text())
    otro["ratings"] = [{"rating": "X", "pd_hasta": 0.5}, {"rating": "Y", "pd_hasta": 1}]
    (tmp_path / V2).write_text(json.dumps(otro))
    datos = {"flujo": "pyme", "version": 9, **versiones.contenido("pyme")}
    datos["scorecard"] = {"split": [{"modelo": V1, "peso": 1}, {"modelo": V2, "peso": 1}]}

    with pytest.raises(ReglasInvalidas, match="mismos ratings"):
        reglas_desde_dict(datos, tmp_path)


def test_el_diff_muestra_split_y_sombra(versiones):
    vigente = versiones.reglas("pyme")

    cambios = {c.que: (c.antes, c.despues) for c in diferencias(vigente, _split(versiones, sombra=[PUNTOS]))}

    assert cambios["Scorecard · split"] == ("pyme.v2.json", f"{V1} 50 % · {V2} 50 %")
    assert cambios["Scorecard · modelos en sombra"] == (None, PUNTOS)


# --- motor --------------------------------------------------------------------------------


@dataclass
class ConsultorConCosto:
    """ConsultorFalso que sabe qué fuentes son pagas y anota cuáles se pidieron."""

    base: object
    pagas: set = field(default_factory=lambda: {"bureau"})

    def variables(self, fuente):
        return self.base.variables(fuente)

    def snapshots(self):
        return self.base.snapshots()

    def es_paga(self, fuente):
        return fuente in self.pagas


def _cuit_para(reglas, modelo_id):
    return next(c for c in CUITS if reglas.asignar(c).id == modelo_id)


@pytest.mark.parametrize("decide", ["pyme-prueba@1", "pyme-prueba@2"])
def test_con_split_decide_el_asignado_y_el_otro_queda_en_sombra(versiones, decide):
    reglas = _split(versiones)
    cuit = _cuit_para(reglas, decide)
    modelos = _modelos({"pyme-prueba@1": _ok(0.03), "pyme-prueba@2": _ok(0.15, version="2")})

    evaluacion = evaluar(Solicitud(cuit, "pyme", 10_000_000, 12), reglas, armar_consultor(BASE_PYME, modelo_ml=modelos))

    pd_decide, pd_otro = (0.03, 0.15) if decide.endswith("1") else (0.15, 0.03)
    assert evaluacion.pd == pd_decide
    rating = next(p for p in evaluacion.traza if p.etapa == "rating")
    assert rating.detalle["split"]["asignado"] == decide
    (sombra,) = [p for p in evaluacion.traza if p.etapa == "sombra"]
    assert sombra.resultado == "ok" and sombra.detalle["pd"] == pd_otro and sombra.regla != decide


def test_la_sombra_de_puntos_no_consulta_fuentes_pagas(versiones):
    reglas = _reglas(versiones, modelo=V1, sombra=[PUNTOS])
    falso = armar_consultor(BASE_PYME, modelo_ml=_modelos({"pyme-prueba@1": _ok(0.03)}))

    evaluacion = evaluar(solicitud_pyme(), reglas, ConsultorConCosto(falso))

    assert "bureau" not in falso.pedidas
    (sombra,) = [p for p in evaluacion.traza if p.etapa == "sombra"]
    assert sombra.resultado == "ok" and sombra.regla == PUNTOS
    assert sombra.detalle["sin_dato"]  # las características del bureau suman sus puntos de sin dato
    assert 0 < sombra.detalle["pd"] < 1 and sombra.detalle["rating"] in "ABCDE"


def test_la_sombra_usa_una_fuente_paga_si_ya_se_consulto(versiones):
    reglas = _reglas(versiones, modelo=PUNTOS, si_no_disponible="continuar", sombra=[V1])
    falso = armar_consultor(BASE_PYME, modelo_ml=_modelos({"pyme-prueba@1": _ok(0.08)}))

    evaluacion = evaluar(solicitud_pyme(), reglas, ConsultorConCosto(falso))

    (sombra,) = [p for p in evaluacion.traza if p.etapa == "sombra"]
    assert (sombra.regla, sombra.detalle["pd"], sombra.detalle["rating"]) == ("pyme-prueba@1", 0.08, "C")
    assert evaluacion.puntos is not None  # decidió el de puntos


def test_la_sombra_se_calcula_aunque_el_que_decide_no_tenga_pd(versiones):
    reglas = _reglas(versiones, modelo=V1, sombra=[V2])
    modelos = _modelos(
        {"pyme-prueba@1": {"estado": "no_disponible", "motivo": "caído"}, "pyme-prueba@2": _ok(0.04, "2")}
    )

    evaluacion = evaluar(solicitud_pyme(), reglas, armar_consultor(BASE_PYME, modelo_ml=modelos))

    assert evaluacion.veredicto is Veredicto.DERIVAR and evaluacion.pd is None
    (sombra,) = [p for p in evaluacion.traza if p.etapa == "sombra"]
    assert sombra.detalle["pd"] == 0.04


def test_una_sombra_sin_pd_no_corta_la_evaluacion(versiones):
    reglas = _reglas(versiones, modelo=V1, sombra=[V2])
    modelos = _modelos({"pyme-prueba@1": _ok(0.03), "pyme-prueba@2": {"estado": "sin_antecedentes"}})

    evaluacion = evaluar(solicitud_pyme(), reglas, armar_consultor(BASE_PYME, modelo_ml=modelos))

    assert evaluacion.veredicto is Veredicto.APROBADO
    (sombra,) = [p for p in evaluacion.traza if p.etapa == "sombra"]
    assert sombra.resultado == "sin dato"


def test_las_reglas_ven_la_pd_del_que_decide(versiones):
    contenido = versiones.contenido("pyme")
    contenido["scorecard"] = {"split": [{"modelo": V1, "peso": 1}, {"modelo": V2, "peso": 1}]}
    contenido["derivaciones"].append({"id": "pd_alta", "descripcion": "PD alta", "condicion": "modelo_ml.pd > 0.1"})
    reglas = versiones.candidata("pyme", contenido)[0]
    modelos = _modelos({"pyme-prueba@1": _ok(0.03), "pyme-prueba@2": _ok(0.15, "2")})

    def veredicto(modelo_id):
        cuit = _cuit_para(reglas, modelo_id)
        return evaluar(Solicitud(cuit, "pyme", 10_000_000, 12), reglas, armar_consultor(BASE_PYME, modelo_ml=modelos))

    assert "pd_alta" not in " ".join(veredicto("pyme-prueba@1").motivos)
    assert "pd_alta" in " ".join(veredicto("pyme-prueba@2").motivos)


def test_la_traza_del_caso_muestra_la_sombra_y_el_split(versiones):
    reglas = _split(versiones)
    modelos = _modelos({"pyme-prueba@1": _ok(0.03), "pyme-prueba@2": _ok(0.15, "2")})
    cuit = _cuit_para(reglas, "pyme-prueba@1")

    etapas = {
        e.clave: e
        for e in flujo(
            evaluar(Solicitud(cuit, "pyme", 10_000_000, 12), reglas, armar_consultor(BASE_PYME, modelo_ml=modelos))
        )
    }

    assert "split: le tocó pyme-prueba@1" in etapas["rating"].resumen
    assert etapas["sombra"].resumen == "pyme-prueba@2: D · PD 15,00 %"
    sin_sombra = flujo(evaluar(solicitud_pyme(), versiones.reglas("pyme"), armar_consultor(BASE_PYME)))
    assert "sombra" not in {e.clave for e in sin_sombra}


# --- consultor y servicio --------------------------------------------------------------------


def test_el_consultor_real_sabe_cuales_son_pagas():
    consultor = ConsultorReal(
        "30712345671", {}, {"bureau": ConfigFuente(costo="paga")}, AlmacenEnMemoria(), "x", precargar=()
    )

    assert consultor.es_paga("bureau") and not consultor.es_paga("cendeu")


def test_el_servicio_consulta_todos_los_modelos_en_una_fuente():
    servicio = _servicio(TestClient(crear_app()))
    contenido = servicio.versiones.contenido("pyme")
    contenido["scorecard"] = {"modelo": V1, "sombra": [V2], "si_no_disponible": "derivar"}
    servicio.versiones.publicar("pyme", contenido, "test", "sombra", base=4)
    id = servicio.alta(CUIT_CON_DATOS, "pyme", 10_000_000, 12, {"ventas_anuales": 240_000_000})

    evaluacion = servicio.repositorio.evaluacion(servicio.evaluar(id)).evaluacion

    (snapshot,) = [s for s in evaluacion.snapshots if s.fuente == "modelo_ml"]
    assert [r["pedido"]["version"] for r in snapshot.crudo["respuestas"]] == ["1", "2"]
    (sombra,) = [p for p in evaluacion.traza if p.etapa == "sombra"]
    assert sombra.regla == "pyme-prueba@2" and sombra.resultado == "ok"


def test_avisos_cubren_los_modelos_en_sombra():
    servicio = _servicio(TestClient(crear_app(modelos=[{"modelo": "pyme-prueba", "version": "1", "segmento": "pyme"}])))
    contenido = servicio.versiones.contenido("pyme")
    contenido["scorecard"]["sombra"] = [V2]
    servicio.versiones.guardar_borrador("pyme", contenido, 4)

    assert servicio.avisos_modelo("pyme") == ["el servicio no publica pyme-prueba@2"]


def test_sin_url_la_sombra_queda_no_evaluable(versiones):
    reglas = _reglas(versiones, modelo=PUNTOS, si_no_disponible="continuar", sombra=[V1])

    evaluacion = evaluar(solicitud_pyme(), reglas, armar_consultor(BASE_PYME, modelo_ml=NoDisponible("sin URL")))

    assert evaluacion.veredicto is Veredicto.APROBADO
    (sombra,) = [p for p in evaluacion.traza if p.etapa == "sombra"]
    assert sombra.resultado == "no evaluable" and "sin URL" in sombra.detalle["motivo"]
