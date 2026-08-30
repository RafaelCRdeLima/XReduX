#!/bin/bash
# Inicia o XREDUX no ambiente que tem PySide6, astropy e HEASoft.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="${XREDUX_ENV:-pulsaris-heasoft}"
PYTHON="$HOME/micromamba/envs/$ENV_NAME/bin/python"
# Lançado pelo ícone não há terminal, e uma mensagem em stderr desaparece.
falhar() {
    echo "$1" >&2
    if [[ ! -t 2 ]] && command -v zenity > /dev/null; then
        zenity --error --title="XREDUX" --width=460 --text="$1" 2>/dev/null || true
    fi
    exit 1
}

if [[ ! -x "$PYTHON" ]]; then
    falhar "Ambiente '$ENV_NAME' não encontrado em:
$PYTHON

Defina XREDUX_ENV com o nome correto do ambiente micromamba, ou rode
  python tools/doctor.py
para um diagnóstico completo."
fi
cd "$ROOT"
exec "$PYTHON" -m xredux "$@"
