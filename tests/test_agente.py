"""Agente de exceções: travas de negócio em código + respostas reais gravadas.

Gravações em `tests/gravacoes/agente/`. Para regravar: apague a pasta e rode
`python -m recebimento avaliar-agente --gravar` com OPENROUTER_API_KEY definida.
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from recebimento.agente import (
    PERMITIDAS,
    analisar_nota,
    sugere_cce,
    validar_proposta,
)
from recebimento.avaliacao import avaliar_agente
from recebimento.erp import Erp, carregar_erp_json, criar_engine, sessao
from recebimento.gerador import gerar_cenario, salvar_cenario
from recebimento.ia import Gravado
from recebimento.servico import SefazEmMemoria, receber_xml

GRAVACOES = Path(__file__).parent / "gravacoes" / "agente"


@pytest.mark.parametrize(
    ("frase", "sugere"),
    [
        # frases reais do modelo, que motivaram a regra
        ("solicitamos a aprovação do novo preço ou a emissão de uma carta de correção.", True),
        ("A divergência de ICMS não pode ser corrigida por carta de correção.", False),
        ("A carta de correção não é aplicável para correção de quantidade.", False),
        ("afeta o valor total da nota, impossibilitando o uso de carta de correção.", False),
        ("Favor enviar CC-e ajustando a quantidade.", True),
        ("Sem relação com o ACCEPT header.", False),
    ],
)
def test_detector_de_sugestao_de_cce(frase: str, sugere: bool) -> None:
    assert sugere_cce(frase) is sugere


def _proposta(acao: str, corpo: str = "Prezados, favor verificar.") -> dict[str, Any]:
    return {
        "resumo": "x",
        "tratativas": [{"divergencia": "icms", "acao": acao, "justificativa": "y"}],
        "email": {"assunto": "NF", "corpo": corpo},
    }


def test_cce_como_acao_e_recusada_para_icms() -> None:
    erros = validar_proposta(_proposta("carta_correcao"), ["icms"], set())
    assert any("não é aceita" in e for e in erros)


def test_acao_valida_passa() -> None:
    assert validar_proposta(_proposta("solicitar_cancelamento_reemissao"), ["icms"], set()) == []


def test_valor_inventado_no_email_e_recusado() -> None:
    corpo = "A diferença é de R$ 1.234,56."
    erros = validar_proposta(
        _proposta("solicitar_cancelamento_reemissao", corpo), ["icms"], {Decimal("95.00")}
    )
    assert any("1.234,56" in e for e in erros)
    ok = validar_proposta(
        _proposta("solicitar_cancelamento_reemissao", corpo), ["icms"], {Decimal("1234.56")}
    )
    assert ok == []


def test_toda_divergencia_precisa_de_tratativa() -> None:
    erros = validar_proposta(
        _proposta("solicitar_cancelamento_reemissao"), ["icms", "preco"], set()
    )
    assert any("faltou tratativa" in e for e in erros)


def test_cce_nunca_e_permitida_para_divergencia_de_valor() -> None:
    for codigo in ("preco", "quantidade", "icms", "ipi", "fornecedor"):
        assert "carta_correcao" not in PERMITIDAS[codigo]


class Roteiro:
    def __init__(self, *respostas: dict[str, Any]) -> None:
        self.respostas = list(respostas)

    def completar(self, mensagens: list[Any], **_: Any) -> dict[str, Any]:
        return self.respostas.pop(0)


def _chamada(nome: str, args: dict[str, Any], n: int) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"c{n}",
                "type": "function",
                "function": {"name": nome, "arguments": json.dumps(args)},
            }
        ],
    }


def _ambiente(tmp_path: Path) -> tuple[Any, int]:
    cen = gerar_cenario(seed=42)
    salvar_cenario(cen, tmp_path)
    engine = criar_engine()
    carregar_erp_json(engine, tmp_path / "erp.json")
    sefaz = SefazEmMemoria.de_arquivo(tmp_path / "sefaz.json")
    with sessao(engine) as s:
        erp = Erp(s)
        for arquivo, _ in cen.notas:
            receber_xml(erp, sefaz, (tmp_path / "notas" / arquivo).read_bytes(), arquivo)
        nota_icms = next(n.id for n in erp.notas("bloqueada") if "icms" in n.divergencias_json)
    return engine, nota_icms


def test_agente_corrige_depois_da_trava_e_email_vai_para_o_cadastro(tmp_path: Path) -> None:
    engine, nota_id = _ambiente(tmp_path)
    errada = json.dumps(_proposta("carta_correcao"))
    certa = json.dumps(_proposta("solicitar_cancelamento_reemissao"))
    ia = Roteiro(
        _chamada("listar_divergencias", {"nota_id": nota_id}, 1),
        _chamada("historico_fornecedor", {"cnpj": "00000000000000"}, 2),
        {"role": "assistant", "content": errada},
        {"role": "assistant", "content": certa},
    )
    with sessao(engine) as s:
        parecer = analisar_nota(Erp(s), ia, nota_id)
    assert parecer.valido
    assert parecer.correcoes and "não é aceita" in parecer.correcoes[0]
    assert parecer.email_para.endswith(".exemplo")  # do cadastro, não do texto da IA


def test_agente_sem_investigar_nao_conclui(tmp_path: Path) -> None:
    engine, nota_id = _ambiente(tmp_path)
    certa = json.dumps(_proposta("solicitar_cancelamento_reemissao"))
    ia = Roteiro({"role": "assistant", "content": certa}, {"role": "assistant", "content": certa})
    with sessao(engine) as s:
        parecer = analisar_nota(Erp(s), ia, nota_id)
    assert not parecer.valido
    assert any("ferramentas" in c for c in parecer.correcoes)


def test_eval_do_agente_com_respostas_reais_gravadas() -> None:
    r = avaliar_agente(Gravado(GRAVACOES))
    assert r.notas == 12
    assert r.validos == 12, r.correcoes
    assert min(r.ferramentas_por_nota) >= 2
    for chave in r.acoes:
        codigo, acao = chave.split("→")
        assert acao in PERMITIDAS[codigo]
