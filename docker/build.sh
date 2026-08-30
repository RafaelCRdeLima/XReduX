#!/bin/bash
# Constrói a imagem do XREDUX.
#
# O tarball do SAS entra por bind mount de build a partir de vendor/, então ele
# precisa estar lá — o build não o baixa. Se faltar:
#   python tools/install_sas.py --sas   (ou baixe o tgz da ESA para vendor/)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${XREDUX_IMAGE:-xredux:22.1.0}"
TARBALL="${XREDUX_SAS_TARBALL:-vendor/sas_22.1.0-ubuntu24.04.tgz}"

if [[ ! -f "$ROOT/$TARBALL" ]]; then
    echo "Tarball do SAS não encontrado em $ROOT/$TARBALL" >&2
    exit 1
fi

echo "Construindo $IMAGE (leva alguns minutos; ~1 GB de HEASoft é baixado)"
cd "$ROOT"
exec docker build \
    -f docker/Dockerfile \
    --build-arg "SAS_TARBALL=$TARBALL" \
    -t "$IMAGE" \
    "$@" .
