from decimal import Decimal

import pytest

from recebimento.gerador import gerar_cenario
from recebimento.nfe_xml import ErroLeituraNFe, conferir_nota, gerar_xml, ler_xml


def _nota_ok():  # type: ignore[no-untyped-def]
    cen = gerar_cenario(seed=1)
    arquivo = next(a for a, g in cen.gabarito.items() if g["tipo"] == "ok")
    return dict(cen.notas)[arquivo]


def test_ida_e_volta_xml_preserva_a_nota() -> None:
    nota = _nota_ok()
    assert ler_xml(gerar_xml(nota)) == nota


def test_xml_tem_namespace_e_id_oficiais() -> None:
    xml = gerar_xml(_nota_ok()).decode()
    assert 'xmlns="http://www.portalfiscal.inf.br/nfe"' in xml
    assert 'Id="NFe' in xml
    assert "<xPed>" in xml and "<nItemPed>" in xml


def test_nota_gerada_passa_na_conferencia() -> None:
    assert conferir_nota(_nota_ok()) == []


def test_conferencia_aponta_chave_invalida_e_soma_errada() -> None:
    nota = _nota_ok()
    ruim = nota.model_copy(
        update={
            "chave": nota.chave[:43] + str((int(nota.chave[43]) + 1) % 10),
            "totais": nota.totais.model_copy(
                update={"valor_produtos": nota.totais.valor_produtos + Decimal("10")}
            ),
        }
    )
    codigos = {p.codigo for p in conferir_nota(ruim)}
    assert {"chave_invalida", "total_produtos", "total_nota"} <= codigos


def test_xml_malformado() -> None:
    with pytest.raises(ErroLeituraNFe, match="malformado"):
        ler_xml(b"<nfeProc><NFe>")


def test_xml_que_nao_e_nfe() -> None:
    with pytest.raises(ErroLeituraNFe, match="infNFe"):
        ler_xml(b"<?xml version='1.0'?><pedido/>")


def test_campo_obrigatorio_ausente() -> None:
    xml = gerar_xml(_nota_ok()).replace(b"<nNF>", b"<nNFx>").replace(b"</nNF>", b"</nNFx>")
    with pytest.raises(ErroLeituraNFe, match="ide/nNF"):
        ler_xml(xml)


def test_xxe_nao_le_arquivo_local(tmp_path) -> None:  # type: ignore[no-untyped-def]
    segredo = tmp_path / "segredo.txt"
    segredo.write_text("SENHA-SECRETA")
    xml = (
        gerar_xml(_nota_ok())
        .decode()
        .replace(
            "<?xml version='1.0' encoding='UTF-8'?>",
            f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "{segredo.as_uri()}">]>',
        )
        .replace("<xNome>", "<xNome>&xxe;", 1)
    )
    try:
        nota = ler_xml(xml.encode())
    except ErroLeituraNFe:
        return  # recusar também é seguro
    assert "SENHA-SECRETA" not in nota.emitente_nome
