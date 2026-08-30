"""Perfil de instrumento do PULSARIS a partir das respostas reais da observação.

O PULSARIS já sabe ler ARF e RMF OGIP: ``scripts/build_instrument_profiles.py``
tem ``read_arf``, ``read_rmf_response``, ``response_moments`` e
``write_sparse_response``, e é esse código que se reaproveita aqui — reimplementar
o leitor de FITS deles daria dois formatos para manter em sincronia.

O que muda é a origem dos dados. Hoje o perfil ``xmm_epn_timing`` do PULSARIS usa
uma RMF enlatada e um envelope de área efetiva escrito à mão
(``xmm_area``, no ``main()`` daquele script), porque — nas palavras do próprio
comentário — *observation-specific ARFs require SAS*. Com o SAS no pipeline,
passa-se a ARF verdadeira, gerada por ``arfgen`` para esta observação, esta
região e esta posição no detector.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

MANIFEST_NAME = "manifest.json"
#: Mesma amostragem usada pelo PULSARIS: preserva as bordas da resposta barato.
PROFILE_ROWS = 256


class ProfileError(RuntimeError):
    """Falha ao construir ou instalar um perfil de instrumento."""


def load_pulsaris_builder(pulsaris_root: Path) -> ModuleType:
    """Importa ``build_instrument_profiles.py`` do PULSARIS como módulo."""
    script = Path(pulsaris_root) / "scripts" / "build_instrument_profiles.py"
    if not script.is_file():
        raise ProfileError(f"script do PULSARIS não encontrado: {script}")
    spec = importlib.util.spec_from_file_location("pulsaris_build_profiles", script)
    if spec is None or spec.loader is None:
        raise ProfileError(f"não foi possível carregar {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # o script é de terceiros; qualquer falha é fatal aqui
        raise ProfileError(f"falha ao carregar {script.name}: {error}") from error
    return module


def normalize_rmf(rmf: Path, destination: Path) -> Path:
    """Garante uma RMF com extensão ``MATRIX``, como o leitor do PULSARIS exige.

    O ``rmfgen`` do SAS pode nomear a extensão ``SPECRESP MATRIX``; o
    ``read_rmf_response`` do PULSARIS procura exatamente por ``MATRIX`` e falharia.
    A renomeação é feita numa cópia, deixando o produto original do SAS intacto
    para o XSPEC e para o registro da redução.
    """
    from astropy.io import fits

    with fits.open(rmf, memmap=False) as hdus:
        names = [str(hdu.header.get("EXTNAME", "")).strip().upper() for hdu in hdus]
        if "MATRIX" in names and "EBOUNDS" in names:
            if destination.resolve() != rmf.resolve():
                shutil.copy2(rmf, destination)
            return destination
        if "EBOUNDS" not in names:
            raise ProfileError(f"{rmf.name} não tem extensão EBOUNDS")

        renamed = False
        for hdu in hdus:
            current = str(hdu.header.get("EXTNAME", "")).strip().upper()
            if current in {"SPECRESP MATRIX", "SPECRESPMATRIX"}:
                hdu.header["EXTNAME"] = "MATRIX"
                renamed = True
        if not renamed:
            raise ProfileError(
                f"{rmf.name} não tem extensão MATRIX nem SPECRESP MATRIX")
        destination.parent.mkdir(parents=True, exist_ok=True)
        hdus.writeto(destination, overwrite=True)
    return destination


@dataclass
class ProfileBundle:
    """Um perfil pronto para ser instalado no PULSARIS."""

    identifier: str
    entry: dict
    profile_csv: Path
    response_bin: Path
    arf: Path | None = None
    rmf: Path | None = None
    warnings: list[str] = field(default_factory=list)


def build(pulsaris_root: Path, output_dir: Path, identifier: str, label: str,
          instrument: str, arf: Path | None, rmf: Path,
          energy_range_kev: tuple[float, float], time_resolution_us: float,
          dead_time_us: float = 0.0, calibration: str = "",
          source_url: str = "https://www.cosmos.esa.int/web/xmm-newton") -> ProfileBundle:
    """Constrói o par ``.csv`` + ``.rmfbin`` do perfil a partir de ARF e RMF reais."""
    builder = load_pulsaris_builder(pulsaris_root)
    output_dir.mkdir(parents=True, exist_ok=True)

    prepared_rmf = normalize_rmf(rmf, output_dir / f"{identifier}_matrix.rmf")

    warnings: list[str] = []
    if arf is not None and Path(arf).is_file():
        area = builder.read_arf(Path(arf))
    else:
        area = [(energy_range_kev[0], 0.0), (energy_range_kev[1], 0.0)]
        warnings.append(
            "sem ARF: a área efetiva do perfil fica zerada e o ajuste do PULSARIS "
            "não terá normalização absoluta"
        )

    response = builder.read_rmf_response(prepared_rmf)
    moments = builder.response_moments(response)

    profile_csv = output_dir / f"{identifier}.csv"
    stride = max(1, len(moments) // PROFILE_ROWS)
    selected = moments[::stride]
    if selected and selected[-1] != moments[-1]:
        selected.append(moments[-1])
    with profile_csv.open("w", newline="") as stream:
        stream.write("# energy_keV,area_cm2,rmf_mean_keV,rmf_sigma_keV,rmf_efficiency\n")
        writer = csv.writer(stream)
        for energy, mean, sigma, efficiency in selected:
            writer.writerow((f"{energy:.9g}", f"{builder.interpolate(area, energy):.9g}",
                             f"{mean:.9g}", f"{sigma:.9g}", f"{efficiency:.9g}"))

    response_bin = output_dir / f"{identifier}.rmfbin"
    builder.write_sparse_response(response_bin, area, response)

    entry = {
        "id": identifier,
        "label": label,
        "instrument": instrument,
        "energy_min_keV": float(energy_range_kev[0]),
        "energy_max_keV": float(energy_range_kev[1]),
        "time_resolution_us": float(time_resolution_us),
        "dead_time_us": float(dead_time_us),
        "calibration": calibration or "ARF e RMF gerados pelo SAS para esta observação.",
        "source_url": source_url,
        "profile_file": f"instrument_data/profiles/{profile_csv.name}",
        "response_file": f"instrument_data/profiles/{response_bin.name}",
        "redistribution": "full_sparse_ogip_rmf",
        "raw_files": [],
    }
    for path in (arf, rmf):
        if path is not None and Path(path).is_file():
            entry["raw_files"].append({
                "path": f"instrument_data/raw/xmm_newton/{Path(path).name}",
                "download_url": source_url,
                "sha256": builder.sha256(Path(path)),
            })

    return ProfileBundle(identifier=identifier, entry=entry, profile_csv=profile_csv,
                         response_bin=response_bin, arf=Path(arf) if arf else None,
                         rmf=Path(rmf), warnings=warnings)


def preview_install(pulsaris_root: Path, bundle: ProfileBundle) -> list[str]:
    """Descreve exatamente o que a instalação faria, sem escrever nada.

    A interface mostra esta lista antes de tocar no repositório do PULSARIS: o
    usuário pediu que a instalação fosse um ato explícito, não um efeito colateral.
    """
    root = Path(pulsaris_root)
    profiles = root / "instrument_data" / "profiles"
    raw = root / "instrument_data" / "raw" / "xmm_newton"
    manifest = profiles / MANIFEST_NAME

    actions = [
        f"copiar {bundle.profile_csv.name} → {profiles / bundle.profile_csv.name}",
        f"copiar {bundle.response_bin.name} → {profiles / bundle.response_bin.name}",
    ]
    for path in (bundle.arf, bundle.rmf):
        if path is not None and path.is_file():
            actions.append(f"copiar {path.name} → {raw / path.name}")

    existing = _read_manifest(manifest)
    known = {profile.get("id") for profile in existing.get("profiles", [])}
    verb = "substituir" if bundle.identifier in known else "acrescentar"
    actions.append(f"{verb} o perfil '{bundle.identifier}' em {manifest}")
    return actions


def install(pulsaris_root: Path, bundle: ProfileBundle) -> list[str]:
    """Instala o perfil no PULSARIS e devolve o que foi feito."""
    root = Path(pulsaris_root)
    profiles = root / "instrument_data" / "profiles"
    raw = root / "instrument_data" / "raw" / "xmm_newton"
    manifest_path = profiles / MANIFEST_NAME
    if not profiles.is_dir():
        raise ProfileError(f"diretório de perfis do PULSARIS não encontrado: {profiles}")

    raw.mkdir(parents=True, exist_ok=True)
    done: list[str] = []
    for source in (bundle.profile_csv, bundle.response_bin):
        shutil.copy2(source, profiles / source.name)
        done.append(str(profiles / source.name))
    for path in (bundle.arf, bundle.rmf):
        if path is not None and path.is_file():
            shutil.copy2(path, raw / path.name)
            done.append(str(raw / path.name))

    manifest = _read_manifest(manifest_path)
    manifest.setdefault("format", "PULSARIS_INSTRUMENT_PROFILES")
    manifest.setdefault("version", 2)
    entries = manifest.setdefault("profiles", [])
    for index, profile in enumerate(entries):
        if profile.get("id") == bundle.identifier:
            entries[index] = bundle.entry
            break
    else:
        entries.append(bundle.entry)

    backup = manifest_path.with_suffix(".json.bak")
    if manifest_path.is_file():
        shutil.copy2(manifest_path, backup)
        done.append(f"{backup} (cópia de segurança)")
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    done.append(str(manifest_path))
    return done


def _read_manifest(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProfileError(f"manifest do PULSARIS ilegível: {error}") from error
    return data if isinstance(data, dict) else {}


def identifier_for(obsid: str, instrument: str, mode: str = "") -> str:
    """Identificador estável de perfil para uma observação e câmera."""
    parts = ["xmm", instrument.lower(), mode.lower(), obsid]
    return "_".join(part for part in parts if part)
