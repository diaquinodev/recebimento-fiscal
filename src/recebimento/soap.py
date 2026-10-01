"""SOAP 1.1 nos dois sentidos, sem framework: só lxml, para o envelope ficar visível.

1. Serviço de recebimento (`receberNFe`): outro sistema envia a NF-e (base64) e recebe a
   decisão do 3-way match. Contrato em `WSDL_RECEBIMENTO`.
2. SEFAZ simulado (`nfeConsultaNF`): imita a consulta de situação por chave do serviço
   NFeConsultaProtocolo4. O serviço real exige certificado digital (TLS mútuo) da empresa;
   aqui a resposta vem de um dicionário.
3. Cliente do SEFAZ: monta o envelope de consulta, envia por HTTP e traduz `cStat`.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from lxml import etree

from recebimento.dominio import SituacaoSefaz

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
NS_RF = "urn:recebimento-fiscal:v1"
NS_NFE = "http://www.portalfiscal.inf.br/nfe"
NS_WS_CONSULTA = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeConsultaProtocolo4"

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)

#: cStat da consulta de situação → situação. 100 autorizada, 101 cancelada, 110 denegada,
#: 217 não consta na base do SEFAZ.
CSTAT = {
    "100": SituacaoSefaz.AUTORIZADA,
    "101": SituacaoSefaz.CANCELADA,
    "110": SituacaoSefaz.DENEGADA,
    "217": SituacaoSefaz.NAO_ENCONTRADA,
}
_MOTIVO = {
    SituacaoSefaz.AUTORIZADA: ("100", "Autorizado o uso da NF-e"),
    SituacaoSefaz.CANCELADA: ("101", "Cancelamento de NF-e homologado"),
    SituacaoSefaz.DENEGADA: ("110", "Uso Denegado"),
    SituacaoSefaz.NAO_ENCONTRADA: ("217", "NF-e não consta na base de dados da SEFAZ"),
}


class ErroSoap(ValueError):
    """Envelope inválido: vira SOAP Fault com faultcode Client."""


# ============================== envelope ==============================


def envelope(corpo: etree._Element) -> bytes:
    env = etree.Element(f"{{{SOAP_ENV}}}Envelope", nsmap={"soap": SOAP_ENV})
    etree.SubElement(env, f"{{{SOAP_ENV}}}Body").append(corpo)
    return bytes(etree.tostring(env, xml_declaration=True, encoding="UTF-8"))


def fault(mensagem: str, codigo: str = "soap:Client") -> bytes:
    f = etree.Element(f"{{{SOAP_ENV}}}Fault")
    etree.SubElement(f, "faultcode").text = codigo
    etree.SubElement(f, "faultstring").text = mensagem
    return envelope(f)


def corpo_da_requisicao(conteudo: bytes) -> etree._Element:
    """Primeiro elemento dentro de soap:Body. Erros viram `ErroSoap`."""
    try:
        raiz = etree.fromstring(conteudo, parser=_PARSER)
    except etree.XMLSyntaxError as erro:
        raise ErroSoap(f"envelope malformado: {erro}") from erro
    if raiz.tag != f"{{{SOAP_ENV}}}Envelope":
        raise ErroSoap("raiz não é soap:Envelope (SOAP 1.1)")
    body = raiz.find(f"{{{SOAP_ENV}}}Body")
    if body is None or len(body) == 0:
        raise ErroSoap("soap:Body vazio")
    return body[0]


def _filho(pai: etree._Element, ns: str, nome: str) -> str:
    el = pai.find(f"{{{ns}}}{nome}")
    if el is None or el.text is None:
        raise ErroSoap(f"campo obrigatório ausente: {nome}")
    return el.text.strip()


# ============================== recebimento ==============================


@dataclass(frozen=True)
class RespostaRecebimento:
    status: str
    chave: str
    impacto_reais: str
    divergencias: list[tuple[str, str]]


def pedido_receber(xml_nfe: bytes, arquivo: str) -> bytes:
    """Envelope `receberNFe` (o que um sistema cliente enviaria)."""
    op = etree.Element(f"{{{NS_RF}}}receberNFe", nsmap={"rf": NS_RF})
    etree.SubElement(op, f"{{{NS_RF}}}arquivo").text = arquivo
    etree.SubElement(op, f"{{{NS_RF}}}xmlNFe").text = base64.b64encode(xml_nfe).decode()
    return envelope(op)


def atender_recebimento(
    conteudo: bytes, receber: Callable[[bytes, str], RespostaRecebimento]
) -> tuple[int, bytes]:
    """Trata a requisição SOAP; devolve (status HTTP, envelope de resposta ou Fault)."""
    try:
        op = corpo_da_requisicao(conteudo)
        if op.tag != f"{{{NS_RF}}}receberNFe":
            raise ErroSoap(f"operação desconhecida: {etree.QName(op).localname}")
        arquivo = _filho(op, NS_RF, "arquivo")
        try:
            xml_nfe = base64.b64decode(_filho(op, NS_RF, "xmlNFe"), validate=True)
        except binascii.Error as erro:
            raise ErroSoap("xmlNFe não é base64 válido") from erro
    except ErroSoap as erro:
        return 500, fault(str(erro))  # SOAP 1.1: Fault vai com HTTP 500

    r = receber(xml_nfe, arquivo)
    resp = etree.Element(f"{{{NS_RF}}}receberNFeResponse", nsmap={"rf": NS_RF})
    etree.SubElement(resp, f"{{{NS_RF}}}status").text = r.status
    etree.SubElement(resp, f"{{{NS_RF}}}chave").text = r.chave
    etree.SubElement(resp, f"{{{NS_RF}}}impactoReais").text = r.impacto_reais
    for codigo, mensagem in r.divergencias:
        d = etree.SubElement(resp, f"{{{NS_RF}}}divergencia")
        etree.SubElement(d, f"{{{NS_RF}}}codigo").text = codigo
        etree.SubElement(d, f"{{{NS_RF}}}mensagem").text = mensagem
    return 200, envelope(resp)


def ler_resposta_recebimento(conteudo: bytes) -> RespostaRecebimento:
    corpo = corpo_da_requisicao(conteudo)
    if corpo.tag == f"{{{SOAP_ENV}}}Fault":
        raise ErroSoap(corpo.findtext("faultstring") or "SOAP Fault")
    return RespostaRecebimento(
        status=_filho(corpo, NS_RF, "status"),
        chave=corpo.findtext(f"{{{NS_RF}}}chave") or "",
        impacto_reais=_filho(corpo, NS_RF, "impactoReais"),
        divergencias=[
            (_filho(d, NS_RF, "codigo"), _filho(d, NS_RF, "mensagem"))
            for d in corpo.findall(f"{{{NS_RF}}}divergencia")
        ],
    )


# ============================== SEFAZ simulado ==============================


def pedido_consulta_sefaz(chave: str) -> bytes:
    """Envelope de consulta de situação (consSitNFe 4.00 dentro de nfeDadosMsg)."""
    msg = etree.Element(f"{{{NS_WS_CONSULTA}}}nfeDadosMsg", nsmap={None: NS_WS_CONSULTA})  # type: ignore[dict-item]
    cons = etree.SubElement(msg, f"{{{NS_NFE}}}consSitNFe", nsmap={None: NS_NFE}, versao="4.00")  # type: ignore[dict-item]
    etree.SubElement(cons, f"{{{NS_NFE}}}tpAmb").text = "2"  # 2 = homologação
    etree.SubElement(cons, f"{{{NS_NFE}}}xServ").text = "CONSULTAR"
    etree.SubElement(cons, f"{{{NS_NFE}}}chNFe").text = chave
    return envelope(msg)


def atender_sefaz(conteudo: bytes, situacoes: dict[str, str]) -> tuple[int, bytes]:
    try:
        msg = corpo_da_requisicao(conteudo)
        cons = msg.find(f"{{{NS_NFE}}}consSitNFe")
        if msg.tag != f"{{{NS_WS_CONSULTA}}}nfeDadosMsg" or cons is None:
            raise ErroSoap("esperado nfeDadosMsg/consSitNFe")
        chave = _filho(cons, NS_NFE, "chNFe")
    except ErroSoap as erro:
        return 500, fault(str(erro))
    situacao = SituacaoSefaz(situacoes.get(chave, SituacaoSefaz.NAO_ENCONTRADA.value))
    cstat, motivo = _MOTIVO[situacao]
    resp = etree.Element(
        f"{{{NS_WS_CONSULTA}}}nfeResultMsg",
        nsmap={None: NS_WS_CONSULTA},  # type: ignore[dict-item]
    )
    ret = etree.SubElement(resp, f"{{{NS_NFE}}}retConsSitNFe", nsmap={None: NS_NFE}, versao="4.00")  # type: ignore[dict-item]
    etree.SubElement(ret, f"{{{NS_NFE}}}tpAmb").text = "2"
    etree.SubElement(ret, f"{{{NS_NFE}}}cStat").text = cstat
    etree.SubElement(ret, f"{{{NS_NFE}}}xMotivo").text = motivo
    etree.SubElement(ret, f"{{{NS_NFE}}}chNFe").text = chave
    return 200, envelope(resp)


class SefazSoapCliente:
    """Consulta a situação da NF-e por SOAP (implementa `servico.ConsultaSefaz`)."""

    def __init__(self, url: str, http: httpx.Client | None = None) -> None:
        self.url = url
        self.http = http or httpx.Client(timeout=10)

    def situacao(self, chave: str) -> SituacaoSefaz:
        resposta = self.http.post(
            self.url,
            content=pedido_consulta_sefaz(chave),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "nfeConsultaNF"},
        )
        corpo = corpo_da_requisicao(resposta.content)
        if corpo.tag == f"{{{SOAP_ENV}}}Fault":
            raise ErroSoap(corpo.findtext("faultstring") or "SOAP Fault do SEFAZ")
        cstat = corpo.findtext(f".//{{{NS_NFE}}}cStat") or ""
        if cstat not in CSTAT:
            raise ErroSoap(f"cStat inesperado do SEFAZ: {cstat!r}")
        return CSTAT[cstat]


# ============================== WSDL ==============================

WSDL_RECEBIMENTO = """<?xml version="1.0" encoding="UTF-8"?>
<wsdl:definitions name="RecebimentoFiscal"
    targetNamespace="urn:recebimento-fiscal:v1"
    xmlns:rf="urn:recebimento-fiscal:v1"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema"
    xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
    xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/">
  <wsdl:types>
    <xsd:schema targetNamespace="urn:recebimento-fiscal:v1" elementFormDefault="qualified">
      <xsd:element name="receberNFe">
        <xsd:complexType>
          <xsd:sequence>
            <xsd:element name="arquivo" type="xsd:string"/>
            <xsd:element name="xmlNFe" type="xsd:base64Binary"/>
          </xsd:sequence>
        </xsd:complexType>
      </xsd:element>
      <xsd:complexType name="Divergencia">
        <xsd:sequence>
          <xsd:element name="codigo" type="xsd:string"/>
          <xsd:element name="mensagem" type="xsd:string"/>
        </xsd:sequence>
      </xsd:complexType>
      <xsd:element name="receberNFeResponse">
        <xsd:complexType>
          <xsd:sequence>
            <xsd:element name="status" type="xsd:string"/>
            <xsd:element name="chave" type="xsd:string"/>
            <xsd:element name="impactoReais" type="xsd:decimal"/>
            <xsd:element name="divergencia" type="rf:Divergencia"
                         minOccurs="0" maxOccurs="unbounded"/>
          </xsd:sequence>
        </xsd:complexType>
      </xsd:element>
    </xsd:schema>
  </wsdl:types>
  <wsdl:message name="receberNFeRequest">
    <wsdl:part name="parameters" element="rf:receberNFe"/>
  </wsdl:message>
  <wsdl:message name="receberNFeResponse">
    <wsdl:part name="parameters" element="rf:receberNFeResponse"/>
  </wsdl:message>
  <wsdl:portType name="RecebimentoPortType">
    <wsdl:operation name="receberNFe">
      <wsdl:input message="rf:receberNFeRequest"/>
      <wsdl:output message="rf:receberNFeResponse"/>
    </wsdl:operation>
  </wsdl:portType>
  <wsdl:binding name="RecebimentoBinding" type="rf:RecebimentoPortType">
    <soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
    <wsdl:operation name="receberNFe">
      <soap:operation soapAction="receberNFe"/>
      <wsdl:input><soap:body use="literal"/></wsdl:input>
      <wsdl:output><soap:body use="literal"/></wsdl:output>
    </wsdl:operation>
  </wsdl:binding>
  <wsdl:service name="RecebimentoService">
    <wsdl:port name="RecebimentoPort" binding="rf:RecebimentoBinding">
      <soap:address location="{endereco}"/>
    </wsdl:port>
  </wsdl:service>
</wsdl:definitions>
"""
