"""Camada de IA: uma interface, duas implementações.

- `OpenRouter`: chamada real (API compatível com OpenAI; modelo padrão Gemini 2.5 Flash, que
  lê imagem e chama ferramentas). Chave só na variável de ambiente `OPENROUTER_API_KEY`.
- `Gravado`: reproduz respostas reais salvas em disco. Testes e CI rodam sem rede e sem custo;
  com `modo="gravar"`, chama o provedor real e salva a resposta para a próxima vez.

Trocar de provedor não muda nada no resto do sistema.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

Mensagem = dict[str, Any]

MODELO_PADRAO = "google/gemini-2.5-flash"


class ErroIA(RuntimeError):
    pass


class ProvedorIA(Protocol):
    def completar(
        self,
        mensagens: list[Mensagem],
        *,
        ferramentas: list[dict[str, Any]] | None = None,
        modo_json: bool = False,
        id_chamada: str | None = None,
    ) -> Mensagem:
        """Devolve a mensagem do assistente (`content` e, se houver, `tool_calls`)."""
        ...


class OpenRouter:
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(
        self,
        modelo: str = MODELO_PADRAO,
        chave: str | None = None,
        http: httpx.Client | None = None,
        tentativas: int = 3,
    ) -> None:
        self.modelo = modelo
        self.chave = chave or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.chave:
            raise ErroIA("defina OPENROUTER_API_KEY (variável de ambiente)")
        self.http = http or httpx.Client(timeout=120)
        self.tentativas = tentativas

    def completar(
        self,
        mensagens: list[Mensagem],
        *,
        ferramentas: list[dict[str, Any]] | None = None,
        modo_json: bool = False,
        id_chamada: str | None = None,
    ) -> Mensagem:
        corpo: dict[str, Any] = {"model": self.modelo, "messages": mensagens, "temperature": 0}
        if ferramentas:
            corpo["tools"] = ferramentas
        if modo_json:
            corpo["response_format"] = {"type": "json_object"}
        espera = 2.0
        for tentativa in range(1, self.tentativas + 1):
            r = self.http.post(
                self.URL,
                json=corpo,
                headers={
                    "Authorization": f"Bearer {self.chave}",
                    "X-Title": "recebimento-fiscal",
                },
            )
            if r.status_code in (429, 500, 502, 503) and tentativa < self.tentativas:
                time.sleep(espera)  # limite de uso ou instabilidade: espera e tenta de novo
                espera *= 2
                continue
            if r.status_code != 200:
                raise ErroIA(f"OpenRouter HTTP {r.status_code}: {r.text[:300]}")
            dados = r.json()
            if "error" in dados:
                raise ErroIA(f"OpenRouter: {dados['error']}")
            mensagem: Mensagem = dados["choices"][0]["message"]
            return mensagem
        raise ErroIA("OpenRouter: tentativas esgotadas")


class Gravado:
    """Reproduz respostas salvas; em `modo="gravar"` chama `real` e salva."""

    def __init__(
        self,
        pasta: Path,
        real: ProvedorIA | None = None,
        modo: Literal["reproduzir", "gravar"] = "reproduzir",
    ) -> None:
        self.pasta = pasta
        self.real = real
        self.modo = modo

    @staticmethod
    def _id(mensagens: list[Mensagem], ferramentas: list[dict[str, Any]] | None) -> str:
        texto = json.dumps([mensagens, ferramentas], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(texto.encode()).hexdigest()[:16]

    def completar(
        self,
        mensagens: list[Mensagem],
        *,
        ferramentas: list[dict[str, Any]] | None = None,
        modo_json: bool = False,
        id_chamada: str | None = None,
    ) -> Mensagem:
        # id explícito quando a mensagem tem imagem: os bytes podem variar entre sistemas
        nome = id_chamada or self._id(mensagens, ferramentas)
        arquivo = self.pasta / f"{nome}.json"
        if arquivo.exists():
            gravada: Mensagem = _json_load(arquivo)
            return gravada
        if self.modo != "gravar" or self.real is None:
            raise ErroIA(f"sem gravação para {nome!r}; rode com modo='gravar' e a chave definida")
        resposta = self.real.completar(
            mensagens, ferramentas=ferramentas, modo_json=modo_json, id_chamada=id_chamada
        )
        self.pasta.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(_json_dump(resposta), encoding="utf-8", newline="\n")
        return resposta


def _json_load(arquivo: Path) -> Any:
    return json.loads(arquivo.read_text(encoding="utf-8"))


def _json_dump(valor: Any) -> str:
    return json.dumps(valor, ensure_ascii=False, indent=2) + "\n"
