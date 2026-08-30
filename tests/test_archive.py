"""O arquivo agrupa observações por fonte, casando pela posição."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xredux.archive import Archive, sanitise_name, separation_arcmin  # noqa: E402


class NameTest(unittest.TestCase):
    def test_keeps_readable_names(self) -> None:
        self.assertEqual(sanitise_name("RX J1856.5-3754"), "RX J1856.5-3754")

    def test_drops_hostile_characters(self) -> None:
        self.assertEqual(sanitise_name("Cen  X-3/A"), "Cen X-3A")

    def test_falls_back_when_nothing_remains(self) -> None:
        self.assertEqual(sanitise_name("   "), "fonte-sem-nome")


class SeparationTest(unittest.TestCase):
    def test_right_ascension_shrinks_with_declination(self) -> None:
        self.assertAlmostEqual(separation_arcmin(0.0, 60.0, 1.0, 60.0), 30.0, places=6)

    def test_declination_is_direct(self) -> None:
        self.assertAlmostEqual(separation_arcmin(0.0, 0.0, 0.0, 1.0), 60.0, places=6)


class ArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.archive = Archive(self.root)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_same_position_under_different_names_shares_one_folder(self) -> None:
        """O caso real: o ODF diz RBS1223, a busca diz RX J1308.6+2127."""
        first = self.archive.observation_dir("0163560101", "RX J1308.6+2127",
                                             197.2025, 21.4524)
        second = self.archive.observation_dir("0844140101", "RBS1223",
                                              197.2029, 21.4522)
        self.assertEqual(first.parent, second.parent)
        self.assertEqual(first.parent.name, "RX J1308.6+2127")

        stored = json.loads((first.parent / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["aliases"], ["RBS1223"])

    def test_distinct_sources_stay_apart(self) -> None:
        one = self.archive.observation_dir("0163560101", "RBS 1223", 197.20, 21.45)
        other = self.archive.observation_dir("0412601301", "RX J1856.5-3754",
                                             284.15, -37.91)
        self.assertNotEqual(one.parent, other.parent)
        self.assertEqual(len(self.archive.sources()), 2)

    def test_name_matches_when_position_is_unknown(self) -> None:
        """Sem coordenadas o nome ainda serve, ignorando espaços e caixa."""
        first = self.archive.observation_dir("0163560101", "RX J1308.6+2127",
                                             197.2, 21.45)
        second = self.archive.observation_dir("0844140101", "rxj1308.6+2127")
        self.assertEqual(first.parent, second.parent)

    def test_known_observation_is_not_moved(self) -> None:
        """Reabrir uma observação nunca deve reorganizar o disco sozinho."""
        original = self.archive.observation_dir("0163560101", "RBS1223", 197.2, 21.45)
        again = self.archive.observation_dir("0163560101", "Outro Nome", 10.0, -20.0)
        self.assertEqual(again, original)

    def test_plan_does_not_touch_the_disk(self) -> None:
        self.archive.source_for("RBS1223", 197.2, 21.45, create=False)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_legacy_observations_are_still_found(self) -> None:
        (self.root / "0412601301").mkdir()
        self.assertEqual(self.archive.legacy_observations(), ["0412601301"])
        self.assertEqual(self.archive.locate("0412601301"), self.root / "0412601301")
        self.assertIsNone(self.archive.locate("0000000000"))

    def test_observations_and_odf_state_are_reported(self) -> None:
        directory = self.archive.observation_dir("0163560101", "RBS1223", 197.2, 21.45)
        source = self.archive.sources()[0]
        self.assertEqual(source.observations(), ["0163560101"])
        self.assertIsNone(source.odf_directory("0163560101"))

        odf = directory / "odf"
        odf.mkdir()
        (odf / "0123_0163560101_PNS00300IME.FIT").touch()
        self.assertEqual(source.odf_directory("0163560101"), odf)


if __name__ == "__main__":
    unittest.main()
