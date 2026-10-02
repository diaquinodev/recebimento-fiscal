"""Tipos do domínio de compras (Procure-to-Pay).

Vocabulário de ERP corporativo, para quem vem do SAP:
- PedidoCompra  ≈ pedido de compra (transação ME21N)
- Recebimento   ≈ entrada de mercadoria no armazém (MIGO)
- NotaFiscal    ≈ NF-e do fornecedor, conferida na verificação de fatura (MIRO)

Dinheiro e quantidade são `Decimal`: `float` erra centavos.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Modelo(BaseModel):
    model_config = ConfigDict(frozen=True)


class Fornecedor(Modelo):
    cnpj: str = Field(pattern=r"^\d{14}$")
    razao_social: str
    uf: str = Field(pattern=r"^[A-Z]{2}$")
    email: str


class ItemPedido(Modelo):
    item: int = Field(ge=1)
    codigo: str
    descricao: str
    ncm: str = Field(pattern=r"^\d{8}$")
    quantidade: Decimal = Field(gt=0)
    preco_unitario: Decimal = Field(gt=0)
    aliquota_icms: Decimal = Field(ge=0, le=1)
    aliquota_ipi: Decimal = Field(ge=0, le=1)


class PedidoCompra(Modelo):
    numero: str
    fornecedor_cnpj: str
    data: date
    itens: tuple[ItemPedido, ...]

    def item(self, numero: int) -> ItemPedido | None:
        return next((i for i in self.itens if i.item == numero), None)


class ItemRecebido(Modelo):
    item_pedido: int
    quantidade: Decimal = Field(ge=0)


class Recebimento(Modelo):
    numero: str
    pedido: str
    data: date
    itens: tuple[ItemRecebido, ...]

    def quantidade(self, item_pedido: int) -> Decimal:
        return sum((i.quantidade for i in self.itens if i.item_pedido == item_pedido), Decimal(0))


class ItemNota(Modelo):
    numero: int = Field(ge=1)
    codigo: str
    descricao: str
    ncm: str
    cfop: str
    quantidade: Decimal
    valor_unitario: Decimal
    valor_total: Decimal
    base_icms: Decimal
    aliquota_icms: Decimal
    valor_icms: Decimal
    aliquota_ipi: Decimal
    valor_ipi: Decimal
    pedido: str | None = None  # xPed: número do pedido de compra do cliente
    item_pedido: int | None = None  # nItemPed: item desse pedido


class Totais(Modelo):
    valor_produtos: Decimal
    valor_icms: Decimal
    valor_ipi: Decimal
    valor_nota: Decimal


class NotaFiscal(Modelo):
    chave: str
    numero: int
    serie: int
    data_emissao: date
    emitente_cnpj: str
    emitente_nome: str
    emitente_uf: str
    destinatario_cnpj: str
    itens: tuple[ItemNota, ...]
    totais: Totais


class SituacaoSefaz(StrEnum):
    AUTORIZADA = "autorizada"
    CANCELADA = "cancelada"
    DENEGADA = "denegada"
    NAO_ENCONTRADA = "nao_encontrada"
