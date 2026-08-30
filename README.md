# XREDUX

Redução de dados de pulsares observados pelo XMM-Newton, com interface gráfica,
do download do ODF até a contagem de fótons por canal.

O programa não reimplementa nada de astronomia de raios X: ele conduz o
**SAS 22.1.0** da ESA e o **HEASoft/FTOOLS/XRONOS**, que são o software
estabelecido para esses dados. O que o XREDUX acrescenta é o encadeamento das
etapas, a interface, o registro do que foi feito e a ponte com o
[PULSARIS](../PULSARIS).

## Por que existe

O PULSARIS simula pontos quentes em estrelas de nêutrons e ajusta dados de
eventos fase-energia por MCMC. Até aqui ele só podia dobrar o modelo através de
respostas *representativas* — o próprio `scripts/build_instrument_profiles.py`
registra a limitação:

> the official canned product supplies redistribution only;
> **observation-specific ARFs require SAS**

O XREDUX gera essa resposta específica da observação. Ao final de uma redução
você tem, para uma observação real:

- a lista de eventos baricentrada, com tempo, canal e energia por fóton;
- os espectros OGIP (fonte, fundo, agrupado) — a contagem por canal;
- a **RMF e a ARF geradas por `rmfgen` e `arfgen` para esta observação**,
  esta região e esta posição no detector;
- um perfil de instrumento do PULSARIS construído a partir dessas respostas.

## Instalação

Pré-requisitos: um ambiente micromamba com HEASoft e PySide6 (aqui,
`pulsaris-heasoft`) e cerca de 6 GB livres em disco.

```bash
python tools/install_sas.py --all     # SAS 22.1.0 + CCF (conjunto "valid") + astroquery
python tools/doctor.py                # diagnóstico completo do ambiente
```

O instalador nunca roda `sudo`. Se faltar alguma biblioteca de sistema, ele
imprime o comando `apt` para você decidir.

## Contêiner

Se preferir não instalar nada no sistema, a imagem traz SAS 22.1.0, HEASoft 6.36,
PySide6 e o pipeline:

```bash
docker/build.sh          # exige vendor/sas_22.1.0-ubuntu24.04.tgz presente
docker/run.sh            # abre a interface gráfica
docker/run.sh doctor     # diagnóstico
docker/run.sh reduce --obsid 0412601301 --target "RX J1856.5-3754" --period 7.055
docker/run.sh shell      # bash com SAS e HEASoft já inicializados
```

Duas coisas ficam **fora** da imagem de propósito:

- os **CCF** (2,8 GB, atualizados toda semana) — montados em `/ccf`; assá-los
  obrigaria a reconstruir a imagem inteira a cada atualização de calibração;
- os **dados e produtos** — montados em `/work`, para sobreviverem ao contêiner.

O `docker/run.sh` monta `external/ccf`, `products/` e, se existir, o repositório
do PULSARIS em `/pulsaris`. Ele também roda o contêiner com o seu UID, senão tudo
em `products/` sairia pertencendo ao root.

O tarball do SAS entra por *bind mount* de build, não por `COPY`: sem isso ele
viraria uma camada de 1,5 GB dentro da imagem final.

A interface gráfica usa o X11 do hospedeiro (socket e cookie montados pelo
`run.sh`). Num servidor sem display, todos os outros subcomandos funcionam.

Notas de construção, aprendidas na marra:

- O build é **multi-estágio** em vez de usar `--mount=type=bind`, que exigiria
  BuildKit — nem toda instalação do Docker traz o plugin `buildx`. O estágio que
  carrega o tarball é descartado, então a imagem final tem 8,7 GB em vez de 10,2.
- O `configure_install` do SAS exige `python` no `PATH`. O `headas-init` só põe o
  HEADAS lá, e o Ubuntu base não traz Python nenhum.
- `libcurl3t64-gnutls` é a única dependência de sistema que falta num Ubuntu 24.04
  limpo. Sem ela nem o `sasversion` carrega.

Verificado dentro do contêiner: `doctor` limpo, 82 testes passando, os 118 pares
tarefa/parâmetro conferidos, a janela abrindo pelo X11 e a análise de
RX J1856.5-3754 reproduzindo **P = 7,055240 s** e fração pulsada de (1,34 ± 0,22)%
— os mesmos dígitos obtidos no hospedeiro.

## Uso

```bash
./xredux.sh
```

Para um ícone na área de trabalho e no menu de aplicativos:

```bash
python tools/install_desktop.py     # --remove desfaz
```

Instala o ícone nos sete tamanhos que o tema procura, escreve a entrada
`.desktop` nos dois lugares e marca a da área de trabalho como confiável — sem
isso o Cinnamon recusa o duplo clique. Lançado pelo ícone não há terminal, então
o `xredux.sh` mostra falhas de ambiente numa janela em vez de perdê-las no
stderr.

A janela apresenta as oito etapas do pipeline na ordem em que dependem umas das
outras. O log de todas as tarefas externas fica visível o tempo todo, e cada
etapa pode ser cancelada.

O botão **Guia** na barra superior abre um passo a passo com as telas reais do
programa e as decisões que cada etapa pede, no idioma selecionado. Ele é gerado
a partir da própria interface, para não descolar dela:

```bash
python tools/build_guide.py --session <ObsID já reduzido> --search "<alvo>"
```

| Etapa | O que roda |
|---|---|
| 1. Aquisição | consulta TAP ao XSA, download do ODF pela interface AIO |
| 2. Calibração | `cifbuild`, `odfingest` |
| 3. Processamento | `epproc`, `emproc`, `rgsproc`, `omichain`/`omfchain` |
| 4. Filtragem | `evselect` (fundo de alta energia), `tabgtigen`, `evselect` (GTI) |
| 5. Regiões | `evselect` (imagem), `eregionanalyse`, `epatplot` |
| 6. Timing | `barycen`, `epiclccorr`, `powspec`, `efsearch`, `efold`, Z²ₙ e teste H |
| 7. Espectros | `evselect` (espectro), `backscale`, `rmfgen`, `arfgen`, `ftgrouppha`, `phasecalc` |
| 8. Exportação | CSV de eventos, tabela de fundo, `nh`, e perfil de instrumento |

### Busca de período

A busca é feita em duas passadas, por um motivo concreto. `powspec` e `efsearch`
trabalham sobre a curva de luz binada e varrem um intervalo amplo de forma
barata. Depois, Z²ₙ e o teste H entram sobre os **tempos de chegada não
binados**, numa grade estreita ao redor do candidato: são mais sensíveis quando
a fração pulsada é baixa e o perfil quase senoidal. RX J1856.5-3754, o alvo de
validação, tem fração pulsada de cerca de 1% — no limite do que o *epoch folding*
binado detecta.

### Exportação para o PULSARIS

O CSV segue o formato lido por `scripts/mcmc_fit.py` e `scripts/heasoft_fold.py`:
bloco de metadados em `#` seguido de `TIME,PI,DETECTED_ENERGY_KEV`. Duas
conversões merecem atenção:

- o `PI` de um evento do EPIC é a energia calibrada **em eV**;
- o `PI` que o PULSARIS espera é o **índice de canal** da resposta que acompanha
  o dado. O mapeamento sai da extensão `EBOUNDS` da RMF desta observação, o que
  mantém canal e resposta coerentes.

Junto do CSV de eventos saem mais duas coisas que o ajuste de dados reais exige:

- **`*_background.csv`** — a taxa de fundo por keV, escalada para a região da
  fonte pela razão dos `BACKSCAL` (receita padrão OGIP). Sem ela o PULSARIS
  atribui à estrela todo evento que caiu na região de extração.
- **`nh_galactic_upper_1e22`** no cabeçalho — a coluna de hidrogênio na linha de
  visada, obtida da ferramenta **`nh` do HEASoft** (levantamento HI4PI). É a
  coluna galáctica *integrada*, portanto **limite superior** para uma fonte
  dentro da Galáxia: para RX J1856.5-3754, a ~123 pc, ela dá 6,8×10²⁰ cm⁻²
  enquanto o valor ajustado na literatura fica perto de 1×10²⁰.

A instalação no repositório do PULSARIS é sempre um ato explícito: o botão
mostra exatamente o que será escrito e só age depois da confirmação. O
`manifest.json` é copiado para `.json.bak` antes de qualquer alteração.

O servidor do PULSARIS limita uploads a 100 MB. Uma observação longa do EPIC-pn
passa disso, então a exportação corta em banda de energia e, se ainda exceder,
oferece decimação com semente registrada no cabeçalho — nunca silenciosamente.

## Validação em RX J1856.5-3754

O pipeline foi validado de ponta a ponta contra um resultado publicado, sobre a
observação **0412601301** (EPIC-pn, Small Window, THIN1, 82,5 ks; revolução 2062,
março de 2011).

| Grandeza | XREDUX | Literatura |
|---|---|---|
| Período | **7,055240 s** (Z²ₙ) / 7,055241 s (`efsearch`) | 7,055 s |
| Significância | H = 38,2 · p ≈ 2,3×10⁻⁷ | — |
| Fração pulsada (0,15–1,2 keV) | **(1,34 ± 0,22)%** | ~1,2% |
| Eventos da fonte | 404 892 em 55,9 ks de tempo vivo | — |

Recuperar 7,055 s é o teste decisivo da correção baricêntrica: sem `barycen` o
movimento orbital da Terra espalha o sinal e o pico desaparece. As duas rotas de
busca — `efsearch` sobre a curva binada e Z²ₙ sobre os tempos não binados —
concordam em 10⁻⁶ s, cada uma partindo de um caminho de código diferente.

A figura fica em `products/RX J1856.5-3754/0412601301/0412601301_timing.png`:

```bash
python tools/reduce.py --obsid 0412601301 --target "RX J1856.5-3754" \
    --period 7.055 --band 150 1200 --export
python tools/plot_timing.py --obsid 0412601301 --period 7.055
```

### Fração pulsada: qual estimador

Há três números possíveis, e eles não medem a mesma coisa.

A razão `(max-min)/(max+min)` do histograma dobrado é **enviesada para cima**:
com 16 bins, a diferença entre máximo e mínimo de puro ruído de Poisson já é da
ordem de alguns por cento. Em RX J1856 ela devolve 1,77% contra 1,34% dos
estimadores de Fourier.

A amplitude do **fundamental**, `a = sqrt(2(Z²₁ - 2)/N)`, é o número certo para
um perfil quase senoidal — e só para esse caso.

Quando o teste H escolhe **m > 1**, o perfil tem estrutura além do fundamental e
o programa passa a reportar também a **fração RMS**,
`sqrt((Z²ₘ - 2m)/N)`, que soma todos os harmônicos. A diferença não é sutil: em
RBS 1223, cujo perfil tem dois picos por rotação, o fundamental dá 5,13% e o RMS
dá **11,43%** — Z²₁ = 85 contra Z²₃ = 830, ou seja, quase 90% da potência está
nos harmônicos que o fundamental ignora.

## Busca de período sem candidato prévio

O `efsearch` varre uma vizinhança de um período que se informa — serve para
refinar, não para achar. **Procurar período (powspec)** faz a busca ampla, e
precisa desviar de três armadilhas, todas medidas nas observações deste
repositório:

| armadilha | o que acontece | como se resolve |
|---|---|---|
| harmônico | num perfil de dois picos, o maior pico do periodograma é P/2 — na 0844140101, 5,155 s com potência 93 contra 10,317 s com 16 | subir a escada enquanto 2P ou 3P ainda tiverem fundamental |
| subharmônico | dobrar em 3P repete o perfil três vezes e o teste H acusa: H = 45 em 30,95 s | exigir potência no fundamental Z²₁, que num subharmônico é ruído (2,0 contra 39,5) |
| ruído vermelho | a potência cresce para frequências baixas; Z²₁ = 399 em 443 s sem pulsação nenhuma | comparar Z²₁ com a **vizinhança**, não com um limiar fixo — ali o contraste é 1,6, contra 22 no período verdadeiro |

O limiar de contraste não é escolhido a dedo: sob ruído branco a mediana de Z²₁
é 1,38, então exigir contraste 10 diz o mesmo que o limiar absoluto de 13,8 —
só que medindo o ruído onde ele está.

São 30 picos do periodograma, e não os cinco mais altos: na 0412601301, cuja
fração pulsada é 1,3%, o pico verdadeiro fica em 25º lugar entre 65 mil
frequências.

Verificado às cegas nas duas fontes: 10,31089 s para a RBS 1223 (literatura
10,31 s) e 7,05523 s para a RX J1856.5-3754 (Tiengo & Mereghetti 2007:
7,055 s).

## Arquivamento

As observações ficam agrupadas pela fonte, não soltas pelo ObsID:

```
products/
  RXJ1308.6+2127/
    source.json          nome legível, coordenadas e apelidos da fonte
    0163560101/
    0844140101/
  RXJ1856.5-3754/
    source.json
    0412601301/
```

**O caminho só usa letras, dígitos, `.`, `-` e `_`.** O SAS recusa pontuação em
caminhos, e cada componente falha de um jeito diferente — todos dizendo apenas
que o arquivo não existe:

| caractere | quem quebra | como |
|---|---|---|
| espaço | `odfingest` | recebe `RX J1308.6` como `RXJ1308.6` |
| `+` | `epproc::hkgtigen` | corta em `RXJ1308.6`, lendo o resto como extensão |
| `[` `]` | CFITSIO | toma por seletor de extensão |
| `:` | leitor do SAS | não resolve o caminho |

Sondar uma tarefa só não basta: o `+` passa no `dshead` e no `ftlist`, e quebra
no `hkgtigen`. Por isso a regra é conservadora, e o sinal da declinação vira
separador — `RX J1308.6+2127` fica `RXJ1308.6_2127`. O nome legível vive no
`source.json`. O `tools/doctor.py` verifica as pastas e o diretório de trabalho.

**Mover uma observação exige corrigir o sumário.** O `odfingest` grava o caminho
absoluto do ODF dentro do `*SUM.SAS`; depois de mover, ele aponta para o nada e
o `epproc` falha reclamando de um arquivo de eventos, sem mencionar o sumário.
`Pipeline.restore()` conserta isso sozinho ao reabrir a observação.

O agrupamento é **por posição**, não por nome. A mesma fonte chega como
`RBS1223` no sumário do ODF e como `RX J1308.6+2127` na busca por nome; casar
por texto criaria duas pastas para uma fonte só. Duas observações a menos de 3′
uma da outra vão para a mesma pasta, e os nomes alternativos ficam registrados
como apelidos no `source.json`.

O nome canônico registrado ali é o que identifica tudo o que sai da redução:
título da janela, cabeçalho de cada gráfico, nome dos arquivos exportados e
metadados do CSV do PULSARIS. É o nome do arquivo que vale, não o que a sessão
guardou no dia — a sessão pode ter registrado um apelido.

Como a designação que encabeça uma figura é escolha editorial (a mesma fonte é
`RBS 1223` ou `RX J1308.6+2127` conforme a revista), **Renomear fonte…** no
seletor troca o nome e guarda o anterior como apelido. A posição não muda, então
o agrupamento continua funcionando.

Na interface, **Usar ODF local** lista o que já está no disco — fontes,
observações e quais têm ODF extraído — em vez de abrir um navegador de arquivos.

Observações do arranjo antigo, soltas em `products/<ObsID>/`, continuam sendo
encontradas. Para reorganizá-las:

```bash
python tools/organise_archive.py            # mostra o plano
python tools/organise_archive.py --apply    # move
```

## Reprodutibilidade

Cada observação tem seu diretório em `products/<fonte>/<ObsID>/` com:

- `session.json` — estado de cada etapa, parâmetros, produtos, códigos de saída;
- `reproduce.sh` — todos os comandos executados, em ordem, pronto para rodar.

Uma redução interrompida é retomada de onde parou; `epproc` não se refaz à toa.

## Testes

```bash
cd tests && python -m unittest discover
```

A maior parte não precisa do SAS. Cobrem as estatísticas de timing (incluindo a
recuperação de um período injetado), a exportação — lida de volta pelos próprios
leitores do PULSARIS — a construção do perfil de instrumento, a sessão e o
executor de processos. Os que dependem do repositório do PULSARIS se pulam
sozinhos quando ele não está presente.

`test_integration_sas.py` roda o SAS de verdade sobre uma lista de eventos
sintética: curva de fundo, GTI, filtragem, imagem e espectro. Verifica, entre
outras coisas, que a soma das contagens por canal bate exatamente com o número
de eventos selecionados. Também se pula sozinho sem SAS.

Com o SAS instalado, vale rodar também:

```bash
python tools/check_parameters.py
```

Ele extrai da árvore sintática de `xredux/tasks` todo par tarefa/parâmetro que o
programa usa e confronta com o que a instalação local declara. Errar o nome de um
parâmetro do SAS nem sempre dá erro: algumas tarefas ignoram o que não reconhecem
e seguem com o padrão, produzindo um resultado plausível e errado.

## Notas do ambiente

**Fontes.** O `libfontconfig` que acompanha o PySide6 deste ambiente conda
segfaulta ao ler o cache de fontes do sistema (versões incompatíveis), no caminho
de *fallback* — ou seja, diante de qualquer caractere ausente na fonte primária.
`xredux/gui/fonts.py` resolve apontando o fontconfig para uma configuração e um
cache próprios, antes de o Qt carregar. Sem isso o programa cairia diante de um
nome de alvo ou caminho com um caractere fora do comum.

**Versões.** O SAS 22.1.0 foi compilado contra o HEASoft 6.33.2 e o ambiente aqui
tem o 6.36. Verificado nesta máquina: a combinação funciona — `sasversion`,
`evselect` e as demais 15 tarefas respondem normalmente. Se num outro ambiente
`python tools/doctor.py` acusar falha em `sasversion`, crie um ambiente dedicado
com `heasoft=6.33.2` do canal
`https://heasarc.gsfc.nasa.gov/FTP/software/conda` e aponte `heasoft_env` nas
preferências, sem mexer no ambiente do PULSARIS.

## Estrutura

```
xredux/
  config.py env.py runner.py session.py i18n.py pipeline.py
  tasks/    acquisition calibration epic rgs om filtering regions timing spectra
  export/   pulsaris profile
  gui/      main_window app fonts · pages/ · widgets/
  locales/  pt_BR.json en.json
  guide/    guide.pt_BR.html guide.en.html · img/
tools/      install_sas.py doctor.py check_parameters.py
            reduce.py plot_timing.py build_guide.py
            organise_archive.py
tests/
products/<fonte>/<ObsID>/
```

A interface roda no ambiente com PySide6 e astropy; cada tarefa do SAS é um
**subprocesso** com ambiente montado por `xredux/env.py`, incluindo um `PFILES`
próprio por sessão. Isso desacopla as versões de Python e HEASoft do SAS das da
interface, e impede que execuções simultâneas disputem os arquivos de parâmetros
do HEASoft — uma disputa que produz resultados silenciosamente errados.

## O Python das tarefas do SAS

Metade das tarefas do SAS 22.1 termina chamando um script auxiliar com shebang
`#!/usr/bin/env python`. Se esse `python` for o do sistema, a tarefa faz todo o
trabalho pesado e morre no fim com `ModuleNotFoundError` — um erro que não se
parece nem um pouco com um problema de ambiente. Por isso `xredux/env.py` põe o
interpretador do XREDUX no `PATH` das tarefas, atrás só dos binários do SAS e do
HEASoft, e define `PYTHONNOUSERSITE=1` para que um pacote em `~/.local` não passe
na frente do ambiente.

`sas_python_packages.txt` pede `PyQt5`, que **não** é instalado: a interface roda
em PySide6 no mesmo ambiente, e dois bindings Qt no mesmo processo disputam
plugins de plataforma. `pyds9`, `notebook` e `pytest` servem a ferramentas
interativas que o pipeline não usa.

O `epatplot` recebe o `plotfile` como nome **relativo** e com extensão `.pdf`:
ele perde a barra inicial ao repassar o caminho ao script que desenha, e o
auxiliar do SAS 22.1 só produz PDF.

## Referências

- SAS: <https://www.cosmos.esa.int/web/xmm-newton/sas>
- Threads de análise da ESA: <https://www.cosmos.esa.int/web/xmm-newton/sas-threads>
- Buccheri et al. (1983), A&A 128, 245 — estatística Z²ₙ
- de Jager et al. (1989), A&A 221, 180 — teste H
- Tiengo & Mereghetti (2007), ApJ 657, L101 — pulsação de 7,055 s em RX J1856.5-3754
- Kaastra & Bleeker (2016), A&A 587, A151 — agrupamento ótimo de espectros
