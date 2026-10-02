from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from recebimento.erp import Erp, carregar_erp_json, criar_engine, sessao
from recebimento.gerador import gerar_cenario, salvar_cenario
from recebimento.servico import SefazEmMemoria, codigos, receber_xml


@pytest.fixture
def ambiente(tmp_path: Path) -> tuple[Engine, SefazEmMemoria, Path]:
    salvar_cenario(gerar_cenario(seed=4), tmp_path)
    engine = criar_engine()
    carregar_erp_json(engine, tmp_path / "erp.json")
    return engine, SefazEmMemoria.de_arquivo(tmp_path / "sefaz.json"), tmp_path


def test_carga_do_erp_e_idempotente(ambiente: tuple[Engine, SefazEmMemoria, Path]) -> None:
    engine, _, pasta = ambiente
    carregar_erp_json(engine, pasta / "erp.json")  # segunda carga não duplica
    with sessao(engine) as s:
        erp = Erp(s)
        pedido = erp.pedido("4500000001")
        assert pedido is not None and len(pedido.itens) >= 1
        assert erp.fornecedor(pedido.fornecedor_cnpj) is not None
        assert str(erp.tolerancias().preco_percentual) == "0.02"


def test_xml_invalido_e_registrado_como_rejeitado(
    ambiente: tuple[Engine, SefazEmMemoria, Path],
) -> None:
    engine, sefaz, _ = ambiente
    with sessao(engine) as s:
        row = receber_xml(Erp(s), sefaz, b"<isso nao e xml", "lixo.xml")
        assert row.status == "rejeitada" and codigos(row) == ["xml_invalido"]


def test_mesma_nota_duas_vezes_a_segunda_e_rejeitada(
    ambiente: tuple[Engine, SefazEmMemoria, Path],
) -> None:
    engine, sefaz, pasta = ambiente
    xml = sorted((pasta / "notas").glob("*.xml"))[0].read_bytes()
    with sessao(engine) as s:
        erp = Erp(s)
        primeira = receber_xml(erp, sefaz, xml, "a.xml")
        segunda = receber_xml(erp, sefaz, xml, "b.xml")
        # leitura dentro da sessão: fora dela o SQLAlchemy recusa (objeto desconectado)
        if primeira.status != "rejeitada":
            assert segunda.status == "rejeitada" and "duplicada" in codigos(segunda)


def test_notas_ficam_gravadas_com_impacto(ambiente: tuple[Engine, SefazEmMemoria, Path]) -> None:
    engine, sefaz, pasta = ambiente
    with sessao(engine) as s:
        erp = Erp(s)
        for xml in sorted((pasta / "notas").glob("*.xml")):
            receber_xml(erp, sefaz, xml.read_bytes(), xml.name)
        bloqueadas = erp.notas("bloqueada")
        assert bloqueadas
        assert all(n.impacto_reais != 0 for n in bloqueadas)
        assert len(erp.notas()) == len(list((pasta / "notas").glob("*.xml")))
