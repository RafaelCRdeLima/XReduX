"""Testes da construção e instalação do perfil de instrumento do PULSARIS.

A construção reaproveita o leitor de ARF/RMF do próprio PULSARIS, então estes
testes só rodam quando o repositório vizinho está presente. A instalação, por
outro lado, é testada contra uma árvore falsa: escrever no repositório real
durante um teste seria inaceitável.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from astropy.io import fits

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_export import write_rmf  # noqa: E402
from xredux.config import DEFAULT_PULSARIS_ROOT  # noqa: E402
from xredux.export import profile as profile_export  # noqa: E402

PULSARIS_ROOT = DEFAULT_PULSARIS_ROOT
HAS_PULSARIS = (PULSARIS_ROOT / "scripts" / "build_instrument_profiles.py").is_file()


def write_arf(path: Path, rows: int = 200) -> None:
    """ARF mínima com a extensão SPECRESP, como a que o ``arfgen`` produz."""
    energ_lo = np.linspace(0.1, 12.0, rows, dtype=np.float32)
    energ_hi = energ_lo + (12.0 - 0.1) / rows
    # Envelope grosseiro da área efetiva do EPIC-pn, com pico perto de 1,5 keV.
    centre = 0.5 * (energ_lo + energ_hi)
    specresp = (1500.0 * np.exp(-0.5 * ((centre - 1.5) / 2.5) ** 2)).astype(np.float32)

    hdu = fits.BinTableHDU.from_columns(fits.ColDefs([
        fits.Column(name="ENERG_LO", format="E", array=energ_lo),
        fits.Column(name="ENERG_HI", format="E", array=energ_hi),
        fits.Column(name="SPECRESP", format="E", array=specresp),
    ]), name="SPECRESP")
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path, overwrite=True)


def rename_matrix_extension(path: Path, name: str) -> None:
    """Renomeia a extensão MATRIX, imitando a variação de nome do ``rmfgen``."""
    with fits.open(path, mode="update") as hdus:
        for hdu in hdus:
            if str(hdu.header.get("EXTNAME", "")).strip().upper() == "MATRIX":
                hdu.header["EXTNAME"] = name


class NormalizeRmfTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.rmf = self.directory / "response.rmf"
        write_rmf(self.rmf)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_keeps_a_conforming_matrix(self) -> None:
        output = profile_export.normalize_rmf(self.rmf, self.directory / "copy.rmf")
        with fits.open(output) as hdus:
            names = [str(hdu.header.get("EXTNAME", "")).upper() for hdu in hdus]
        self.assertIn("MATRIX", names)
        self.assertIn("EBOUNDS", names)

    def test_renames_specresp_matrix(self) -> None:
        """É o nome que o rmfgen do SAS às vezes usa e que o PULSARIS não acha."""
        rename_matrix_extension(self.rmf, "SPECRESP MATRIX")
        output = profile_export.normalize_rmf(self.rmf, self.directory / "fixed.rmf")
        with fits.open(output) as hdus:
            names = [str(hdu.header.get("EXTNAME", "")).upper() for hdu in hdus]
        self.assertIn("MATRIX", names)

    def test_rejects_a_response_without_matrix(self) -> None:
        rename_matrix_extension(self.rmf, "SOMETHING ELSE")
        with self.assertRaises(profile_export.ProfileError):
            profile_export.normalize_rmf(self.rmf, self.directory / "broken.rmf")


class IdentifierTest(unittest.TestCase):
    def test_matches_the_pulsaris_naming(self) -> None:
        self.assertEqual(
            profile_export.identifier_for("0106260101", "EPN", "TIMING"),
            "xmm_epn_timing_0106260101")

    def test_omits_an_unknown_mode(self) -> None:
        self.assertEqual(profile_export.identifier_for("0106260101", "EMOS1"),
                         "xmm_emos1_0106260101")


@unittest.skipUnless(HAS_PULSARIS, "repositório do PULSARIS não encontrado")
class ProfileBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.arf = self.directory / "source.arf"
        self.rmf = self.directory / "source.rmf"
        write_arf(self.arf)
        write_rmf(self.rmf)
        self.output = self.directory / "profiles"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def build(self, arf: Path | None = None):
        return profile_export.build(
            PULSARIS_ROOT, self.output,
            identifier="xmm_epn_timing_test", label="XMM-Newton / EPIC-pn teste",
            instrument="EPN TIMING THIN", arf=arf, rmf=self.rmf,
            energy_range_kev=(0.15, 12.0), time_resolution_us=29.52)

    def test_produces_the_two_expected_files(self) -> None:
        bundle = self.build(self.arf)
        self.assertTrue(bundle.profile_csv.is_file())
        self.assertTrue(bundle.response_bin.is_file())
        self.assertEqual(bundle.entry["redistribution"], "full_sparse_ogip_rmf")
        self.assertEqual(bundle.warnings, [])

    def test_csv_matches_the_reference_header(self) -> None:
        """O formato tem de bater com o dos perfis que o PULSARIS já traz."""
        reference = (PULSARIS_ROOT / "instrument_data" / "profiles"
                     / "xmm_epn_timing.csv")
        bundle = self.build(self.arf)
        produced = bundle.profile_csv.read_text(encoding="utf-8").splitlines()
        self.assertEqual(produced[0],
                         reference.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(len(produced[1].split(",")), 5)

    def test_real_arf_gives_a_non_zero_effective_area(self) -> None:
        """O ponto do programa: área efetiva vinda do arfgen, não de um envelope."""
        bundle = self.build(self.arf)
        rows = [line for line in bundle.profile_csv.read_text(encoding="utf-8")
                .splitlines() if not line.startswith("#")]
        areas = np.array([float(row.split(",")[1]) for row in rows])
        self.assertGreater(areas.max(), 100.0)

    def test_missing_arf_is_reported_not_silently_zeroed(self) -> None:
        bundle = self.build(None)
        self.assertTrue(any("sem ARF" in message for message in bundle.warnings))

    def test_sparse_response_has_the_pulsaris_magic(self) -> None:
        bundle = self.build(self.arf)
        with bundle.response_bin.open("rb") as stream:
            self.assertEqual(stream.read(8), b"PLSRMF3\0")


@unittest.skipUnless(HAS_PULSARIS, "repositório do PULSARIS não encontrado")
class ProfileInstallTest(unittest.TestCase):
    """Instala numa árvore falsa: o repositório real nunca é tocado num teste."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)

        self.fake_root = self.directory / "PULSARIS"
        profiles = self.fake_root / "instrument_data" / "profiles"
        profiles.mkdir(parents=True)
        (profiles / "manifest.json").write_text(json.dumps({
            "format": "PULSARIS_INSTRUMENT_PROFILES", "version": 2,
            "profiles": [{"id": "generic", "label": "Ideal generic detector"}],
        }, indent=2), encoding="utf-8")

        self.arf = self.directory / "source.arf"
        self.rmf = self.directory / "source.rmf"
        write_arf(self.arf)
        write_rmf(self.rmf)
        self.bundle = profile_export.build(
            PULSARIS_ROOT, self.directory / "profiles",
            identifier="xmm_epn_timing_test", label="teste",
            instrument="EPN TIMING", arf=self.arf, rmf=self.rmf,
            energy_range_kev=(0.15, 12.0), time_resolution_us=29.52)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def manifest(self) -> dict:
        path = self.fake_root / "instrument_data" / "profiles" / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_preview_writes_nothing(self) -> None:
        before = sorted(path.name for path in
                        (self.fake_root / "instrument_data" / "profiles").iterdir())
        actions = profile_export.preview_install(self.fake_root, self.bundle)
        after = sorted(path.name for path in
                       (self.fake_root / "instrument_data" / "profiles").iterdir())
        self.assertEqual(before, after)
        self.assertTrue(any("acrescentar" in action for action in actions))

    def test_install_adds_the_profile_and_keeps_the_others(self) -> None:
        profile_export.install(self.fake_root, self.bundle)
        identifiers = [entry["id"] for entry in self.manifest()["profiles"]]
        self.assertIn("generic", identifiers)
        self.assertIn("xmm_epn_timing_test", identifiers)

    def test_reinstall_replaces_instead_of_duplicating(self) -> None:
        profile_export.install(self.fake_root, self.bundle)
        profile_export.install(self.fake_root, self.bundle)
        identifiers = [entry["id"] for entry in self.manifest()["profiles"]]
        self.assertEqual(identifiers.count("xmm_epn_timing_test"), 1)

    def test_install_keeps_a_backup_of_the_manifest(self) -> None:
        profile_export.install(self.fake_root, self.bundle)
        backup = (self.fake_root / "instrument_data" / "profiles"
                  / "manifest.json.bak")
        self.assertTrue(backup.is_file())

    def test_raw_products_are_copied_for_provenance(self) -> None:
        profile_export.install(self.fake_root, self.bundle)
        raw = self.fake_root / "instrument_data" / "raw" / "xmm_newton"
        self.assertTrue((raw / self.arf.name).is_file())
        self.assertTrue((raw / self.rmf.name).is_file())


if __name__ == "__main__":
    unittest.main()
