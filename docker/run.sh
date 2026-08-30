#!/bin/bash
# Roda o XREDUX no contêiner, com as montagens e o X11 já resolvidos.
#
#   docker/run.sh                       # interface gráfica
#   docker/run.sh doctor                # diagnóstico
#   docker/run.sh reduce --obsid ...     # redução sem interface
#   docker/run.sh shell                 # bash com SAS e HEASoft prontos
#
# Variáveis reconhecidas: XREDUX_IMAGE, XREDUX_CCF_DIR, XREDUX_WORK_DIR,
# XREDUX_PULSARIS_DIR.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${XREDUX_IMAGE:-xredux:22.1.0}"
CCF_DIR="${XREDUX_CCF_DIR:-$ROOT/external/ccf}"
WORK_DIR="${XREDUX_WORK_DIR:-$ROOT/products}"
PULSARIS_DIR="${XREDUX_PULSARIS_DIR:-$HOME/Codes/PULSARIS}"

if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
    echo "Imagem '$IMAGE' não encontrada. Construa antes:" >&2
    echo "  docker/build.sh" >&2
    exit 1
fi

mkdir -p "$WORK_DIR"
[[ -d "$CCF_DIR" ]] || echo "AVISO: $CCF_DIR não existe; o cifbuild vai falhar." >&2

# Rodar com o UID do hospedeiro mantém os produtos graváveis e com o dono certo
# do lado de fora — sem isso tudo em products/ sai pertencendo ao root.
options=(--rm --init
         --user "$(id -u):$(id -g)"
         -v "$CCF_DIR:/ccf:ro"
         -v "$WORK_DIR:/work"
         -e "XREDUX_THREADS=$(nproc)")

[[ -d "$PULSARIS_DIR" ]] && options+=(-v "$PULSARIS_DIR:/pulsaris")
[[ -t 0 ]] && options+=(-it)

# O X11 é montado sempre que houver display, e não só no subcomando `gui`:
# qualquer coisa rodada com `python` ou `shell` pode querer abrir uma janela.
command="${1:-gui}"
if [[ -n "${DISPLAY:-}" ]]; then
    options+=(-e "DISPLAY=$DISPLAY" -v /tmp/.X11-unix:/tmp/.X11-unix:ro)
    # O X do hospedeiro controla o acesso por cookie; sem ele a janela é
    # recusada mesmo com o socket montado.
    for cookie in "${XAUTHORITY:-}" "$HOME/.Xauthority"; do
        if [[ -n "$cookie" && -f "$cookie" ]]; then
            options+=(-v "$cookie:/tmp/.Xauthority:ro" -e XAUTHORITY=/tmp/.Xauthority)
            break
        fi
    done
elif [[ "$command" == "gui" ]]; then
    echo "DISPLAY não está definido: não há como abrir a janela." >&2
    echo "Num terminal gráfico isso costuma bastar:  export DISPLAY=:0" >&2
    exit 2
fi

exec docker run "${options[@]}" "$IMAGE" "$@"
