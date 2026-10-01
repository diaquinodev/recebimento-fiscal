import pytest

from recebimento.chave import (
    chave_valida,
    cnpj_valido,
    completar_cnpj,
    decompor_chave,
    dv_chave,
    montar_chave,
)


def test_cnpj_exemplo_classico() -> None:
    # 11.222.333/0001-81: exemplo de CNPJ válido usado em material didático
    assert completar_cnpj("112223330001") == "11222333000181"
    assert cnpj_valido("11222333000181")
    assert not cnpj_valido("11222333000182")
    assert not cnpj_valido("11111111111111")


def test_montar_e_decompor_chave() -> None:
    cnpj = completar_cnpj("101010100001")
    chave = montar_chave("SP", "2609", cnpj, serie=1, numero=1234, codigo_numerico="87654321")
    assert len(chave) == 44
    assert chave_valida(chave)
    partes = decompor_chave(chave)
    assert (partes.uf, partes.ano_mes, partes.cnpj) == ("SP", "2609", cnpj)
    assert (partes.modelo, partes.serie, partes.numero) == ("55", 1, 1234)


def test_qualquer_digito_trocado_invalida_a_chave() -> None:
    chave = montar_chave("MG", "2610", completar_cnpj("202020200001"), 1, 99, "1")
    for pos in range(44):
        digito = int(chave[pos])
        trocada = chave[:pos] + str((digito + 1) % 10) + chave[pos + 1 :]
        assert not chave_valida(trocada), f"troca na posição {pos} passou"


def test_dv_resto_zero_ou_um_vira_zero() -> None:
    # procura bases com resto 0 e 1 para cobrir a regra especial do módulo 11
    vistos = set()
    for n in range(2000):
        # cUF+AAMM+CNPJ+mod+série (25) + nNF (9) + tpEmis (1) + cNF (8) = 43
        base = f"3526091122233300018155001{n:09d}112345678"
        assert len(base) == 43
        soma = sum(
            int(d) * p
            for d, p in zip(reversed(base), ([2, 3, 4, 5, 6, 7, 8, 9] * 6)[:43], strict=True)
        )
        if soma % 11 in (0, 1):
            assert dv_chave(base) == 0
            vistos.add(soma % 11)
    assert vistos == {0, 1}


@pytest.mark.parametrize("chave", ["", "123", "a" * 44, "1" * 45])
def test_chave_malformada(chave: str) -> None:
    assert not chave_valida(chave)
