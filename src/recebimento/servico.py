"""Serviço de recebimento: XML → leitura → contexto do ERP → motor → registro.

É a única porta de entrada de notas (arquivo, SOAP ou OCR), para que toda nota passe pelas
mesmas regras e fique registrada com a decisão.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from recebimento.chave import chave_valida
from recebimento.dominio import NotaFiscal, PedidoCompra, SituacaoSefaz
from recebimento.erp import Erp, NotaEntradaRow
from recebimento.match import ContextoErp, Divergencia, Resultado, Status, avaliar_nota
from recebimento.nfe_xml import ErroLeituraNFe, ler_xml


class ConsultaSefaz(Protocol):
    def situacao(self, chave: str) -> SituacaoSefaz: ...


class SefazEmMemoria:
    """SEFAZ simulado a partir de um dicionário chave → situação (ex.: `sefaz.json`)."""

    def __init__(self, situacoes: dict[str, str]) -> None:
        self._situacoes = situacoes

    @classmethod
    def de_arquivo(cls, caminho: Path) -> SefazEmMemoria:
        return cls(json.loads(caminho.read_text(encoding="utf-8")))

    def situacao(self, chave: str) -> SituacaoSefaz:
        return SituacaoSefaz(self._situacoes.get(chave, SituacaoSefaz.NAO_ENCONTRADA.value))


def montar_contexto(erp: Erp, sefaz: ConsultaSefaz, nota: NotaFiscal) -> ContextoErp:
    pedidos: dict[str, PedidoCompra] = {}
    for numero in {i.pedido for i in nota.itens if i.pedido}:
        pedido = erp.pedido(numero)
        if pedido is not None:
            pedidos[numero] = pedido
    return ContextoErp(
        compradora_cnpj=erp.compradora_cnpj(),
        pedidos=pedidos,
        recebimentos={numero: erp.recebimentos(numero) for numero in pedidos},
        tolerancias=erp.tolerancias(),
        chave_ja_lancada=erp.chave_aceita(nota.chave),
        situacao_sefaz=sefaz.situacao(nota.chave) if chave_valida(nota.chave) else None,
    )


def receber_xml(
    erp: Erp, sefaz: ConsultaSefaz, conteudo: bytes, arquivo: str, origem: str = "arquivo"
) -> NotaEntradaRow:
    try:
        nota = ler_xml(conteudo)
    except ErroLeituraNFe as erro:
        resultado = Resultado(Status.REJEITADA, (Divergencia("xml_invalido", str(erro)),))
        return erp.registrar(
            chave="",
            arquivo=arquivo,
            origem=origem,
            emitente_cnpj="",
            numero=0,
            valor_nota=Decimal(0),
            resultado=resultado,
            xml=conteudo,
        )
    resultado = avaliar_nota(nota, montar_contexto(erp, sefaz, nota))
    return erp.registrar(
        chave=nota.chave,
        arquivo=arquivo,
        origem=origem,
        emitente_cnpj=nota.emitente_cnpj,
        numero=nota.numero,
        valor_nota=nota.totais.valor_nota,
        resultado=resultado,
        xml=conteudo,
    )


def codigos(row: NotaEntradaRow) -> list[str]:
    return sorted({d["codigo"] for d in json.loads(row.divergencias_json)})
