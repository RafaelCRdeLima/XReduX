# syntax=docker/dockerfile:1.7
#
# XREDUX completo: SAS 22.1.0 da ESA, HEASoft 6.36, PySide6 e o pipeline.
#
# O que deliberadamente NÃO entra na imagem:
#
#   * os arquivos de calibração (CCF) — são 2,8 GB, mudam toda semana, e assá-los
#     obrigaria a reconstruir a imagem inteira a cada atualização. Entram por
#     montagem em /ccf;
#   * os dados e produtos das observações — montados em /work, para sobreviverem
#     ao contêiner.
#
# O tarball do SAS entra num estágio descartado, e não na imagem final: assim ele
# não vira uma camada permanente de 1,5 GB. O build é multi-estágio em vez de usar
# `--mount=type=bind` justamente para não exigir BuildKit — o construtor clássico
# do Docker também dá conta.
#
#   docker build -f docker/Dockerfile -t xredux:22.1.0 .

FROM ubuntu:24.04 AS base

ARG DEBIAN_FRONTEND=noninteractive
ARG SAS_TARBALL=vendor/sas_22.1.0-ubuntu24.04.tgz

# Bibliotecas de sistema em dois grupos: as que o SAS espera, e as que o Qt
# precisa para abrir uma janela via X11 do hospedeiro.
#
# libcurl3t64-gnutls é a única dependência de sistema que o SAS 22.1.0 não
# encontra num Ubuntu 24.04 limpo: sem ela nem o sasversion carrega.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bzip2 ca-certificates curl file perl gfortran libgomp1 \
        libncurses6 libreadline8t64 libtinfo6 \
        libcurl3t64-gnutls \
        libx11-6 libx11-xcb1 libxext6 libxrender1 libxi6 libsm6 libice6 \
        libxcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
        libxcb-randr0 libxcb-render0 libxcb-render-util0 libxcb-shape0 \
        libxcb-shm0 libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 \
        libxkbcommon0 libxkbcommon-x11-0 libgl1 libegl1 libopengl0 \
        libdbus-1-3 libfontconfig1 libfreetype6 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# HEASoft, PySide6 e o resto do Python, pelo canal conda do HEASARC
# ---------------------------------------------------------------------------
ENV MAMBA_ROOT_PREFIX=/opt/micromamba \
    ENV_PREFIX=/opt/micromamba/envs/xredux

RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest \
      | tar -xj -C /usr/local bin/micromamba

COPY docker/environment.yml /tmp/environment.yml
RUN micromamba create -y -f /tmp/environment.yml \
    && micromamba clean --all --yes \
    && rm -rf /opt/micromamba/pkgs

# ---------------------------------------------------------------------------
# SAS 22.1.0, num estágio que fica para trás
# ---------------------------------------------------------------------------
FROM base AS sasbuild
ARG SAS_TARBALL
ENV SAS_PARENT=/opt/sas

COPY ${SAS_TARBALL} /tmp/sas.tgz
RUN set -eu; \
    mkdir -p "${SAS_PARENT}"; \
    tar -xzf /tmp/sas.tgz -C "${SAS_PARENT}"; \
    rm -f /tmp/sas.tgz; \
    export HEADAS="${ENV_PREFIX}/heasoft"; \
    . "${HEADAS}/headas-init.sh"; \
    export SAS_PERL=/usr/bin/perl; \
    # O configure_install exige python >= 3.8 no PATH. O headas-init só põe o
    # HEADAS lá, e o Ubuntu base não traz python — sem esta linha ele desiste.
    export PATH="${ENV_PREFIX}/bin:${PATH}"; \
    cd "${SAS_PARENT}"; \
    ./install.sh; \
    ln -s "$(ls -d ${SAS_PARENT}/xmmsas_*)" "${SAS_PARENT}/current"; \
    test -f "${SAS_PARENT}/current/setsas.sh"

# ---------------------------------------------------------------------------
# Imagem final: base + o SAS já instalado, sem o tarball
# ---------------------------------------------------------------------------
FROM base
ENV SAS_PARENT=/opt/sas
COPY --from=sasbuild /opt/sas /opt/sas

WORKDIR /opt/xredux
COPY xredux ./xredux
COPY tools ./tools
COPY tests ./tests
COPY README.md ./
COPY docker/entrypoint.sh /usr/local/bin/xredux-entrypoint

RUN chmod +x /usr/local/bin/xredux-entrypoint \
    && mkdir -p /ccf /work \
    && chmod 0777 /work

# Escritos pelo entrypoint; ficam fora de $HOME porque o contêiner costuma rodar
# com o UID do usuário do hospedeiro, que pode não ter home aqui dentro.
ENV XDG_CONFIG_HOME=/tmp/xredux-config \
    XDG_CACHE_HOME=/tmp/xredux-cache \
    XREDUX_ROOT=/opt/xredux \
    XREDUX_CCF=/ccf \
    XREDUX_WORK=/work \
    XREDUX_PULSARIS=/pulsaris \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/micromamba/envs/xredux/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

VOLUME ["/ccf", "/work"]
ENTRYPOINT ["/usr/local/bin/xredux-entrypoint"]
CMD ["gui"]
