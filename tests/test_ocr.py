"""OCR com respostas reais do Gemini gravadas em `tests/gravacoes/ocr/` (sem rede no CI).

Para regravar: apague a pasta e rode `python -m recebimento avaliar-ocr --gravar`
com OPENROUTER_API_KEY definida.
"""

import json
from pathlib import Path
from typing import Any

from recebimento.avaliacao import avaliar_ocr
from recebimento.danfe import gerar_imagem
from recebimento.dominio import NotaFiscal
from recebimento.erp import Erp, carregar_erp_json, criar_engine, sessao
from recebimento.gerador import gerar_cenario, salvar_cenario
from recebimento.ia import Gravado
from recebimento.ocr import PROMPT_VERSAO, ler_danfe, reconstruir_chave
from recebimento.servico import SefazEmMemoria, codigos, receber_imagem

GRAVACOES = Path(__file__).parent / "gravacoes" / "ocr"


class Roteiro:
    """Provedor falso que devolve respostas prontas, em ordem."""

    def __init__(self, *respostas: str) -> None:
        self.respostas = list(respostas)
        self.chamadas = 0

    def completar(self, mensagens: list[Any], **_: Any) -> dict[str, Any]:
        self.chamadas += 1
        return {"role": "assistant", "content": self.respostas.pop(0)}


def _json_de(nota: NotaFiscal, chave: str | None = None) -> str:
    """O JSON que a IA devolveria para esta nota (opcionalmente com a chave lida errada)."""
    c = chave or nota.chave
    itens = []
    for i in nota.itens:
        dados = {k: str(v) for k, v in i.model_dump().items() if k != "numero"}
        dados["aliquota_icms_pct"] = str(i.aliquota_icms * 100)
        dados["aliquota_ipi_pct"] = str(i.aliquota_ipi * 100)
        itens.append({**dados, "item_pedido": i.item_pedido})
    return json.dumps(
        {
            "chave_grupos": [c[i : i + 4] for i in range(0, len(c), 4)],
            "numero": nota.numero,
            "serie": nota.serie,
            "data_emissao": nota.data_emissao.isoformat(),
            "emitente_cnpj": nota.emitente_cnpj,
            "emitente_nome": nota.emitente_nome,
            "emitente_uf": nota.emitente_uf,
            "destinatario_cnpj": nota.destinatario_cnpj,
            "itens": itens,
            "valor_produtos": str(nota.totais.valor_produtos),
            "valor_icms": str(nota.totais.valor_icms),
            "valor_ipi": str(nota.totais.valor_ipi),
            "valor_nota": str(nota.totais.valor_nota),
        }
    )


def _nota_ok() -> NotaFiscal:
    cen = gerar_cenario(seed=42)
    return dict(cen.notas)[next(a for a, g in cen.gabarito.items() if g["tipo"] == "ok")]


def test_eval_ocr_com_respostas_reais_gravadas() -> None:
    r = avaliar_ocr(Gravado(GRAVACOES), seed=42, foto=True)
    assert r.notas == 22
    assert r.fiscal_correto == 22, r.exemplos
    assert r.erradas_aceitas == 0
    assert r.conferidas == 20  # as 2 notas com chave inválida de propósito não passam


def test_reconstroi_chave_com_digitos_duplicados() -> None:
    nota = _nota_ok()
    lida = nota.chave[:10] + nota.chave[8:]  # a IA repetiu dois dígitos (46 no total)
    assert reconstruir_chave(nota, lida) == nota.chave


def test_nao_reconstroi_se_o_final_lido_estiver_errado() -> None:
    nota = _nota_ok()
    lida = nota.chave[:-3] + str((int(nota.chave[-3]) + 1) % 10) + nota.chave[-2:]
    assert reconstruir_chave(nota, lida) is None


def test_leitura_com_chave_reconstruida_registra_o_ajuste() -> None:
    nota = _nota_ok()
    ruim = nota.chave[:10] + nota.chave[8:]
    leitura = ler_danfe(b"img", "image/png", Roteiro(_json_de(nota, ruim)))
    assert leitura.conferida and leitura.nota is not None
    assert leitura.nota.chave == nota.chave
    assert leitura.ajustes and "reconstruída" in leitura.ajustes[0]


def test_resposta_fora_do_formato_ganha_uma_correcao() -> None:
    ia = Roteiro("isso não é json", _json_de(_nota_ok()))
    leitura = ler_danfe(b"img", "image/png", ia)
    assert ia.chamadas == 2 and leitura.conferida


def test_duas_respostas_fora_do_formato_viram_problema() -> None:
    leitura = ler_danfe(b"img", "image/png", Roteiro("{}", "nada"))
    assert leitura.nota is None and leitura.problemas[0].codigo == "formato"


def test_valor_lido_errado_e_pego_pela_conferencia() -> None:
    nota = _nota_ok()
    dados = json.loads(_json_de(nota))
    dados["itens"][0]["valor_total"] = str(nota.itens[0].valor_total + 100)
    leitura = ler_danfe(b"img", "image/png", Roteiro(json.dumps(dados)))
    assert not leitura.conferida
    assert {"item_total", "total_produtos"} <= {p.codigo for p in leitura.problemas}


def test_imagem_entra_pelo_mesmo_fluxo_do_xml(tmp_path: Path) -> None:
    cen = gerar_cenario(seed=42)
    salvar_cenario(cen, tmp_path)
    engine = criar_engine()
    carregar_erp_json(engine, tmp_path / "erp.json")
    sefaz = SefazEmMemoria.de_arquivo(tmp_path / "sefaz.json")
    ia = Gravado(GRAVACOES)
    with sessao(engine) as s:
        erp = Erp(s)
        for arquivo, nota in cen.notas:
            if arquivo.endswith("-reenvio.xml"):
                continue
            imagem, mime = gerar_imagem(nota, foto=True)
            id_chamada = f"seed42-{arquivo[:-4]}-foto-p{PROMPT_VERSAO}"
            row = receber_imagem(erp, sefaz, ia, imagem, mime, arquivo, id_chamada=id_chamada)
            esperado = cen.gabarito[arquivo]
            assert row.origem == "ocr"
            if esperado["tipo"] == "chave_invalida":
                assert codigos(row) == ["leitura_ocr"], arquivo
            else:
                obtido = (row.status, codigos(row))
                assert obtido == (esperado["status"], esperado["divergencias"]), arquivo


def test_gravacoes_sao_respostas_do_modelo() -> None:
    arquivos = sorted(GRAVACOES.glob("*.json"))
    assert len(arquivos) == 22
    for arquivo in arquivos:
        gravada = json.loads(arquivo.read_text(encoding="utf-8"))
        assert gravada["role"] == "assistant" and gravada["content"]
