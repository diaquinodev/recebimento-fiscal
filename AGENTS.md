# Regras para agentes neste repositório

- Conta, tolerância e decisão de bloqueio ficam em código determinístico (`match.py`), nunca
  na IA. A IA só lê imagem (DANFE), investiga com ferramentas e redige.
- Dinheiro e quantidade são `Decimal`. Nada de `float` em valor.
- XML de terceiros é lido só pelo parser seguro de `nfe_xml.py` (sem entidades, sem rede).
- Dados são fictícios. Nunca commitar nota, CNPJ ou pedido de empresa real.
- Antes de abrir PR: `ruff check . && ruff format --check . && mypy src && pytest -q`.
