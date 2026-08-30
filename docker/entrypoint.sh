#!/bin/bash
# Ponto de entrada do contêiner do XREDUX.
#
# Escreve as preferências apontando para os caminhos de dentro do contêiner e
# despacha para o subcomando pedido. As preferências são geradas a cada execução
# porque os pontos de montagem podem mudar entre chamadas.
set -euo pipefail

PYTHON="${ENV_PREFIX}/bin/python"
SETTINGS_DIR="${XDG_CONFIG_HOME}/xredux"
SETTINGS="${SETTINGS_DIR}/settings.json"

mkdir -p "${SETTINGS_DIR}" "${XDG_CACHE_HOME}"

# Os CCF são montados, não assados na imagem. Sem eles o cifbuild não roda, e é
# melhor dizer isso agora do que deixar a redução falhar no meio.
ccf_count=$(ls -1 "${XREDUX_CCF}" 2>/dev/null | grep -c '\.CCF$' || true)
if [[ "${ccf_count}" -eq 0 ]]; then
    echo "AVISO: nenhum arquivo .CCF em ${XREDUX_CCF}." >&2
    echo "       Monte o repositório de calibração:  -v /caminho/ccf:/ccf:ro" >&2
    echo "       Sem ele o cifbuild falha e nada é reduzido." >&2
fi

cat > "${SETTINGS}" <<JSON
{
  "language": "${XREDUX_LANGUAGE:-pt_BR}",
  "sas_dir": "${SAS_PARENT}/current",
  "ccf_path": "${XREDUX_CCF}",
  "headas": "${ENV_PREFIX}/heasoft",
  "heasoft_env": "${ENV_PREFIX}",
  "pulsaris_root": "${XREDUX_PULSARIS:-/pulsaris}",
  "work_dir": "${XREDUX_WORK}",
  "max_threads": ${XREDUX_THREADS:-$(nproc)}
}
JSON

cd "${XREDUX_ROOT}"

command="${1:-gui}"
[[ $# -gt 0 ]] && shift || true

case "${command}" in
    gui)
        if [[ -z "${DISPLAY:-}" ]]; then
            echo "ERRO: DISPLAY não está definido; a interface gráfica precisa de X11." >&2
            echo "      Use docker/run.sh, ou rode um subcomando sem janela:" >&2
            echo "        reduce, plot, doctor, parameters, test, shell" >&2
            exit 2
        fi
        exec "${PYTHON}" -m xredux "$@"
        ;;
    reduce)     exec "${PYTHON}" tools/reduce.py "$@" ;;
    plot)       exec "${PYTHON}" tools/plot_timing.py "$@" ;;
    doctor)     exec "${PYTHON}" tools/doctor.py "$@" ;;
    parameters) exec "${PYTHON}" tools/check_parameters.py "$@" ;;
    test)       exec "${PYTHON}" -m unittest discover -s tests "$@" ;;
    python)     exec "${PYTHON}" "$@" ;;
    shell)
        # Shell com SAS e HEASoft já inicializados, para uso manual das tarefas.
        # O rc vai para um arquivo de verdade: com substituição de processo o
        # exec fecharia o descritor antes de o bash filho conseguir lê-lo.
        rcfile="${XDG_CACHE_HOME}/sasrc"
        cat > "${rcfile}" <<'RC'
export HEADAS="${ENV_PREFIX}/heasoft"
source "$HEADAS/headas-init.sh" > /dev/null
export SAS_DIR="${SAS_PARENT}/current" SAS_PERL=/usr/bin/perl
export SAS_CCFPATH="${XREDUX_CCF}"
source "$SAS_DIR/setsas.sh" > /dev/null
export PATH="${ENV_PREFIX}/bin:$PATH"
cd "${XREDUX_WORK}"
sasversion -v 2>/dev/null | head -1
RC
        exec bash --rcfile "${rcfile}" "$@"
        ;;
    help|--help|-h)
        cat <<'USAGE'
XREDUX no contêiner. Subcomandos:

  gui           interface gráfica (exige DISPLAY e o socket X11 montado)
  reduce ...    redução completa sem interface (tools/reduce.py)
  plot ...      figuras de timing (tools/plot_timing.py)
  doctor        diagnóstico do ambiente
  parameters    confere os parâmetros SAS/HEASoft usados pelo código
  test          suíte de testes
  shell         bash com SAS e HEASoft inicializados
  python ...    interpretador do ambiente

Montagens esperadas:
  -v <ccf>:/ccf:ro          arquivos de calibração
  -v <produtos>:/work       dados e produtos das observações
  -v <PULSARIS>:/pulsaris   opcional, para instalar perfis de instrumento
USAGE
        ;;
    *)
        # Qualquer outra coisa é tratada como comando direto, o que mantém o
        # contêiner utilizável para tarefas do SAS avulsas.
        exec "${command}" "$@"
        ;;
esac
