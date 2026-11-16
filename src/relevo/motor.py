"""El motor de decisión: ``evaluar`` es toda su interface.

inputs → killers → derivaciones → scorecard (PD) → rating → política → veredicto

Invariantes:
- Determinístico: misma solicitud + mismas reglas + mismos snapshots → misma evaluación.
- No lanza excepciones por fallas de fuentes: todo termina en un veredicto y en la traza.
- Nunca pide una fuente si la evaluación ya se decidió (los cortes son inmediatos y las
  fuentes se piden de forma perezosa, recién cuando una regla las necesita).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from relevo import identificacion
from relevo.condiciones import Expresion, SinDato
from relevo.consultor import Consultor, Disponible, NoDisponible, ResultadoFuente, SinAntecedentes
from relevo.dominio import Evaluacion, Oferta, Paso, Solicitud, Veredicto
from relevo.reglas import PD_EXTERNA, Politica, Regla, Reglas, Scorecard


def evaluar(solicitud: Solicitud, reglas: Reglas, consultor: Consultor) -> Evaluacion:
    return _Evaluacion(solicitud, reglas, consultor).correr()


class _NoEvaluable(Exception):
    """Falta una fuente (o una variable) para evaluar una regla."""


class _Corte(Exception):
    """La evaluación ya está decidida: no se evalúa nada más."""

    def __init__(self, veredicto: Veredicto, motivo: str):
        self.veredicto = veredicto
        self.motivo = motivo


_ACCION_A_VEREDICTO = {"derivar": Veredicto.DERIVAR, "rechazar": Veredicto.RECHAZADO}


class _Evaluacion:
    def __init__(self, solicitud: Solicitud, reglas: Reglas, consultor: Consultor):
        self.solicitud = solicitud
        self.reglas = reglas
        self.consultor = consultor
        self.resultados: dict[str, ResultadoFuente] = {}
        self.traza: list[Paso] = []
        self.a_derivar: list[str] = []
        self.puntos: float | None = None
        self.pd: float | None = None
        self.rating: str | None = None
        self.decisor = reglas.asignar(solicitud.cuit)  # el modelo que decide (con split, el que le tocó)
        self.variables_solicitud = {
            "cuit": identificacion.normalizar(solicitud.cuit),
            "cuit_valido": identificacion.es_valido(solicitud.cuit),
            "tipo_persona": identificacion.tipo_persona(solicitud.cuit),
            "segmento": solicitud.segmento,
            "monto": solicitud.monto,
            "plazo_meses": solicitud.plazo_meses,
        }

    # --- pipeline ---------------------------------------------------------

    def correr(self) -> Evaluacion:
        oferta = None
        try:
            self._killers()
            self._derivaciones()
            self._scorecard()
            oferta = self._politica()
        except _Corte as corte:
            if corte.veredicto is Veredicto.RECHAZADO:
                return self._cerrar(Veredicto.RECHAZADO, [corte.motivo], None)
            self.a_derivar.append(corte.motivo)
        veredicto = Veredicto.DERIVAR if self.a_derivar else Veredicto.APROBADO
        return self._cerrar(veredicto, self.a_derivar, oferta)

    def _killers(self) -> None:
        for regla in self.reglas.killers:
            if self._condicion(regla, "killer"):
                raise _Corte(Veredicto.RECHAZADO, f"{regla.id}: {regla.descripcion}")

    def _derivaciones(self) -> None:
        for regla in self.reglas.derivaciones:
            if self._condicion(regla, "derivacion"):
                self.a_derivar.append(f"{regla.id}: {regla.descripcion}")

    def _condicion(self, regla: Regla, etapa: str) -> bool:
        """Evalúa la condición de un killer o una derivación y la deja en la traza.
        Si falta la fuente, aplica `si_no_disponible` de la regla."""
        valores: dict[str, Any] = {}
        try:
            salta = bool(self._evaluar(regla.condicion, valores))
        except SinDato as error:
            self._paso(etapa, regla.id, "sin dato", valores=valores, motivo=str(error))
            return False
        except _NoEvaluable as error:
            self._paso(
                etapa, regla.id, "no evaluable", valores=valores, motivo=str(error), accion=regla.si_no_disponible
            )
            self._no_evaluable(regla.si_no_disponible, f"{regla.id}: no evaluable ({error})")
            return False
        self._paso(etapa, regla.id, "salta" if salta else "no salta", valores=valores)
        return salta

    def _no_evaluable(self, accion: str, motivo: str) -> None:
        if accion in _ACCION_A_VEREDICTO:
            raise _Corte(_ACCION_A_VEREDICTO[accion], motivo)
        self.a_derivar.append(motivo)

    def _scorecard(self) -> None:
        """El modelo que decide da la PD y el rating. Después se calculan los de sombra (y los otros del
        split), aunque el que decide haya cortado la evaluación: sus PD se guardan en la traza."""
        try:
            if self.decisor.externo:
                self._modelo_externo(self.decisor)
            else:
                self._puntos(self.decisor)
        finally:
            self._sombras()

    def _puntos(self, scorecard: Scorecard) -> None:
        total = 0.0
        for caracteristica in scorecard.caracteristicas:
            valores: dict[str, Any] = {}
            valor = None
            try:
                valor = self._evaluar(caracteristica.expresion, valores)
                try:
                    resultado, puntos, motivo = "ok", caracteristica.puntos(valor), None
                except TypeError as error:
                    raise _NoEvaluable(f"tipo de dato inesperado: {valor!r} ({error})") from None
            except SinDato as error:
                resultado, puntos, motivo = "sin dato", caracteristica.sin_dato, str(error)
            except _NoEvaluable as error:
                resultado, puntos, motivo = "no evaluable", caracteristica.sin_dato, str(error)
            self._paso(
                "scorecard",
                caracteristica.nombre,
                resultado,
                valor=valor,
                valores=valores,
                puntos=puntos,
                **({"motivo": motivo} if motivo else {}),
            )
            if resultado == "no evaluable":
                self._no_evaluable(
                    scorecard.si_no_disponible, f"scorecard {caracteristica.nombre}: no evaluable ({motivo})"
                )
            total += puntos

        self.puntos = total
        self.pd = scorecard.pd(total)
        self.rating = scorecard.rating_para(self.pd)
        self._paso(
            "rating",
            "rating",
            self.rating,
            puntos=total,
            pd=self.pd,
            modelo=scorecard.modelo,
            provisional=scorecard.provisional,
            **self._split(),
        )

    def _modelo_externo(self, scorecard: Scorecard) -> None:
        """La PD la da un modelo de ML externo (ADR 0010). Sin PD no hay rating: se deriva o se rechaza."""
        assert scorecard.externo is not None
        valores: dict[str, Any] = {}
        try:
            pd = float(self._evaluar(PD_EXTERNA, valores))
            if not 0 <= pd <= 1:
                raise _NoEvaluable(f"PD fuera de [0, 1]: {pd}")
        except (SinDato, _NoEvaluable) as error:
            resultado = "sin dato" if isinstance(error, SinDato) else "no evaluable"
            self._paso(
                "scorecard", scorecard.externo.id, resultado, motivo=str(error), accion=scorecard.si_no_disponible
            )
            raise _Corte(
                _ACCION_A_VEREDICTO[scorecard.si_no_disponible],
                f"scorecard {scorecard.externo.id}: sin PD del modelo ({error})",
            ) from None
        entrada = self._entrada_modelo(scorecard.externo.id, self._fuente)
        self._paso("scorecard", scorecard.externo.id, "ok", valor=pd, factores=entrada.get("factores") or [])
        self.pd = pd
        self.rating = scorecard.rating_para(pd)
        self._paso(
            "rating",
            "rating",
            self.rating,
            pd=pd,
            modelo=scorecard.modelo,
            externo=scorecard.externo.id,
            provisional=scorecard.provisional,
            **self._split(),
        )

    def _split(self) -> dict[str, Any]:
        """Qué modelo le tocó a la solicitud y cómo se repartió, para la traza."""
        if not self.reglas.split:
            return {}
        pesos = {b.scorecard.id: b.peso for b in self.reglas.split}
        return {"split": {"asignado": self.decisor.id, "pesos": pesos, "semilla": self.reglas.semilla}}

    # --- modelos en sombra (ADR 0010) ---------------------------------------

    def _sombras(self) -> None:
        """La PD de los modelos que no deciden: los de sombra y los otros del split. Nunca cortan la
        evaluación ni piden una fuente paga que no se haya consultado ya: si les falta un dato, queda
        anotado en su paso."""
        otros = [b.scorecard for b in self.reglas.split if b.scorecard is not self.decisor]
        for scorecard in (*otros, *self.reglas.sombra):
            try:
                detalle = self._sombra_externa(scorecard) if scorecard.externo else self._sombra_puntos(scorecard)
                self._paso("sombra", scorecard.id, "ok", **detalle)
            except (SinDato, _NoEvaluable) as error:
                resultado = "sin dato" if isinstance(error, SinDato) else "no evaluable"
                self._paso("sombra", scorecard.id, resultado, motivo=str(error), modelo=scorecard.modelo)

    def _sombra_externa(self, scorecard: Scorecard) -> dict[str, Any]:
        assert scorecard.externo is not None
        entrada = self._entrada_modelo(scorecard.externo.id, self._fuente_sin_costo)
        if entrada["estado"] == "sin_antecedentes":
            raise SinDato(f"{scorecard.externo.id}: sin antecedentes")
        if entrada["estado"] != "disponible":
            raise _NoEvaluable(f"{scorecard.externo.id}: {entrada.get('motivo')}")
        pd = float(entrada["pd"])
        return {
            "pd": pd,
            "rating": scorecard.rating_para(pd),
            "modelo": scorecard.modelo,
            "externo": scorecard.externo.id,
            "factores": entrada.get("factores") or [],
        }

    def _sombra_puntos(self, scorecard: Scorecard) -> dict[str, Any]:
        total, sin_dato = 0.0, []
        for caracteristica in scorecard.caracteristicas:
            try:
                valor = caracteristica.expresion.evaluar(lambda f, v: self._resolver(f, v, sin_costo=True), {})
                total += caracteristica.puntos(valor)
            except (SinDato, _NoEvaluable, TypeError, ValueError):
                total += caracteristica.sin_dato
                sin_dato.append(caracteristica.nombre)
        pd = scorecard.pd(total)
        return {
            "pd": pd,
            "rating": scorecard.rating_para(pd),
            "modelo": scorecard.modelo,
            "puntos": total,
            **({"sin_dato": sin_dato} if sin_dato else {}),
        }

    def _fuente_sin_costo(self, fuente: str) -> ResultadoFuente:
        """Una fuente para un modelo en sombra: si es paga y todavía no se consultó, no se consulta."""
        if fuente not in self.resultados and getattr(self.consultor, "es_paga", lambda f: False)(fuente):
            raise _NoEvaluable(f"{fuente}: es paga y no se consultó (los modelos en sombra no generan costo)")
        return self._fuente(fuente)

    def _entrada_modelo(self, id: str, obtener: Callable[[str], ResultadoFuente]) -> dict[str, Any]:
        """La respuesta de un modelo de ML dentro de la fuente ``modelo_ml``."""
        resultado = obtener("modelo_ml")
        if isinstance(resultado, NoDisponible):
            raise _NoEvaluable(f"modelo_ml: {resultado.motivo}")
        if isinstance(resultado, SinAntecedentes):
            return {"estado": "sin_antecedentes"}
        entrada = (resultado.variables.get("modelos") or {}).get(id)
        if entrada is None:
            raise _NoEvaluable(f"modelo_ml: no se consultó {id}")
        return entrada

    def _politica(self) -> Oferta:
        politica = self.reglas.politica
        rating = self.rating
        assert rating is not None
        if rating in politica.rechazar_ratings:
            self._paso("politica", "rating", "rechaza", rating=rating)
            raise _Corte(Veredicto.RECHAZADO, f"politica: rating {rating}")
        if rating in politica.derivar_ratings:
            self._paso("politica", "rating", "deriva", rating=rating)
            self.a_derivar.append(f"politica: rating {rating}")

        valores: dict[str, Any] = {}
        try:
            tasa_base = float(self._evaluar(politica.tasa_base, valores))
            cuota_maxima = self._cuota_maxima(politica, valores)
        except (SinDato, _NoEvaluable) as error:
            self._paso("politica", "oferta", "no calculable", valores=valores, motivo=str(error))
            accion = "rechazar" if politica.si_no_disponible == "rechazar" else "derivar"
            raise _Corte(_ACCION_A_VEREDICTO[accion], f"politica: no se pudo calcular la oferta ({error})") from None

        tna = tasa_base + politica.spread_por_rating[rating]
        plazo = min(max(self.solicitud.plazo_meses, politica.plazo_minimo), politica.plazo_maximo)
        tope = _monto_para_cuota(cuota_maxima, tna, plazo)
        monto = math.floor(min(self.solicitud.monto, tope) / politica.redondeo_monto) * politica.redondeo_monto
        calculo = {
            "valores": valores,
            "tasa_base": tasa_base,
            "spread": politica.spread_por_rating[rating],
            "tna": tna,
            "plazo": plazo,
            "cuota_maxima": round(cuota_maxima, 2),
            "monto_maximo": round(tope, 2),
        }
        if monto < politica.monto_minimo:
            self._paso("politica", "oferta", "debajo del mínimo", monto=monto, **calculo)
            raise _Corte(
                Veredicto.RECHAZADO,
                f"politica: la oferta máxima ({monto:,.0f}) está por debajo del monto mínimo "
                f"({politica.monto_minimo:,.0f})",
            )
        oferta = Oferta(
            monto=monto, plazo_meses=plazo, tasa_nominal_anual=tna, cuota=_cuota_francesa(monto, tna, plazo)
        )
        self._paso("politica", "oferta", "ofrece", monto=monto, cuota=oferta.cuota, **calculo)
        return oferta

    def _cuota_maxima(self, politica: Politica, valores: dict[str, Any]) -> float:
        capacidad = politica.capacidad_pago
        valor = self._evaluar(capacidad.variable, valores)
        if capacidad.tipo == "porcentaje_ventas":
            return float(valor) / 12 * capacidad.porcentaje_cuota
        tabla = capacidad.ingreso_anual_por_categoria or {}
        if str(valor) not in tabla:
            raise _NoEvaluable(f"categoría de monotributo desconocida: {valor!r}")
        return tabla[str(valor)] / 12 * capacidad.porcentaje_cuota

    # --- fuentes ----------------------------------------------------------

    def _evaluar(self, expresion: Expresion, valores: dict[str, Any]) -> Any:
        """Una fuente que devuelve un tipo inesperado deja la regla no evaluable."""
        try:
            return expresion.evaluar(self._resolver, valores)
        except (TypeError, ValueError) as error:
            raise _NoEvaluable(f"tipo de dato inesperado en {expresion.texto!r}: {error}") from None

    def _resolver(self, fuente: str, variable: str, sin_costo: bool = False) -> Any:
        if fuente == "solicitud":
            return self.variables_solicitud[variable]
        obtener = self._fuente_sin_costo if sin_costo else self._fuente
        if fuente == "modelo_ml":
            return self._variable_modelo(variable, obtener)
        resultado = obtener(fuente)
        if isinstance(resultado, NoDisponible):
            raise _NoEvaluable(f"{fuente}: {resultado.motivo}")
        if variable == "sin_antecedentes":
            return isinstance(resultado, SinAntecedentes)
        if isinstance(resultado, SinAntecedentes):
            raise SinDato(f"{fuente}: sin antecedentes")
        if variable not in resultado.variables:
            raise _NoEvaluable(f"{fuente}: variable ausente {variable!r}")
        valor = resultado.variables[variable]
        if valor is None:
            raise SinDato(f"{fuente}.{variable}: sin dato")
        return valor

    def _variable_modelo(self, variable: str, obtener: Callable[[str], ResultadoFuente]) -> Any:
        """``modelo_ml.<variable>`` es siempre del modelo que decide para esta solicitud."""
        if self.decisor.externo is None:
            raise _NoEvaluable("modelo_ml: el modelo que decide no es de ML")
        entrada = self._entrada_modelo(self.decisor.externo.id, obtener)
        if variable == "sin_antecedentes":
            return entrada["estado"] == "sin_antecedentes"
        if entrada["estado"] == "sin_antecedentes":
            raise SinDato(f"modelo_ml ({self.decisor.externo.id}): sin antecedentes")
        if entrada["estado"] != "disponible":
            raise _NoEvaluable(f"modelo_ml ({self.decisor.externo.id}): {entrada.get('motivo')}")
        if entrada.get(variable) is None:
            raise SinDato(f"modelo_ml.{variable}: sin dato")
        return entrada[variable]

    def _fuente(self, fuente: str) -> ResultadoFuente:
        if fuente not in self.resultados:
            try:
                resultado = self.consultor.variables(fuente)
            except Exception as error:  # noqa: BLE001 — una falla de fuente nunca rompe el motor
                resultado = NoDisponible(f"error inesperado: {type(error).__name__}: {error}")
            self.resultados[fuente] = resultado
            match resultado:
                case Disponible(variables={"desactualizada": True}):
                    self._paso("fuente", fuente, "disponible (desactualizada)")
                case Disponible():
                    self._paso("fuente", fuente, "disponible")
                case SinAntecedentes():
                    self._paso("fuente", fuente, "sin antecedentes")
                case NoDisponible(motivo=motivo):
                    self._paso("fuente", fuente, "no disponible", motivo=motivo)
        return self.resultados[fuente]

    # --- cierre -----------------------------------------------------------

    def _paso(self, etapa: str, regla: str, resultado: str, **detalle: Any) -> None:
        self.traza.append(Paso(etapa, regla, resultado, detalle))

    def _cerrar(self, veredicto: Veredicto, motivos: list[str], oferta: Oferta | None) -> Evaluacion:
        no_consultadas = tuple(f for f in self.reglas.fuentes if f not in self.resultados)
        self._paso(
            "veredicto",
            "veredicto",
            veredicto.value,
            motivos=list(motivos),
            fuentes_no_consultadas=list(no_consultadas),
        )
        return Evaluacion(
            solicitud=self.solicitud,
            version_reglas=self.reglas.id,
            veredicto=veredicto,
            motivos=tuple(motivos),
            traza=tuple(self.traza),
            snapshots=tuple(self.consultor.snapshots()),
            fuentes_no_consultadas=no_consultadas,
            puntos=self.puntos,
            pd=self.pd,
            rating=self.rating,
            oferta=oferta,
        )


def _monto_para_cuota(cuota: float, tna: float, plazo: int) -> float:
    i = tna / 100 / 12
    return cuota * plazo if i == 0 else cuota * (1 - (1 + i) ** -plazo) / i


def _cuota_francesa(monto: float, tna: float, plazo: int) -> float:
    i = tna / 100 / 12
    return round(monto / plazo if i == 0 else monto * i / (1 - (1 + i) ** -plazo), 2)
