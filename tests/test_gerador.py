import json
from pathlib import Path

from recebimento.chave import chave_valida, cnpj_valido
from recebimento.gerador import ESPERADO, TIPOS, gerar_cenario, salvar_cenario
from recebimento.nfe_xml import conferir_nota, ler_xml


def test_mesma_semente_gera_o_mesmo_cenario() -> None:
    a, b = gerar_cenario(seed=7), gerar_cenario(seed=7)
    assert [n.chave for _, n in a.notas] == [n.chave for _, n in b.notas]
    assert a.gabarito == b.gabarito


def test_todos_os_tipos_aparecem_com_o_esperado() -> None:
    cen = gerar_cenario(seed=3, repeticoes=2)
    tipos = [g["tipo"] for g in cen.gabarito.values()]
    for tipo in TIPOS:
        if tipo != "duplicada":
            assert tipos.count(tipo) >= 2, tipo
    for g in cen.gabarito.values():
        assert (g["status"], g["divergencias"]) == ESPERADO[g["tipo"]]


def test_cnpjs_validos_e_chaves_validas_salvo_a_plantada() -> None:
    cen = gerar_cenario(seed=5)
    assert all(cnpj_valido(f.cnpj) for f in cen.fornecedores)
    for arquivo, nota in cen.notas:
        esperado_valida = cen.gabarito[arquivo]["tipo"] != "chave_invalida"
        assert chave_valida(nota.chave) == esperado_valida, arquivo


def test_notas_so_tem_os_problemas_plantados() -> None:
    cen = gerar_cenario(seed=11)
    for arquivo, nota in cen.notas:
        codigos = {p.codigo for p in conferir_nota(nota)}
        if cen.gabarito[arquivo]["tipo"] == "chave_invalida":
            assert codigos == {"chave_invalida"}, arquivo
        else:
            assert codigos == set(), (arquivo, codigos)


def test_salvar_grava_arquivos_legiveis(tmp_path: Path) -> None:
    cen = gerar_cenario(seed=2, repeticoes=1)
    salvar_cenario(cen, tmp_path)
    erp = json.loads((tmp_path / "erp.json").read_text(encoding="utf-8"))
    assert len(erp["pedidos"]) == len(cen.pedidos)
    xmls = sorted((tmp_path / "notas").glob("*.xml"))
    assert len(xmls) == len(cen.notas)
    lidas = {p.name: ler_xml(p.read_bytes()) for p in xmls}
    assert lidas == dict(cen.notas)
