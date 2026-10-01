"""Eval do motor: gera N cenários, processa todas as notas e compara com o gabarito.

Mede o que importa para o negócio:
- detecção: das notas que deveriam ser barradas, quantas foram (status e motivo certos);
- falso bloqueio: notas boas retidas à toa (custa relacionamento com fornecedor).
"""

from __future__ import annotations

import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from recebimento.erp import Erp, carregar_erp_json, criar_engine, sessao
from recebimento.gerador import gerar_cenario, salvar_cenario
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
