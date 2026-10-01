import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lxml import etree

from recebimento.api import criar_app
from recebimento.dominio import SituacaoSefaz
from recebimento.erp import carregar_erp_json, criar_engine
from recebimento.gerador import gerar_cenario, salvar_cenario
from recebimento.servico import SefazEmMemoria
from recebimento.soap import (
    ErroSoap,
    SefazSoapCliente,
    ler_resposta_recebimento,
    pedido_receber,
)


@pytest.fixture
def pasta(tmp_path: Path) -> Path:
    salvar_cenario(gerar_cenario(seed=8), tmp_path)
    return tmp_path


@pytest.fixture
def cliente(pasta: Path) -> TestClient:
    engine = criar_engine()
    carregar_erp_json(engine, pasta / "erp.json")
    situacoes = json.loads((pasta / "sefaz.json").read_text(encoding="utf-8"))
    return TestClient(criar_app(engine, SefazEmMemoria(situacoes), situacoes))


def _gabarito(pasta: Path) -> dict[str, dict[str, object]]:
    return json.loads((pasta / "gabarito.json").read_text(encoding="utf-8"))


def test_wsdl_e_xml_valido_com_endereco(cliente: TestClient) -> None:
    r = cliente.get("/soap/recebimento?wsdl")
    assert r.status_code == 200
    wsdl = etree.fromstring(r.content)
    assert wsdl.get("name") == "RecebimentoFiscal"
    assert b"http://testserver/soap/recebimento" in r.content


def test_soap_recebe_todas_as_notas_com_a_decisao_do_gabarito(
    cliente: TestClient, pasta: Path
) -> None:
    gab = _gabarito(pasta)
    for xml in sorted((pasta / "notas").glob("*.xml"), key=lambda p: p.stem):
        r = cliente.post(
            "/soap/recebimento",
            content=pedido_receber(xml.read_bytes(), xml.name),
            headers={"Content-Type": "text/xml", "SOAPAction": "receberNFe"},
        )
        assert r.status_code == 200, r.text
        resposta = ler_resposta_recebimento(r.content)
        assert resposta.status == gab[xml.name]["status"], xml.name
        assert sorted({c for c, _ in resposta.divergencias}) == gab[xml.name]["divergencias"]
    notas = cliente.get("/notas").json()
    assert len(notas) == len(gab) and all(n["origem"] == "soap" for n in notas)


def test_envelope_do_zeep_cliente_de_mercado_e_aceito(
    cliente: TestClient, pasta: Path, tmp_path: Path
) -> None:
    zeep = pytest.importorskip("zeep")
    wsdl = tmp_path / "recebimento.wsdl"
    wsdl.write_bytes(cliente.get("/soap/recebimento?wsdl").content)
    zc = zeep.Client(str(wsdl))
    xml = sorted((pasta / "notas").glob("*.xml"))[0].read_bytes()
    mensagem = zc.create_message(zc.service, "receberNFe", arquivo="via-zeep.xml", xmlNFe=xml)
    r = cliente.post(
        "/soap/recebimento",
        content=etree.tostring(mensagem),
        headers={"Content-Type": "text/xml", "SOAPAction": "receberNFe"},
    )
    assert r.status_code == 200, r.text
    assert ler_resposta_recebimento(r.content).status in {"liberada", "bloqueada", "rejeitada"}


@pytest.mark.parametrize(
    ("corpo", "trecho"),
    [
        (b"isso nao e xml", "malformado"),
        (b"<a/>", "soap:Envelope"),
        (
            b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            b'<x:outra xmlns:x="urn:recebimento-fiscal:v1"/></s:Body></s:Envelope>',
            "operação desconhecida",
        ),
    ],
)
def test_requisicao_ruim_vira_soap_fault(cliente: TestClient, corpo: bytes, trecho: str) -> None:
    r = cliente.post("/soap/recebimento", content=corpo)
    assert r.status_code == 500
    with pytest.raises(ErroSoap, match=trecho):
        ler_resposta_recebimento(r.content)


def test_cliente_soap_do_sefaz_contra_o_simulado(cliente: TestClient, pasta: Path) -> None:
    sefaz = SefazSoapCliente("/sefaz/NFeConsultaProtocolo4", http=cliente)
    situacoes = json.loads((pasta / "sefaz.json").read_text(encoding="utf-8"))
    for chave, situacao in situacoes.items():
        assert sefaz.situacao(chave) == SituacaoSefaz(situacao)
    assert sefaz.situacao("3" * 44) == SituacaoSefaz.NAO_ENCONTRADA


def test_recebimento_consultando_sefaz_por_soap_de_ponta_a_ponta(pasta: Path) -> None:
    """O recebimento usa o cliente SOAP do SEFAZ, que chama o SEFAZ simulado por HTTP."""
    engine = criar_engine()
    carregar_erp_json(engine, pasta / "erp.json")
    situacoes = json.loads((pasta / "sefaz.json").read_text(encoding="utf-8"))
    sefaz_app = TestClient(criar_app(criar_engine(), SefazEmMemoria({}), situacoes))
    sefaz = SefazSoapCliente("/sefaz/NFeConsultaProtocolo4", http=sefaz_app)
    app = TestClient(criar_app(engine, sefaz, situacoes))
    gab = _gabarito(pasta)
    for xml in sorted((pasta / "notas").glob("*.xml"), key=lambda p: p.stem):
        r = app.post("/soap/recebimento", content=pedido_receber(xml.read_bytes(), xml.name))
        assert ler_resposta_recebimento(r.content).status == gab[xml.name]["status"], xml.name


def test_api_json_filtra_por_status(cliente: TestClient, pasta: Path) -> None:
    for xml in sorted((pasta / "notas").glob("*.xml"), key=lambda p: p.stem):
        cliente.post("/soap/recebimento", content=pedido_receber(xml.read_bytes(), xml.name))
    bloqueadas = cliente.get("/notas", params={"status": "bloqueada"}).json()
    assert bloqueadas and all(n["status"] == "bloqueada" for n in bloqueadas)
    detalhe = cliente.get(f"/notas/{bloqueadas[0]['id']}").json()
    assert detalhe["divergencias"]
    assert cliente.get("/notas/99999").status_code == 404
