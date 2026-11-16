"""Seam ``ClienteClaude``: cómo los agentes le hablan a Claude.

Dos adapters de producción (ADR 0004):

- ``ClaudeCLI`` (por defecto): ``claude -p`` con la suscripción del usuario. Sin API key.
- ``ClaudeAPI``: la API de Anthropic con el SDK oficial (``pip install -e '.[api]'``)
  y ``ANTHROPIC_API_KEY`` en el entorno o en ``.env``.

Cualquier falla (sin agente configurado, CLI ausente, timeout, error de la API) es
``AgenteNoDisponible``: la app la muestra y el veredicto no se ve afectado.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

RAIZ = Path(__file__).resolve().parents[2]
# Todos los agentes usan Haiku por costo, por CLI y por API. ``RELEVO_MODELO`` lo cambia.
MODELO = "claude-haiku-5-5"
MODELO_API = MODELO  # nombre anterior
# Haiku no tiene fallback del lado del servidor ante una negativa: se manda solo a los que sí.
SIN_FALLBACK_DE_SERVIDOR = ("claude-haiku-",)
TIPOS_IMAGEN = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
TIPOS_TEXTO = {".txt", ".md", ".csv", ".json"}


class AgenteNoDisponible(RuntimeError):
    """El agente no está configurado o no pudo responder."""


class ClienteClaude(Protocol):
    def completar(self, prompt: str, archivos: Sequence[Path] = (), sistema: str | None = None) -> str: ...


class SinAgente:
    """Agente no configurado (``RELEVO_AGENTE=ninguno``)."""

    def completar(self, prompt: str, archivos: Sequence[Path] = (), sistema: str | None = None) -> str:
        raise AgenteNoDisponible("no hay agente configurado (RELEVO_AGENTE=ninguno)")


class ClaudeCLI:
    """``claude -p`` en modo no interactivo.

    Corre en un directorio neutro, en ``--safe-mode`` (sin CLAUDE.md, hooks ni MCP del
    usuario) y sin guardar la sesión. Solo habilita la herramienta Read, y solo cuando
    hay archivos que leer. Quita ``ANTHROPIC_API_KEY`` del entorno para que use la
    suscripción y no la key de la API."""

    def __init__(
        self,
        ejecutable: str = "claude",
        modelo: str | None = MODELO,
        timeout: float = 300,
        ejecutar: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.ejecutable = ejecutable
        self.modelo = modelo
        self.timeout = timeout
        self.ejecutar = ejecutar

    def completar(self, prompt: str, archivos: Sequence[Path] = (), sistema: str | None = None) -> str:
        archivos = [Path(a).resolve() for a in archivos]
        comando = [self.ejecutable, "-p", "--output-format", "json", "--no-session-persistence", "--safe-mode"]
        if self.modelo:
            comando += ["--model", self.modelo]
        if sistema:
            comando += ["--system-prompt", sistema]
        if archivos:
            directorios = sorted({str(a.parent) for a in archivos})
            comando += ["--tools", "Read", "--allowedTools", "Read", "--add-dir", *directorios]
            prompt += "\n\nArchivos (leelos con la herramienta Read):\n" + "\n".join(f"- {a}" for a in archivos)
        else:
            comando += ["--tools", ""]
        entorno = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        directorio = str(archivos[0].parent) if archivos else tempfile.gettempdir()
        try:
            proceso = self.ejecutar(
                comando,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=directorio,
                env=entorno,
            )
        except FileNotFoundError:
            raise AgenteNoDisponible(f"no se encontró el ejecutable {self.ejecutable!r} de Claude Code") from None
        except subprocess.TimeoutExpired:
            raise AgenteNoDisponible(f"Claude Code no respondió en {self.timeout:.0f} s") from None
        try:
            salida = json.loads(proceso.stdout)
        except (json.JSONDecodeError, TypeError):
            detalle = (proceso.stderr or proceso.stdout or "").strip()[:300]
            raise AgenteNoDisponible(f"Claude Code falló (código {proceso.returncode}): {detalle}") from None
        if proceso.returncode != 0 or salida.get("is_error") or not isinstance(salida.get("result"), str):
            mensaje = str(salida.get("result", ""))[:300]
            if "authenticate" in mensaje.lower() or "log in" in mensaje.lower():
                mensaje += " (iniciá sesión de nuevo: correr `claude` en una terminal y usar /login)"
            raise AgenteNoDisponible(f"Claude Code devolvió un error: {mensaje}")
        return salida["result"]


class ClaudeAPI:
    """API de Anthropic con el SDK oficial.

    Con los modelos que lo admiten usa ``fallbacks: "default"``: si el modelo declina una
    solicitud por seguridad, la API la reintenta con otro modelo dentro de la misma llamada.
    Haiku no lo admite: una negativa es ``AgenteNoDisponible``."""

    def __init__(self, modelo: str = MODELO, esfuerzo: str = "medium", cliente: Any = None):
        self.modelo = modelo
        self.esfuerzo = esfuerzo
        self._cliente = cliente

    def _sdk(self) -> Any:
        if self._cliente is None:
            try:
                import anthropic
            except ImportError:
                raise AgenteNoDisponible("falta el SDK de Anthropic: pip install -e '.[api]'") from None
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise AgenteNoDisponible("falta ANTHROPIC_API_KEY (en el entorno o en .env)")
            self._cliente = anthropic.Anthropic()
        return self._cliente

    def completar(self, prompt: str, archivos: Sequence[Path] = (), sistema: str | None = None) -> str:
        cliente = self._sdk()
        contenido = [_bloque_archivo(Path(a)) for a in archivos] + [{"type": "text", "text": prompt}]
        parametros: dict[str, Any] = {
            "model": self.modelo,
            "max_tokens": 16000,
            "output_config": {"effort": self.esfuerzo},
            "messages": [{"role": "user", "content": contenido}],
        }
        if not self.modelo.startswith(SIN_FALLBACK_DE_SERVIDOR):
            parametros |= {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        if sistema:
            parametros["system"] = sistema
        try:
            respuesta = cliente.beta.messages.create(**parametros)
        except Exception as error:  # el SDK tiene su jerarquía, pero para la app toda falla es lo mismo
            raise AgenteNoDisponible(f"la API de Anthropic falló: {error}") from None
        if respuesta.stop_reason == "refusal":
            raise AgenteNoDisponible("el modelo declinó responder")
        texto = "".join(b.text for b in respuesta.content if getattr(b, "type", None) == "text")
        if not texto.strip():
            raise AgenteNoDisponible(f"respuesta vacía (stop_reason={respuesta.stop_reason})")
        return texto


def _bloque_archivo(archivo: Path) -> dict[str, Any]:
    sufijo = archivo.suffix.lower()
    if sufijo == ".pdf":
        datos = base64.standard_b64encode(archivo.read_bytes()).decode()
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": datos}}
    if sufijo in TIPOS_IMAGEN:
        datos = base64.standard_b64encode(archivo.read_bytes()).decode()
        return {"type": "image", "source": {"type": "base64", "media_type": TIPOS_IMAGEN[sufijo], "data": datos}}
    if sufijo in TIPOS_TEXTO:
        return {
            "type": "text",
            "text": f"Archivo {archivo.name}:\n\n{archivo.read_text(encoding='utf-8', errors='replace')}",
        }
    raise AgenteNoDisponible(f"tipo de archivo no soportado: {archivo.name}")


def cargar_env(archivo: Path = RAIZ / ".env") -> None:
    """Carga ``CLAVE=valor`` de ``.env`` sin pisar lo que ya está en el entorno."""
    if not archivo.is_file():
        return
    for linea in archivo.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip().strip("'\""))


def cliente_desde_entorno() -> ClienteClaude:
    """``RELEVO_AGENTE``: ``cli`` (por defecto), ``api`` o ``ninguno``. El modelo es Haiku en los dos
    (``MODELO``); ``RELEVO_MODELO`` lo cambia."""
    cargar_env()
    tipo = os.environ.get("RELEVO_AGENTE", "cli").strip().lower()
    modelo = os.environ.get("RELEVO_MODELO") or MODELO
    if tipo == "ninguno":
        return SinAgente()
    if tipo == "api":
        return ClaudeAPI(modelo=modelo)
    if tipo == "cli":
        return ClaudeCLI(ejecutable=shutil.which("claude") or "claude", modelo=modelo)
    raise ValueError(f"RELEVO_AGENTE desconocido: {tipo!r} (cli | api | ninguno)")
