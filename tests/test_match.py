from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from recebimento import avaliacao, match
from recebimento.dominio import (
    ItemRecebido,
    NotaFiscal,
    PedidoCompra,
    Recebimento,
    SituacaoSefaz,
)
from recebimento.gerador import COMPRADORA_CNPJ, gerar_cenario
from recebimento.match import ContextoErp, Status, Tolerancias, avaliar_nota


def _base() -> tuple[NotaFiscal, ContextoErp]:
    """Uma nota 'ok' do gerador com o contexto completo do ERP."""
    cen = gerar_cenario(seed=9)
    arquivo = next(a for a, g in cen.gabarito.items() if g["tipo"] == "ok")
    nota = dict(cen.notas)[arquivo]
    numero = nota.itens[0].pedido
    pedido = next(p for p in cen.pedidos if p.numero == numero)
    recs = tuple(r for r in cen.recebimentos if r.pedido == numero)
    ctx = ContextoErp(
        compradora_cnpj=COMPRADORA_CNPJ,
        pedidos={pedido.numero: pedido},
        recebimentos={pedido.numero: recs},
    )
    return nota, ctx


def _com_item(nota: NotaFiscal, **mudancas: object) -> NotaFiscal:
    """Altera o 1º item e recalcula totais e impostos, como uma nota real coerente."""
    c = Decimal("0.01")
    item = nota.itens[0].model_copy(update=mudancas)
    total = (item.quantidade * item.valor_unitario).quantize(c)
    item = item.model_copy(
        update={
            "valor_total": total,
            "base_icms": total,
            "valor_icms": (total * item.aliquota_icms).quantize(c),
            "valor_ipi": (total * item.aliquota_ipi).quantize(c),
        }
    )
    itens = (item, *nota.itens[1:])
    produtos = sum((i.valor_total for i in itens), Decimal(0))
    ipi = sum((i.valor_ipi for i in itens), Decimal(0))
    totais = nota.totais.model_copy(
        update={
            "valor_produtos": produtos,
            "valor_icms": sum((i.valor_icms for i in itens), Decimal(0)),
            "valor_ipi": ipi,
            "valor_nota": produtos + ipi,
        }
    )
    return nota.model_copy(update={"itens": itens, "totais": totais})


def test_nota_ok_liberada() -> None:
    nota, ctx = _base()
    r = avaliar_nota(nota, ctx)
    assert r.status == Status.LIBERADA and r.divergencias == ()


@pytest.mark.parametrize(
    ("fator", "status"),
    [("1.02", Status.LIBERADA), ("1.0201", Status.BLOQUEADA), ("0.90", Status.LIBERADA)],
)
def test_fronteira_da_tolerancia_de_preco(fator: str, status: Status) -> None:
    # exatamente 2% acima passa; 2,01% bloqueia; preço menor que o pedido é favorável
    nota, ctx = _base()
    item = nota.itens[0]
    preco = ctx.pedidos[str(item.pedido)].item(int(item.item_pedido or 0))
    assert preco is not None
    novo = preco.preco_unitario * Decimal(fator)
    r = avaliar_nota(_com_item(nota, valor_unitario=novo), ctx)
    assert r.status == status, r.codigos
    assert ("preco" in r.codigos) == (status == Status.BLOQUEADA)


def test_impacto_da_divergencia_de_preco_em_reais() -> None:
    nota, ctx = _base()
    item = nota.itens[0]
    ip = ctx.pedidos[str(item.pedido)].item(int(item.item_pedido or 0))
    assert ip is not None
    nota = _com_item(nota, valor_unitario=ip.preco_unitario + Decimal("10"))
    div = next(d for d in avaliar_nota(nota, ctx).divergencias if d.codigo == "preco")
    assert div.impacto_reais == Decimal("10") * item.quantidade


def test_recebimento_parcial_em_duas_entradas_soma_as_quantidades() -> None:
    nota, ctx = _base()
    item = nota.itens[0]
    numero = str(item.pedido)
    pedido: PedidoCompra = ctx.pedidos[numero]
    metade = item.quantidade / 2
    recs = tuple(
        Recebimento(
            numero=f"R{n}",
            pedido=numero,
            data=date(2026, 9, 20),
            itens=tuple(
                ItemRecebido(
                    item_pedido=ip.item,
                    quantidade=metade if ip.item == item.item_pedido else ip.quantidade / 2,
                )
                for ip in pedido.itens
            ),
        )
        for n in (1, 2)
    )
    assert avaliar_nota(nota, replace(ctx, recebimentos={numero: recs})).status == Status.LIBERADA


def test_tolerancia_de_quantidade_configuravel() -> None:
    nota, ctx = _base()
    nota = _com_item(nota, quantidade=nota.itens[0].quantidade + 1)
    assert "quantidade" in avaliar_nota(nota, ctx).codigos
    folga = replace(ctx, tolerancias=Tolerancias(quantidade_unidades=Decimal("1")))
    assert "quantidade" not in avaliar_nota(nota, folga).codigos


@pytest.mark.parametrize(
    ("situacao", "codigo"),
    [
        (SituacaoSefaz.CANCELADA, "cancelada_sefaz"),
        (SituacaoSefaz.DENEGADA, "denegada_sefaz"),
        (SituacaoSefaz.NAO_ENCONTRADA, "nao_encontrada_sefaz"),
    ],
)
def test_situacao_sefaz_rejeita(situacao: SituacaoSefaz, codigo: str) -> None:
    nota, ctx = _base()
    r = avaliar_nota(nota, replace(ctx, situacao_sefaz=situacao))
    assert r.status == Status.REJEITADA and r.codigos == [codigo]


def test_nota_para_outro_destinatario_e_rejeitada() -> None:
    nota, ctx = _base()
    r = avaliar_nota(nota, replace(ctx, compradora_cnpj="11222333000181"))
    assert r.codigos == ["destinatario"]


def test_eval_tem_dentes_motor_sabotado_e_detectado(monkeypatch: pytest.MonkeyPatch) -> None:
    """Se o motor ignorar divergência de preço, o eval precisa falhar."""
    original = match.avaliar_nota

    def sem_preco(nota: NotaFiscal, ctx: ContextoErp) -> match.Resultado:
        frouxo = replace(ctx, tolerancias=Tolerancias(preco_percentual=Decimal("0.50")))
        return original(nota, frouxo)

    monkeypatch.setattr("recebimento.servico.avaliar_nota", sem_preco)
    relatorio = avaliacao.avaliar(cenarios=2)
    assert not relatorio.perfeito
    assert relatorio.erros_por_tipo["preco_acima"] > 0


def test_eval_completo_sem_erros() -> None:
    relatorio = avaliacao.avaliar(cenarios=5)
    assert relatorio.perfeito, relatorio.exemplos_erro
    assert relatorio.deteccao == 1.0 and relatorio.taxa_falso_bloqueio == 0.0
