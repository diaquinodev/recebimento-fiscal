"""Chave de acesso da NF-e (44 dígitos) e CNPJ: montagem e dígitos verificadores.

Composição da chave:
    cUF(2) AAMM(4) CNPJ(14) mod(2) série(3) nNF(9) tpEmis(1) cNF(8) cDV(1)

O dígito verificador é módulo 11 com pesos 2 a 9 aplicados da direita para a esquerda.
Validar em código pega erro de digitação ou de leitura (OCR) antes de qualquer consulta.
"""

from __future__ import annotations

from dataclasses import dataclass

CODIGO_UF = {
    "RO": "11", "AC": "12", "AM": "13", "RR": "14", "PA": "15", "AP": "16", "TO": "17",
    "MA": "21", "PI": "22", "CE": "23", "RN": "24", "PB": "25", "PE": "26", "AL": "27",
    "SE": "28", "BA": "29", "MG": "31", "ES": "32", "RJ": "33", "SP": "35", "PR": "41",
    "SC": "42", "RS": "43", "MS": "50", "MT": "51", "GO": "52", "DF": "53",
}  # fmt: skip


def dv_chave(chave43: str) -> int:
    """Dígito verificador (módulo 11) dos 43 primeiros dígitos da chave."""
    if len(chave43) != 43 or not chave43.isdigit():
        raise ValueError("a base da chave precisa ter 43 dígitos")
    soma = 0
    peso = 2
    for digito in reversed(chave43):
        soma += int(digito) * peso
        peso = 2 if peso == 9 else peso + 1
    resto = soma % 11
    return 0 if resto < 2 else 11 - resto


def chave_valida(chave: str) -> bool:
    return len(chave) == 44 and chave.isdigit() and dv_chave(chave[:43]) == int(chave[43])


@dataclass(frozen=True)
class PartesChave:
    uf: str
    ano_mes: str
    cnpj: str
    modelo: str
    serie: int
    numero: int
    tipo_emissao: str
    codigo_numerico: str


def montar_chave(
    uf: str, ano_mes: str, cnpj: str, serie: int, numero: int, codigo_numerico: str
) -> str:
    """Monta a chave com modelo 55 (NF-e), emissão normal (1) e o DV calculado."""
    base = f"{CODIGO_UF[uf]}{ano_mes}{cnpj}55{serie:03d}{numero:09d}1{codigo_numerico:0>8}"
    return base + str(dv_chave(base))


def decompor_chave(chave: str) -> PartesChave:
    if len(chave) != 44 or not chave.isdigit():
        raise ValueError("chave precisa ter 44 dígitos")
    uf = next((sigla for sigla, cod in CODIGO_UF.items() if cod == chave[:2]), "??")
    return PartesChave(
        uf=uf,
        ano_mes=chave[2:6],
        cnpj=chave[6:20],
        modelo=chave[20:22],
        serie=int(chave[22:25]),
        numero=int(chave[25:34]),
        tipo_emissao=chave[34],
        codigo_numerico=chave[35:43],
    )


def _dv_cnpj(base: str) -> int:
    pesos = list(range(len(base) - 7, 1, -1)) + list(range(9, 1, -1))
    resto = sum(int(d) * p for d, p in zip(base, pesos, strict=True)) % 11
    return 0 if resto < 2 else 11 - resto


def completar_cnpj(base12: str) -> str:
    """Acrescenta os dois dígitos verificadores a um CNPJ de 12 dígitos."""
    if len(base12) != 12 or not base12.isdigit():
        raise ValueError("a base do CNPJ precisa ter 12 dígitos")
    com_primeiro = base12 + str(_dv_cnpj(base12))
    return com_primeiro + str(_dv_cnpj(com_primeiro))


def cnpj_valido(cnpj: str) -> bool:
    return (
        len(cnpj) == 14
        and cnpj.isdigit()
        and len(set(cnpj)) > 1
        and completar_cnpj(cnpj[:12]) == cnpj
    )
