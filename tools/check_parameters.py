#!/usr/bin/env python3
"""Confere os parâmetros de tarefas SAS e HEASoft usados pelo código.

Errar o nome de um parâmetro do SAS costuma render um erro claro, mas nem sempre:
algumas tarefas ignoram o que não reconhecem e seguem com o valor padrão, o que
produz um resultado plausível e errado. Este script extrai, direto da árvore
sintática dos módulos em ``xredux/tasks``, todo par ``tarefa``/``parâmetro`` que o
programa usa, e confronta com o que a instalação local declara.

    python tools/check_parameters.py            # relatório completo
    python tools/check_parameters.py --quiet     # só o que estiver errado

Exige SAS e HEASoft instalados; sem eles não há contra o que conferir.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xredux import env as sas_env  # noqa: E402
from xredux.config import Settings  # noqa: E402

GREEN, YELLOW, RED, BOLD, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"

SOURCES = sorted((ROOT / "xredux" / "tasks").glob("*.py")) + [ROOT / "xredux" / "pipeline.py"]

#: Parâmetros aceitos por qualquer tarefa, que não aparecem nos arquivos .par.
UNIVERSAL = {"-V", "-v", "--verbosity", "clobber", "mode", "chatter", "history"}


# ---------------------------------------------------------------------------
# Extração do que o código usa
# ---------------------------------------------------------------------------

def _dict_keys(node: ast.AST, variables: dict[str, set[str]] | None = None) -> set[str]:
    """Chaves literais de um dicionário, seguindo o ``**variavel`` quando dá.

    Várias tarefas montam os parâmetros num dicionário local e o desdobram na
    chamada; ignorar esse caso deixaria justamente ``evselect`` e ``phasecalc``
    quase sem cobertura.
    """
    if isinstance(node, ast.Name) and variables is not None:
        # A chamada recebe um dicionário montado antes, como em epiclccorr.
        return set(variables.get(node.id, set()))
    if not isinstance(node, ast.Dict):
        return set()
    keys: set[str] = set()
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
        elif key is None and variables is not None and isinstance(value, ast.Name):
            keys |= variables.get(value.id, set())
    return keys


def _dict_variables(tree: ast.AST) -> dict[str, set[str]]:
    """Chaves dos dicionários literais atribuídos a nomes simples no módulo."""
    variables: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        value = None
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if not isinstance(value, ast.Dict):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                variables.setdefault(target.id, set()).update(_dict_keys(value))

    # Também conta o que entra por ``parametros.update({...})``, forma usada
    # quando parte dos parâmetros só existe sob condição (rgsproc, por exemplo).
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update" and node.args
                and isinstance(node.func.value, ast.Name)):
            variables.setdefault(node.func.value.id, set()).update(
                _dict_keys(node.args[0]))
    return variables


def _parameter_from_string(node: ast.AST) -> str | None:
    """Nome do parâmetro em elementos como ``"dper=..."`` ou ``f"dper={x}"``."""
    text = None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            text = first.value
    if not text or "=" not in text:
        return None
    name = text.split("=", 1)[0].strip()
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) else None


def collect_usage(paths: list[Path]) -> dict[str, set[str]]:
    """Mapa ``tarefa -> parâmetros`` extraído do código."""
    usage: dict[str, set[str]] = {}

    def note(task: str, parameters: set[str]) -> None:
        usage.setdefault(task, set()).update(parameters)

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for scope, variables in _scopes(tree):
            _collect_scope(scope, variables, note)
    return usage


def _scopes(tree: ast.AST):
    """Pares ``(escopo, variáveis)``, um por função de nível superior.

    O mapeamento de variáveis precisa ser por função, não por módulo: em
    ``tasks/timing.py`` tanto ``barycenter`` quanto ``correct_light_curve``
    chamam seu dicionário local de ``parameters``, e um mapa único trocaria os
    parâmetros de ``barycen`` pelos de ``epiclccorr``.

    Só funções são varridas. Toda chamada de tarefa do XREDUX está dentro de
    uma; uma chamada solta no corpo do módulo passaria despercebida.
    """
    nested = {id(child) for node in ast.walk(tree)
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
              for child in ast.walk(node)
              if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
              and child is not node}

    outer = [node for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and id(node) not in nested]
    return [(node, _dict_variables(node)) for node in outer]


def _collect_scope(scope: ast.AST, variables: dict[str, set[str]], note) -> None:
    """Registra as chamadas de tarefa encontradas dentro de um escopo."""
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        attribute = node.func.attr if isinstance(node.func, ast.Attribute) else \
            (node.func.id if isinstance(node.func, ast.Name) else "")

        # context.sas(step, "tarefa", {...})
        if attribute == "sas" and len(node.args) >= 2:
            task = node.args[1]
            if isinstance(task, ast.Constant) and isinstance(task.value, str):
                keys = (_dict_keys(node.args[2], variables)
                        if len(node.args) > 2 else set())
                for keyword in node.keywords:
                    if keyword.arg == "parameters":
                        keys |= _dict_keys(keyword.value, variables)
                note(task.value, keys)

        # sas_command("tarefa", {...})
        elif attribute == "sas_command" and node.args:
            task = node.args[0]
            if isinstance(task, ast.Constant) and isinstance(task.value, str):
                note(task.value, _dict_keys(node.args[1], variables)
                     if len(node.args) > 1 else set())

        # context.check(step, ["tarefa", "par=valor", ...])
        elif attribute in {"check", "run"} and len(node.args) >= 2:
            command = node.args[1]
            if not isinstance(command, ast.List) or not command.elts:
                continue
            head = command.elts[0]
            if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
                continue
            parameters = {name for name in
                          (_parameter_from_string(element)
                           for element in command.elts[1:]) if name}
            note(head.value, parameters)


# ---------------------------------------------------------------------------
# O que a instalação declara
# ---------------------------------------------------------------------------

#: HEASoft usa o formato IRAF: ``nome,tipo,modo,padrão,...``.
_PAR_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*,")
#: O SAS usa XML: ``<PARAM id="nome" type="..." ...>``.
_PAR_XML = re.compile(r"<PARAM\s+id\s*=\s*[\"']([A-Za-z][A-Za-z0-9_]*)[\"']",
                      re.IGNORECASE)


def parameters_from_par_file(path: Path) -> set[str]:
    """Nomes declarados num arquivo de parâmetros.

    Aceita os dois formatos em jogo: o XML do SAS e o IRAF do HEASoft.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()

    names = set(_PAR_XML.findall(text))
    if names:
        return names
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        match = _PAR_LINE.match(line)
        if match:
            names.add(match.group(1))
    return names


def find_par_file(task: str, environment) -> Path | None:
    """Procura o arquivo de parâmetros da tarefa, no SAS ou no HEASoft."""
    candidates = [
        Path(environment.sas_dir) / "config" / f"{task}.par",
        Path(environment.sas_dir) / "config" / task / f"{task}.par",
        Path(environment.headas) / "syspfiles" / f"{task}.par",
    ]
    candidates += list(Path(environment.sas_dir).glob(f"config/**/{task}.par"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


#: Na ajuda do SAS cada parâmetro aparece como ``  nome -- tipo,...``.
_HELP_PARAMETER = re.compile(
    r"^\s{0,8}(?:--)?([a-z][a-zA-Z0-9_]{2,})\s+--\s|^\s{0,8}([a-z][a-zA-Z0-9_]{2,})\s{2,}",
    re.MULTILINE)


def parameters_from_help(task: str, environment) -> set[str]:
    """Recorre à ajuda da tarefa quando não há arquivo de parâmetros."""
    for flag in ("--help", "-h"):
        try:
            completed = subprocess.run([task, flag], env=environment.variables,
                                       capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return set()
        text = completed.stdout + completed.stderr
        names = {first or second
                 for first, second in _HELP_PARAMETER.findall(text)} - {""}
        if len(names) > 3:
            return names
    return set()


def known_parameters(task: str, environment) -> tuple[set[str], str]:
    par_file = find_par_file(task, environment)
    if par_file is not None:
        names = parameters_from_par_file(par_file)
        if names:
            return names, par_file.name
    return parameters_from_help(task, environment), "--help"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quiet", action="store_true",
                        help="mostra só tarefas com parâmetro desconhecido")
    arguments = parser.parse_args()

    settings = Settings.load()
    try:
        environment = sas_env.build(settings)
    except sas_env.EnvironmentError_ as error:
        print(f"{RED}{error}{RESET}")
        return 2

    usage = collect_usage(SOURCES)
    problems = 0
    unverified = 0

    print(f"{BOLD}Parâmetros usados pelo XREDUX, conferidos contra a instalação local{RESET}")
    for task in sorted(usage):
        used = usage[task] - UNIVERSAL
        declared, source = known_parameters(task, environment)
        if not declared:
            unverified += 1
            if not arguments.quiet:
                print(f"  {YELLOW}?{RESET} {task}: não foi possível ler os parâmetros")
            continue

        unknown = sorted(name for name in used if name not in declared)
        if unknown:
            problems += 1
            print(f"  {RED}✗{RESET} {task} ({source}): desconhecido(s) "
                  f"{', '.join(unknown)}")
        elif not arguments.quiet:
            print(f"  {GREEN}✓{RESET} {task} ({source}): {len(used)} parâmetro(s)")

    print()
    if problems:
        print(f"{RED}{BOLD}{problems} tarefa(s) com parâmetro desconhecido.{RESET}")
        return 1
    if unverified:
        print(f"{YELLOW}Todos os parâmetros reconhecidos; "
              f"{unverified} tarefa(s) não puderam ser conferidas.{RESET}")
        return 0
    print(f"{GREEN}{BOLD}Todos os parâmetros conferem.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
