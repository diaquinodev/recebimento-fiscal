"""Eval do motor: gera N cenários, processa todas as notas e compara com o gabarito.

Mede o que importa para o negócio:
- detecção: das notas que deveriam ser barradas, quantas foram (status e motivo certos);
- falso bloqueio: notas boas retidas à toa (custa relacionamento com fornecedor).
"""

from __future__ import annotations

import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from recebimento.danfe import gerar_imagem
from recebimento.dominio import NotaFiscal
from recebimento.erp import Erp, carregar_erp_json, criar_engine, sessao
from recebimento.gerador import gerar_cenario, salvar_cenario
from recebimento.ia import ProvedorIA
from recebimento.nfe_xml import conferir_nota
from recebimento.ocr import PROMPT_VERSAO, ler_danfe
from recebimento.servico import SefazEmMemoria, codigos, receber_xml


@dataclass
class RelatorioEval:
    notas: int = 0
    acertos: int = 0
    esperadas_barradas: int = 0
    barradas_certo: int = 0
    esperadas_liberadas: int = 0
    falsos_bloqueios: int = 0
    erros_por_tipo: Counter[str] = field(default_factory=Counter)
    exemplos_erro: list[str] = field(default_factory=list)

    @property
    def deteccao(self) -> float:
        return self.barradas_certo / self.esperadas_barradas if self.esperadas_barradas else 1.0

    @property
    def taxa_falso_bloqueio(self) -> float:
        return self.falsos_bloqueios / self.esperadas_liberadas if self.esperadas_liberadas else 0.0

    @property
    def perfeito(self) -> bool:
        return self.acertos == self.notas


def avaliar_cenario(seed: int, pasta: Path, relatorio: RelatorioEval) -> None:
    cen = gerar_cenario(seed=seed)
    salvar_cenario(cen, pasta)
    engine = criar_engine()
    carregar_erp_json(engine, pasta / "erp.json")
    sefaz = SefazEmMemoria.de_arquivo(pasta / "sefaz.json")
    with sessao(engine) as s:
        erp = Erp(s)
        for arquivo, _ in cen.notas:  # na ordem: a duplicada chega depois da original
            row = receber_xml(erp, sefaz, (pasta / "notas" / arquivo).read_bytes(), arquivo)
            esperado = cen.gabarito[arquivo]
            obtido = (row.status, codigos(row))
            relatorio.notas += 1
            certo = obtido == (esperado["status"], esperado["divergencias"])
            relatorio.acertos += certo
            if esperado["status"] == "liberada":
                relatorio.esperadas_liberadas += 1
                relatorio.falsos_bloqueios += row.status != "liberada"
            else:
                relatorio.esperadas_barradas += 1
                relatorio.barradas_certo += certo
            if not certo:
                relatorio.erros_por_tipo[esperado["tipo"]] += 1
                if len(relatorio.exemplos_erro) < 10:
                    relatorio.exemplos_erro.append(
                        f"seed {seed} {arquivo} ({esperado['tipo']}): esperado "
                        f"{esperado['status']} {esperado['divergencias']}, obtido {obtido}"
                    )


def avaliar(cenarios: int = 20) -> RelatorioEval:
    relatorio = RelatorioEval()
    with tempfile.TemporaryDirectory() as tmp:
        for seed in range(1, cenarios + 1):
            avaliar_cenario(seed, Path(tmp) / f"s{seed}", relatorio)
    return relatorio


# ============================== OCR ==============================


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFD", texto).encode("ascii", "ignore").decode().lower()


def diferencas_fiscais(lida: NotaFiscal, original: NotaFiscal) -> list[str]:
    """Diferenças em campos que mexem com dinheiro ou identificação (texto livre fica fora)."""
    campos = (
        "chave",
        "numero",
        "serie",
        "data_emissao",
        "emitente_cnpj",
        "emitente_uf",
        "destinatario_cnpj",
        "totais",
    )
    difs = [c for c in campos if getattr(lida, c) != getattr(original, c)]
    if len(lida.itens) != len(original.itens):
        return [*difs, "quantidade_de_itens"]
    for a, b in zip(lida.itens, original.itens, strict=True):
        da, db = a.model_dump(exclude={"descricao"}), b.model_dump(exclude={"descricao"})
        difs += [f"item{a.numero}.{k}" for k in da if da[k] != db[k]]
    return difs


def diferencas_texto(lida: NotaFiscal, original: NotaFiscal) -> list[str]:
    """Diferenças em texto descritivo, ignorando acento e maiúscula."""
    difs = []
    if _sem_acento(lida.emitente_nome) != _sem_acento(original.emitente_nome):
        difs.append("emitente_nome")
    for a, b in zip(lida.itens, original.itens, strict=False):
        if _sem_acento(a.descricao) != _sem_acento(b.descricao):
            difs.append(f"item{a.numero}.descricao")
    return difs


@dataclass
class RelatorioOcr:
    notas: int = 0
    identicas: int = 0  # leitura == original em tudo, até acento
    fiscal_correto: int = 0  # todos os campos fiscais certos
    so_acento: int = 0  # fiscal certo; texto difere só em acento/maiúscula
    conferidas: int = 0  # passaram na conferência em código
    chaves_reconstruidas: int = 0
    erradas_detectadas: int = 0  # erro fiscal na leitura, e a conferência pegou
    erradas_aceitas: int = 0  # erro fiscal que passou como conferida (o pior caso)
    exemplos: list[str] = field(default_factory=list)


def avaliar_ocr(ia: ProvedorIA, seed: int = 42, foto: bool = True) -> RelatorioOcr:
    """Lê a DANFE de cada nota do cenário e compara com a nota original."""
    cen = gerar_cenario(seed=seed)
    rel = RelatorioOcr()
    tipo = "foto" if foto else "png"
    for arquivo, nota in cen.notas:
        if arquivo.endswith("-reenvio.xml"):
            continue  # mesma imagem da original
        imagem, mime = gerar_imagem(nota, foto=foto)
        id_chamada = f"seed{seed}-{arquivo[:-4]}-{tipo}-p{PROMPT_VERSAO}"
        leitura = ler_danfe(imagem, mime, ia, id_chamada=id_chamada)
        rel.notas += 1
        rel.conferidas += leitura.conferida
        rel.chaves_reconstruidas += bool(leitura.ajustes)
        if leitura.nota is None:
            rel.erradas_detectadas += 1
            rel.exemplos.append(f"{arquivo}: sem leitura {[p.codigo for p in leitura.problemas]}")
            continue
        rel.identicas += leitura.nota == nota
        fiscais = diferencas_fiscais(leitura.nota, nota)
        chave_original_ok = not conferir_nota(nota)
        if not fiscais:
            rel.fiscal_correto += 1
            # campos fiscais iguais: qualquer diferença restante é só de texto
            rel.so_acento += leitura.nota != nota
            if not chave_original_ok and leitura.conferida:
                rel.erradas_aceitas += 1  # chave inválida de verdade não pode passar
        elif leitura.conferida:
            rel.erradas_aceitas += 1
            rel.exemplos.append(f"{arquivo}: ACEITA com erro em {fiscais}")
        else:
            rel.erradas_detectadas += 1
            codigos_ = [p.codigo for p in leitura.problemas]
            rel.exemplos.append(f"{arquivo}: detectada {fiscais} → {codigos_}")
    return rel
