"""Presentación del caso: ficha del solicitante, informe por secciones y traza como flujo."""

from conftest import BASE_PYME, CUIT_PYME, armar_consultor, solicitud_pyme
from relevo.consultor import NoDisponible, SinAntecedentes
from relevo.motor import evaluar
from relevo.presentacion import ficha_solicitante, flujo, secciones_informe

# --- ficha -------------------------------------------------------------------


def datos(ficha):
    return {d.etiqueta: (d.valor, d.nota) for d in ficha.datos}


def test_ficha_pyme_con_fuentes_y_documentos(reglas_pyme):
    consultor = armar_consultor(BASE_PYME, cendeu={"denominacion": "METALÚRGICA DEL SUR S.A."})
    evaluacion = evaluar(solicitud_pyme(), reglas_pyme, consultor)
    extraidos = [{"campos": {"fecha_cierre_ejercicio": {"valor": "2025-12-31", "confianza": 0.9}}}]

    ficha = ficha_solicitante(CUIT_PYME, "pyme", 10_000_000, 12, evaluacion, extraidos)

    d = datos(ficha)
    assert ficha.nombre == "METALÚRGICA DEL SUR S.A."
    assert d["Razón social"] == ("METALÚRGICA DEL SUR S.A.", "BCRA")
    assert d["CUIT"] == ("30-71234567-1", "")
    assert d["Tipo"][0] == "Persona jurídica · PyME"
    assert d["Solicita"][0] == "$ 10.000.000 a 12 meses"
    assert d["Cierre del ejercicio"] == ("31/12/2025", "balance")
    assert d["Antigüedad de la sociedad"] == ("12 años", "igj")
    assert d["Deuda en el sistema"] == ("$ 20.000.000", "cendeu")
    assert d["Score de bureau"] == ("850", "bureau")


def test_ficha_con_fuentes_caidas_usa_el_nombre_de_los_documentos(reglas_pyme):
    consultor = armar_consultor(BASE_PYME, cendeu=NoDisponible("HTTP 503"), igj=SinAntecedentes())
    evaluacion = evaluar(solicitud_pyme(), reglas_pyme, consultor)
    extraidos = [{"campos": {"denominacion": {"valor": "Metalúrgica SA", "confianza": 0.9}}}]

    ficha = ficha_solicitante(CUIT_PYME, "pyme", 10_000_000, 12, evaluacion, extraidos)

    d = datos(ficha)
    assert d["Razón social"] == ("Metalúrgica SA", "documentos")
    assert d["Deuda en el sistema"] == ("—", "cendeu: no disponible")
    assert d["Antigüedad de la sociedad"] == ("—", "igj: sin antecedentes")


def test_ficha_sin_evaluacion():
    ficha = ficha_solicitante("27301234568", "monotributista", 2_000_000, 12, None, [])

    d = datos(ficha)
    assert ficha.nombre is None
    assert d["CUIL"][0] == "27-30123456-8"
    assert d["Tipo"][0] == "Persona humana · Monotributista"
    assert d["Edad"] == ("—", "no consultada")


# --- informe -----------------------------------------------------------------


def test_secciones_del_informe():
    texto = """RESUMEN
- Veredicto: DERIVAR.
- Rating D.

FUNDAMENTOS:
Texto suelto.
- **Bureau**: 394.

QUÉ VERIFICAR: reintentar CENDEU."""

    secciones = secciones_informe(texto)

    assert [s.titulo for s in secciones] == ["Resumen", "Fundamentos", "Qué verificar"]
    assert secciones[0].bloques == [("ul", ["Veredicto: DERIVAR.", "Rating D."])]
    assert secciones[1].bloques == [("p", "Texto suelto."), ("ul", ["Bureau: 394."])]
    assert secciones[2].bloques == [("p", "reintentar CENDEU.")]


def test_informe_sin_titulos_es_una_sola_seccion():
    secciones = secciones_informe("Un párrafo.\nOtro párrafo con BCRA en mayúsculas.")

    assert len(secciones) == 1 and secciones[0].titulo is None
    assert [b[0] for b in secciones[0].bloques] == ["p", "p"]


# --- flujo -------------------------------------------------------------------


def estados(etapas):
    return {e.clave: (e.estado, e.resumen) for e in etapas}


def test_flujo_aprobado(reglas_pyme):
    etapas = flujo(evaluar(solicitud_pyme(), reglas_pyme, armar_consultor(BASE_PYME)), reglas_pyme)

    e = estados(etapas)
    assert [x.clave for x in etapas] == [
        "fuente",
        "killer",
        "derivacion",
        "scorecard",
        "rating",
        "politica",
        "veredicto",
    ]
    assert e["killer"][0] == "ok" and "ninguno salta" in e["killer"][1]
    assert e["politica"] == ("ok", "ofrece $ 10.000.000")
    assert e["veredicto"] == ("ok", "APROBADO")
    killers = next(x for x in etapas if x.clave == "killer")
    assert killers.items[0].descripcion.startswith("CUIT inexistente")


def test_flujo_cortado_por_un_killer(reglas_pyme):
    consultor = armar_consultor(BASE_PYME, propios={"lista_negra": True})

    e = estados(flujo(evaluar(solicitud_pyme(), reglas_pyme, consultor), reglas_pyme))

    assert e["killer"] == ("corta", "rechaza: lista_negra")
    assert e["scorecard"][0] == "no-alcanzada"
    assert e["veredicto"] == ("corta", "RECHAZADO")


def test_flujo_con_fuente_caida(reglas_pyme):
    consultor = armar_consultor(BASE_PYME, cendeu=NoDisponible("HTTP 503"))

    e = estados(flujo(evaluar(solicitud_pyme(), reglas_pyme, consultor), reglas_pyme))

    assert e["fuente"][0] == "alerta" and "con problemas" in e["fuente"][1]
    assert e["killer"][0] == "alerta" and "no evaluables" in e["killer"][1]
    assert e["scorecard"][0] == "alerta" and "sin dato" in e["scorecard"][1]
