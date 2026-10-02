"""Banco do "ERP": pedidos, recebimentos, fornecedores e as notas que já entraram.

SQLAlchemy 2 com tipos portáveis: o mesmo código roda em SQLite (testes e demo) e em Oracle
(basta trocar a URL, ex.: `oracle+oracledb://usuario:senha@host:1521/?service_name=FREEPDB1`).
Valores monetários são `Numeric`, nunca ponto flutuante.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.pool import StaticPool

from recebimento.dominio import (
    Fornecedor,
    ItemPedido,
    ItemRecebido,
    PedidoCompra,
    Recebimento,
)
from recebimento.match import Resultado, Tolerancias

DINHEIRO = Numeric(15, 4)


class Base(DeclarativeBase):
    pass


class FornecedorRow(Base):
    __tablename__ = "fornecedor"
    cnpj: Mapped[str] = mapped_column(String(14), primary_key=True)
    razao_social: Mapped[str] = mapped_column(String(120))
    uf: Mapped[str] = mapped_column(String(2))
    email: Mapped[str] = mapped_column(String(120))


class PedidoRow(Base):
    __tablename__ = "pedido_compra"
    numero: Mapped[str] = mapped_column(String(20), primary_key=True)
    fornecedor_cnpj: Mapped[str] = mapped_column(ForeignKey("fornecedor.cnpj"))
    data: Mapped[date] = mapped_column(Date)
    itens: Mapped[list[ItemPedidoRow]] = relationship(
        order_by="ItemPedidoRow.item", cascade="all, delete-orphan"
    )


class ItemPedidoRow(Base):
    __tablename__ = "item_pedido"
    pedido: Mapped[str] = mapped_column(ForeignKey("pedido_compra.numero"), primary_key=True)
    item: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(30))
    descricao: Mapped[str] = mapped_column(String(200))
    ncm: Mapped[str] = mapped_column(String(8))
    quantidade: Mapped[Decimal] = mapped_column(DINHEIRO)
    preco_unitario: Mapped[Decimal] = mapped_column(DINHEIRO)
    aliquota_icms: Mapped[Decimal] = mapped_column(Numeric(7, 4))
    aliquota_ipi: Mapped[Decimal] = mapped_column(Numeric(7, 4))


class RecebimentoRow(Base):
    __tablename__ = "recebimento"
    numero: Mapped[str] = mapped_column(String(20), primary_key=True)
    pedido: Mapped[str] = mapped_column(ForeignKey("pedido_compra.numero"), index=True)
    data: Mapped[date] = mapped_column(Date)
    itens: Mapped[list[ItemRecebidoRow]] = relationship(cascade="all, delete-orphan")


class ItemRecebidoRow(Base):
    __tablename__ = "item_recebido"
    recebimento: Mapped[str] = mapped_column(ForeignKey("recebimento.numero"), primary_key=True)
    item_pedido: Mapped[int] = mapped_column(Integer, primary_key=True)
    quantidade: Mapped[Decimal] = mapped_column(DINHEIRO)


class ParametroRow(Base):
    __tablename__ = "parametro"
    nome: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[str] = mapped_column(String(200))


class NotaEntradaRow(Base):
    """Cada XML que entrou, com a decisão do motor (aceita ou não)."""

    __tablename__ = "nota_entrada"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chave: Mapped[str] = mapped_column(String(44), index=True)
    arquivo: Mapped[str] = mapped_column(String(200))
    origem: Mapped[str] = mapped_column(String(20))  # arquivo | soap | ocr
    emitente_cnpj: Mapped[str] = mapped_column(String(14))
    numero: Mapped[int] = mapped_column(Integer)
    valor_nota: Mapped[Decimal] = mapped_column(DINHEIRO)
    status: Mapped[str] = mapped_column(String(12), index=True)
    divergencias_json: Mapped[str] = mapped_column(Text)
    impacto_reais: Mapped[Decimal] = mapped_column(DINHEIRO)
    xml: Mapped[bytes] = mapped_column(LargeBinary)
    recebida_em: Mapped[datetime] = mapped_column(DateTime)
    decisao: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decisao_obs: Mapped[str | None] = mapped_column(Text, nullable=True)


def criar_engine(url: str = "sqlite:///:memory:") -> Engine:
    if url.endswith(":memory:"):
        # Banco em memória existe só dentro de UMA conexão. A API atende em várias threads,
        # então todas precisam compartilhar a mesma conexão (StaticPool).
        engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        engine = create_engine(url)
    Base.metadata.create_all(engine)
    return engine


@contextmanager
def sessao(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise


# ============================== carga ==============================


def carregar_erp_json(engine: Engine, caminho: Path) -> None:
    """Carrega `erp.json` (gerado por `python -m recebimento gerar`). Idempotente."""
    dados: dict[str, Any] = json.loads(caminho.read_text(encoding="utf-8"))
    with sessao(engine) as s:
        for f in dados["fornecedores"]:
            s.merge(FornecedorRow(**Fornecedor.model_validate(f).model_dump()))
        for p in dados["pedidos"]:
            ped = PedidoCompra.model_validate(p)
            s.merge(
                PedidoRow(
                    numero=ped.numero,
                    fornecedor_cnpj=ped.fornecedor_cnpj,
                    data=ped.data,
                    itens=[ItemPedidoRow(pedido=ped.numero, **i.model_dump()) for i in ped.itens],
                )
            )
        for r in dados["recebimentos"]:
            rec = Recebimento.model_validate(r)
            s.merge(
                RecebimentoRow(
                    numero=rec.numero,
                    pedido=rec.pedido,
                    data=rec.data,
                    itens=[
                        ItemRecebidoRow(recebimento=rec.numero, **i.model_dump()) for i in rec.itens
                    ],
                )
            )
        s.merge(ParametroRow(nome="compradora_cnpj", valor=dados["compradora"]["cnpj"]))
        for nome, valor in dados["tolerancias"].items():
            s.merge(ParametroRow(nome=f"tolerancia.{nome}", valor=str(valor)))


# ============================== consultas ==============================


class Erp:
    """Consultas e gravações que o serviço de recebimento usa."""

    def __init__(self, s: Session) -> None:
        self.s = s

    def parametro(self, nome: str) -> str | None:
        row = self.s.get(ParametroRow, nome)
        return row.valor if row else None

    def compradora_cnpj(self) -> str:
        return self.parametro("compradora_cnpj") or ""

    def tolerancias(self) -> Tolerancias:
        padrao = Tolerancias()
        return Tolerancias(
            preco_percentual=Decimal(
                self.parametro("tolerancia.preco_percentual") or padrao.preco_percentual
            ),
            quantidade_unidades=Decimal(
                self.parametro("tolerancia.quantidade_unidades") or padrao.quantidade_unidades
            ),
            imposto_reais=Decimal(
                self.parametro("tolerancia.imposto_reais") or padrao.imposto_reais
            ),
        )

    def fornecedor(self, cnpj: str) -> Fornecedor | None:
        row = self.s.get(FornecedorRow, cnpj)
        if row is None:
            return None
        return Fornecedor(cnpj=row.cnpj, razao_social=row.razao_social, uf=row.uf, email=row.email)

    def pedido(self, numero: str) -> PedidoCompra | None:
        row = self.s.get(PedidoRow, numero)
        if row is None:
            return None
        return PedidoCompra(
            numero=row.numero,
            fornecedor_cnpj=row.fornecedor_cnpj,
            data=row.data,
            itens=tuple(
                ItemPedido(
                    item=i.item,
                    codigo=i.codigo,
                    descricao=i.descricao,
                    ncm=i.ncm,
                    quantidade=i.quantidade,
                    preco_unitario=i.preco_unitario,
                    aliquota_icms=i.aliquota_icms,
                    aliquota_ipi=i.aliquota_ipi,
                )
                for i in row.itens
            ),
        )

    def recebimentos(self, pedido: str) -> tuple[Recebimento, ...]:
        rows = self.s.scalars(select(RecebimentoRow).where(RecebimentoRow.pedido == pedido))
        return tuple(
            Recebimento(
                numero=r.numero,
                pedido=r.pedido,
                data=r.data,
                itens=tuple(
                    ItemRecebido(item_pedido=i.item_pedido, quantidade=i.quantidade)
                    for i in r.itens
                ),
            )
            for r in rows
        )

    def chave_aceita(self, chave: str) -> bool:
        """A chave já entrou antes (liberada ou bloqueada)? Rejeitadas não contam."""
        return (
            self.s.scalar(
                select(NotaEntradaRow.id).where(
                    NotaEntradaRow.chave == chave, NotaEntradaRow.status != "rejeitada"
                )
            )
            is not None
        )

    def registrar(
        self,
        *,
        chave: str,
        arquivo: str,
        origem: str,
        emitente_cnpj: str,
        numero: int,
        valor_nota: Decimal,
        resultado: Resultado,
        xml: bytes,
    ) -> NotaEntradaRow:
        row = NotaEntradaRow(
            chave=chave,
            arquivo=arquivo,
            origem=origem,
            emitente_cnpj=emitente_cnpj,
            numero=numero,
            valor_nota=valor_nota,
            status=resultado.status.value,
            divergencias_json=json.dumps(
                [
                    {
                        "codigo": d.codigo,
                        "mensagem": d.mensagem,
                        "item_nota": d.item_nota,
                        "esperado": None if d.esperado is None else str(d.esperado),
                        "encontrado": None if d.encontrado is None else str(d.encontrado),
                        "impacto_reais": str(d.impacto_reais),
                    }
                    for d in resultado.divergencias
                ],
                ensure_ascii=False,
            ),
            impacto_reais=resultado.impacto_reais,
            xml=xml,
            recebida_em=datetime.now(UTC).replace(tzinfo=None),
        )
        self.s.add(row)
        self.s.flush()
        return row

    def notas(self, status: str | None = None) -> list[NotaEntradaRow]:
        consulta = select(NotaEntradaRow).order_by(NotaEntradaRow.id)
        if status:
            consulta = consulta.where(NotaEntradaRow.status == status)
        return list(self.s.scalars(consulta))
