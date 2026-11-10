"""ConsultorReal: precarga, pagas perezosas, reintentos, reutilización y fallback."""

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from relevo.consultor import Disponible, NoDisponible, SinAntecedentes, Snapshot
from relevo.consultor_real import (
    AlmacenEnMemoria,
    ConfigFuente,
    ConsultorReal,
    cargar_config_fuentes,
)
from relevo.fuentes import FuenteCaida

AHORA = datetime(2026, 11, 10, 9, 0)
GRATIS = ConfigFuente(costo="gratuita", reintentos=1, espera=2)
PAGA = ConfigFuente(costo="paga", reintentos=1, espera=2, reutilizar_dias=30)


class AdapterFalso:
    def __init__(self, nombre, respuestas=None, al_consultar=None):
        self.nombre = nombre
        self.respuestas = list(respuestas or [{"valor": 1}])
        self.al_consultar = al_consultar
        self.llamadas = 0

    def consultar(self, identificacion, timeout):
        self.llamadas += 1
        if self.al_consultar:
            self.al_consultar()
        respuesta = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    def traducir(self, crudo):
        if crudo is None:
            return SinAntecedentes()
        return Disponible(dict(crudo))


def armar(adapters, config, precargar=(), almacen=None, clave="sol-1"):
    esperas = []
    consultor = ConsultorReal(
        identificacion="30546689979",
        adapters={a.nombre: a for a in adapters},
        config=config,
        almacen=almacen or AlmacenEnMemoria(),
        clave_solicitud=clave,
        precargar=precargar,
        ahora=lambda: AHORA,
        dormir=esperas.append,
    )
    return consultor, esperas


def test_precarga_las_gratuitas_en_paralelo():
    barrera = threading.Barrier(2, timeout=2)  # solo pasa si las dos consultas corren a la vez
    a = AdapterFalso("a", al_consultar=barrera.wait)
    b = AdapterFalso("b", al_consultar=barrera.wait)

    consultor, _ = armar([a, b], {"a": GRATIS, "b": GRATIS}, precargar=["a", "b"])

    assert consultor.variables("a") == Disponible({"valor": 1})
    assert consultor.variables("b") == Disponible({"valor": 1})
    assert (a.llamadas, b.llamadas) == (1, 1)


def test_la_paga_no_se_consulta_hasta_que_se_pide():
    gratis, paga = AdapterFalso("gratis"), AdapterFalso("paga")

    consultor, _ = armar([gratis, paga], {"gratis": GRATIS, "paga": PAGA}, precargar=["gratis", "paga"])
    snapshots = consultor.snapshots()

    assert paga.llamadas == 0
    assert [s.fuente for s in snapshots] == ["gratis"]


def test_cada_fuente_se_consulta_una_vez():
    paga = AdapterFalso("paga")
    consultor, _ = armar([paga], {"paga": PAGA})

    consultor.variables("paga")
    consultor.variables("paga")

    assert paga.llamadas == 1


def test_reintenta_ante_fuente_caida():
    adapter = AdapterFalso("a", [FuenteCaida("HTTP 503"), {"valor": 2}])
    consultor, esperas = armar([adapter], {"a": GRATIS})

    assert consultor.variables("a") == Disponible({"valor": 2})
    assert esperas == [2]
    assert consultor.snapshots()[0].intentos == 2


def test_agotados_los_reintentos_queda_no_disponible():
    adapter = AdapterFalso("a", [FuenteCaida("timeout (10 s)")])
    consultor, esperas = armar([adapter], {"a": GRATIS})

    assert consultor.variables("a") == NoDisponible("timeout (10 s) (después de 2 intentos)")
    assert adapter.llamadas == 2
    assert esperas == [2]


def test_un_error_inesperado_no_se_reintenta_ni_se_propaga():
    adapter = AdapterFalso("a", [KeyError("campo")])
    consultor, _ = armar([adapter], {"a": GRATIS})

    resultado = consultor.variables("a")

    assert isinstance(resultado, NoDisponible) and "KeyError" in resultado.motivo
    assert adapter.llamadas == 1


def test_una_respuesta_que_no_se_puede_traducir_es_no_disponible():
    adapter = AdapterFalso("a", ["no soy un dict"])
    consultor, _ = armar([adapter], {"a": GRATIS})

    resultado = consultor.variables("a")

    assert isinstance(resultado, NoDisponible) and "traducir" in resultado.motivo


def test_fuente_sin_adapter():
    consultor, _ = armar([], {})

    assert consultor.variables("bureau") == NoDisponible("fuente no configurada")


@pytest.mark.parametrize("dias, reutiliza", [(10, True), (40, False)])
def test_reutiliza_el_snapshot_pago_reciente_de_la_misma_solicitud(dias, reutiliza):
    almacen = AlmacenEnMemoria()
    previo = Snapshot("paga", AHORA - timedelta(days=dias), {"valor": 9}, Disponible({"valor": 9}))
    almacen.guardar("sol-1", previo)
    paga = AdapterFalso("paga")

    consultor, _ = armar([paga], {"paga": PAGA}, almacen=almacen)
    resultado = consultor.variables("paga")

    assert (resultado == Disponible({"valor": 9})) is reutiliza
    assert paga.llamadas == (0 if reutiliza else 1)
    assert consultor.snapshots()[0].origen == ("reutilizado" if reutiliza else "consulta")


def test_no_reutiliza_snapshots_de_otra_solicitud():
    almacen = AlmacenEnMemoria()
    almacen.guardar("sol-otra", Snapshot("paga", AHORA, {"valor": 9}, Disponible({"valor": 9})))
    paga = AdapterFalso("paga")

    armar([paga], {"paga": PAGA}, almacen=almacen)[0].variables("paga")

    assert paga.llamadas == 1


def test_guarda_lo_consultado_para_la_proxima_evaluacion():
    almacen = AlmacenEnMemoria()
    armar([AdapterFalso("paga")], {"paga": PAGA}, almacen=almacen)[0].variables("paga")

    paga = AdapterFalso("paga")
    armar([paga], {"paga": PAGA}, almacen=almacen)[0].variables("paga")

    assert paga.llamadas == 0


BADLAR = ConfigFuente(alcance="global", si_falla_usar_ultimo=True, reintentos=0)


def test_badlar_caida_usa_el_ultimo_valor_marcado_desactualizado():
    almacen = AlmacenEnMemoria()
    armar([AdapterFalso("badlar", [{"tasa": 21.5}])], {"badlar": BADLAR}, almacen=almacen, clave="sol-1")[0].variables(
        "badlar"
    )

    caida = AdapterFalso("badlar", [FuenteCaida("HTTP 502")])
    consultor, _ = armar([caida], {"badlar": BADLAR}, almacen=almacen, clave="sol-2")

    assert consultor.variables("badlar") == Disponible({"tasa": 21.5, "desactualizada": True})
    snapshot = consultor.snapshots()[0]
    assert snapshot.origen == "ultimo_guardado"
    assert "HTTP 502" in snapshot.nota


def test_badlar_caida_sin_valor_guardado_es_no_disponible():
    consultor, _ = armar([AdapterFalso("badlar", [FuenteCaida("HTTP 502")])], {"badlar": BADLAR})

    assert isinstance(consultor.variables("badlar"), NoDisponible)


def test_config_por_defecto():
    config = cargar_config_fuentes()

    assert config["bureau"].paga and config["bureau"].reutilizar_dias == 30
    assert not config["cendeu"].paga and config["cendeu"].timeout == 10 and config["cendeu"].reintentos == 1
    assert config["badlar"].alcance == "global" and config["badlar"].si_falla_usar_ultimo


def test_config_invalida(tmp_path: Path):
    archivo = tmp_path / "fuentes.yaml"
    archivo.write_text("bureau: {costo: paga, reintentoz: 3}")

    with pytest.raises(ValueError, match="bureau"):
        cargar_config_fuentes(archivo)
