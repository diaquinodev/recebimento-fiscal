"""Motor de 3-way match: NF-e × pedido de compra × recebimento.

Função pura: recebe a nota e o que o ERP sabe sobre ela, devolve a decisão. Não acessa banco
nem rede, por isso é testado com milhares de casos e é a mesma lógica com SQLite ou Oracle.

Decisão (como no bloqueio de pagamento de um ERP):
- rejeitada  → a nota não pode entrar (chave inválida, duplicada, cancelada no SEFAZ...).
- bloqueada  → entra, mas o pagamento fica retido até alguém tratar a divergência.
- liberada   → tudo dentro da tolerância; segue para pagamento.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from recebimento.dominio import NotaFiscal, PedidoCompra, Recebimento, SituacaoSefaz
from recebimento.nfe_xml import conferir_nota


class Status(StrEnum):
    LIBERADA = "liberada"
    BLOQUEADA = "bloqueada"
    REJEITADA = "rejeitada"


#: Códigos que impedem a entrada da nota.
REJEICOES = {
    "xml_invalido",
    "chave_invalida",
    "estrutura",
    "duplicada",
    "cancelada_sefaz",
    "denegada_sefaz",
    "nao_encontrada_sefaz",
    "destinatario",
    "leitura_ocr",  # imagem lida sem passar na conferência: revisão humana ou aguardar XML
}


CODIGO_SEFAZ = {
    SituacaoSefaz.CANCELADA: "cancelada_sefaz",
    SituacaoSefaz.DENEGADA: "denegada_sefaz",
    SituacaoSefaz.NAO_ENCONTRADA: "nao_encontrada_sefaz",
}


@dataclass(frozen=True)
class Tolerancias:
    preco_percentual: Decimal = Decimal("0.02")
    quantidade_unidades: Decimal = Decimal("0")
    imposto_reais: Decimal = Decimal("0.05")


@dataclass(frozen=True)
class Divergencia:
    codigo: str
    mensagem: str
    item_nota: int | None = None
    esperado: Decimal | None = None
    encontrado: Decimal | None = None
    impacto_reais: Decimal = Decimal(0)


@dataclass(frozen=True)
class ContextoErp:
    """O que o ERP sabe sobre a nota no momento da entrada."""

    compradora_cnpj: str
    pedidos: Mapping[str, PedidoCompra]
    recebimentos: Mapping[str, tuple[Recebimento, ...]]
    tolerancias: Tolerancias = field(default_factory=Tolerancias)
    chave_ja_lancada: bool = False
    #: None = não consultada (ex.: chave com dígito inválido nem chega ao SEFAZ)
    situacao_sefaz: SituacaoSefaz | None = SituacaoSefaz.AUTORIZADA


@dataclass(frozen=True)
class Resultado:
    status: Status
    divergencias: tuple[Divergencia, ...]

    @property
    def codigos(self) -> list[str]:
        return sorted({d.codigo for d in self.divergencias})

    @property
    def impacto_reais(self) -> Decimal:
        return sum((d.impacto_reais for d in self.divergencias), Decimal(0))


def _brl(valor: Decimal) -> str:
    texto = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


def _decidir(divergencias: list[Divergencia]) -> Resultado:
    if any(d.codigo in REJEICOES for d in divergencias):
        status = Status.REJEITADA
    elif divergencias:
        status = Status.BLOQUEADA
    else:
        status = Status.LIBERADA
    return Resultado(status, tuple(divergencias))


def avaliar_nota(nota: NotaFiscal, ctx: ContextoErp) -> Resultado:
    div: list[Divergencia] = []

    # 1. A nota sozinha: chave, somas
    for p in conferir_nota(nota):
        codigo = "chave_invalida" if p.codigo == "chave_invalida" else "estrutura"
        div.append(Divergencia(codigo, p.mensagem))
    if ctx.chave_ja_lancada:
        div.append(Divergencia("duplicada", "esta chave já foi lançada"))
    if ctx.situacao_sefaz not in (None, SituacaoSefaz.AUTORIZADA):
        div.append(
            Divergencia(
                CODIGO_SEFAZ[ctx.situacao_sefaz],
                f"situação no SEFAZ: {ctx.situacao_sefaz}",
            )
        )
    if nota.destinatario_cnpj != ctx.compradora_cnpj:
        div.append(Divergencia("destinatario", "nota emitida para outro CNPJ"))
    if any(d.codigo in REJEICOES for d in div):
        return _decidir(div)  # sem sentido conferir itens de nota que não pode entrar

    # 2. Item a item contra pedido e recebimento
    tol = ctx.tolerancias
    fornecedor_conferido: set[str] = set()
    for item in nota.itens:
        pedido = ctx.pedidos.get(item.pedido or "")
        if pedido is None:
            div.append(
                Divergencia(
                    "sem_pedido",
                    f"item {item.numero}: pedido {item.pedido!r} não existe no ERP",
                    item.numero,
                    impacto_reais=item.valor_total,
                )
            )
            continue
        if pedido.numero not in fornecedor_conferido:
            fornecedor_conferido.add(pedido.numero)
            if pedido.fornecedor_cnpj != nota.emitente_cnpj:
                div.append(
                    Divergencia(
                        "fornecedor",
                        f"pedido {pedido.numero} é de outro fornecedor "
                        f"({pedido.fornecedor_cnpj}), nota emitida por {nota.emitente_cnpj}",
                        impacto_reais=nota.totais.valor_nota,
                    )
                )
        ip = pedido.item(item.item_pedido or 0)
        if ip is None:
            div.append(
                Divergencia(
                    "sem_pedido",
                    f"item {item.numero}: item {item.item_pedido} não existe no "
                    f"pedido {pedido.numero}",
                    item.numero,
                    impacto_reais=item.valor_total,
                )
            )
            continue

        recebimentos = ctx.recebimentos.get(pedido.numero, ())
        if not recebimentos:
            if not any(d.codigo == "sem_recebimento" for d in div):
                div.append(
                    Divergencia(
                        "sem_recebimento",
                        f"pedido {pedido.numero} sem entrada de mercadoria registrada",
                        impacto_reais=nota.totais.valor_nota,
                    )
                )
        else:
            recebido = sum((r.quantidade(ip.item) for r in recebimentos), Decimal(0))
            if item.quantidade > recebido + tol.quantidade_unidades:
                excesso = item.quantidade - recebido
                div.append(
                    Divergencia(
                        "quantidade",
                        f"item {item.numero}: faturado {item.quantidade:f}, recebido {recebido:f}",
                        item.numero,
                        esperado=recebido,
                        encontrado=item.quantidade,
                        impacto_reais=(excesso * item.valor_unitario).quantize(Decimal("0.01")),
                    )
                )

        limite = ip.preco_unitario * (1 + tol.preco_percentual)
        if item.valor_unitario > limite:
            div.append(
                Divergencia(
                    "preco",
                    f"item {item.numero}: unitário {_brl(item.valor_unitario)} acima do "
                    f"pedido {_brl(ip.preco_unitario)} + {tol.preco_percentual:.0%}",
                    item.numero,
                    esperado=ip.preco_unitario,
                    encontrado=item.valor_unitario,
                    impacto_reais=(
                        (item.valor_unitario - ip.preco_unitario) * item.quantidade
                    ).quantize(Decimal("0.01")),
                )
            )

        icms_esperado = (item.base_icms * ip.aliquota_icms).quantize(Decimal("0.01"))
        if (
            item.aliquota_icms != ip.aliquota_icms
            or abs(item.valor_icms - icms_esperado) > tol.imposto_reais
        ):
            div.append(
                Divergencia(
                    "icms",
                    f"item {item.numero}: ICMS {item.aliquota_icms:.0%} na nota, "
                    f"{ip.aliquota_icms:.0%} no pedido",
                    item.numero,
                    esperado=icms_esperado,
                    encontrado=item.valor_icms,
                    impacto_reais=item.valor_icms - icms_esperado,
                )
            )
        ipi_esperado = (item.valor_total * ip.aliquota_ipi).quantize(Decimal("0.01"))
        if (
            item.aliquota_ipi != ip.aliquota_ipi
            or abs(item.valor_ipi - ipi_esperado) > tol.imposto_reais
        ):
            div.append(
                Divergencia(
                    "ipi",
                    f"item {item.numero}: IPI {item.aliquota_ipi:.0%} na nota, "
                    f"{ip.aliquota_ipi:.0%} no pedido",
                    item.numero,
                    esperado=ipi_esperado,
                    encontrado=item.valor_ipi,
                    impacto_reais=item.valor_ipi - ipi_esperado,
                )
            )
    return _decidir(div)
