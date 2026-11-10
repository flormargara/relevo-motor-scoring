"""Adapters de ``ClienteClaude`` sin llamar a Claude: el proceso y el SDK son falsos."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from relevo.clientes_claude import AgenteNoDisponible, ClaudeAPI, ClaudeCLI, SinAgente, cliente_desde_entorno


class Proceso:
    def __init__(self, stdout="", returncode=0, stderr="", excepcion=None):
        self.salida = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.excepcion = excepcion
        self.llamadas = []

    def __call__(self, comando, **opciones):
        self.llamadas.append((comando, opciones))
        if self.excepcion:
            raise self.excepcion
        return self.salida


def ok(texto):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": texto})


def test_cli_sin_archivos_deshabilita_las_herramientas(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    proceso = Proceso(ok("hola"))

    assert ClaudeCLI(ejecutar=proceso).completar("decí hola", sistema="Sos breve.") == "hola"

    comando, opciones = proceso.llamadas[0]
    assert comando[:2] == ["claude", "-p"]
    assert comando[comando.index("--tools") + 1] == ""
    assert comando[comando.index("--system-prompt") + 1] == "Sos breve."
    assert "--safe-mode" in comando
    assert opciones["input"] == "decí hola"
    assert "ANTHROPIC_API_KEY" not in opciones["env"]  # usa la suscripción, no la key


def test_cli_con_archivos_solo_habilita_read_en_su_directorio(tmp_path):
    archivo = tmp_path / "balance.pdf"
    archivo.write_bytes(b"%PDF")
    proceso = Proceso(ok("{}"))

    ClaudeCLI(ejecutar=proceso).completar("extraé", [archivo])

    comando, opciones = proceso.llamadas[0]
    assert comando[comando.index("--tools") + 1] == "Read"
    assert comando[comando.index("--add-dir") + 1] == str(tmp_path.resolve())
    assert str(archivo.resolve()) in opciones["input"]
    assert opciones["cwd"] == str(tmp_path.resolve())


@pytest.mark.parametrize(
    "proceso",
    [
        Proceso(excepcion=FileNotFoundError()),
        Proceso(excepcion=subprocess.TimeoutExpired("claude", 1)),
        Proceso("no es json", returncode=1, stderr="Not logged in"),
        Proceso(json.dumps({"is_error": True, "result": "Credit balance too low"}), returncode=1),
    ],
)
def test_cli_toda_falla_es_agente_no_disponible(proceso):
    with pytest.raises(AgenteNoDisponible):
        ClaudeCLI(ejecutar=proceso).completar("hola")


class SdkFalso:
    def __init__(self, respuesta=None, error=None):
        self.parametros = None
        self.respuesta, self.error = respuesta, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **parametros):
        self.parametros = parametros
        if self.error:
            raise self.error
        return self.respuesta


def mensaje(texto, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=texto)])


def test_api_manda_pdf_imagen_y_texto(tmp_path):
    pdf, png, txt = tmp_path / "a.pdf", tmp_path / "b.png", tmp_path / "c.txt"
    pdf.write_bytes(b"%PDF"), png.write_bytes(b"\x89PNG"), txt.write_text("Ventas: 100")
    sdk = SdkFalso(mensaje("listo"))

    assert ClaudeAPI(cliente=sdk).completar("extraé", [pdf, png, txt], sistema="Sos preciso.") == "listo"

    p = sdk.parametros
    assert p["model"] == "claude-haiku-5-5"
    assert p["system"] == "Sos preciso."
    assert p["fallbacks"] == "default"
    tipos = [b["type"] for b in p["messages"][0]["content"]]
    assert tipos == ["document", "image", "text", "text"]
    assert "Ventas: 100" in p["messages"][0]["content"][2]["text"]


def test_api_declinada_o_con_error_es_agente_no_disponible():
    with pytest.raises(AgenteNoDisponible):
        ClaudeAPI(cliente=SdkFalso(mensaje("", stop_reason="refusal"))).completar("hola")
    with pytest.raises(AgenteNoDisponible):
        ClaudeAPI(cliente=SdkFalso(error=RuntimeError("500"))).completar("hola")


def test_api_sin_key_es_agente_no_disponible(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(AgenteNoDisponible):
        ClaudeAPI().completar("hola")


def test_cliente_desde_entorno(monkeypatch):
    monkeypatch.setenv("RELEVO_AGENTE", "ninguno")
    assert isinstance(cliente_desde_entorno(), SinAgente)
    monkeypatch.setenv("RELEVO_AGENTE", "api")
    assert isinstance(cliente_desde_entorno(), ClaudeAPI)
    monkeypatch.delenv("RELEVO_AGENTE")
    assert isinstance(cliente_desde_entorno(), ClaudeCLI)
    with pytest.raises(AgenteNoDisponible):
        SinAgente().completar("hola")


def test_cli_con_sesion_vencida_sugiere_iniciar_sesion():
    salida = json.dumps({"is_error": True, "result": "Failed to authenticate: OAuth session expired"})

    with pytest.raises(AgenteNoDisponible, match="/login"):
        ClaudeCLI(ejecutar=Proceso(salida)).completar("hola")
