"""O arquivo agrupa observações por fonte, casando pela posição."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xredux.archive import (Archive, compact_name, file_stem,  # noqa: E402
                            sanitise_name, separation_arcmin)


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
        # A pasta usa a forma compacta; o nome legível fica no descritor.
        self.assertEqual(first.parent.name, "RXJ1308.6+2127")

        stored = json.loads((first.parent / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["name"], "RX J1308.6+2127")
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


class FileStemTest(unittest.TestCase):
    """Os nomes de arquivo vão para figuras e linhas de comando."""

    def test_drops_spaces(self) -> None:
        self.assertEqual(file_stem("RX J1856.5-3754", "0412601301"),
                         "RXJ1856.5-3754_0412601301")

    def test_keeps_the_plus_of_a_positive_declination(self) -> None:
        self.assertEqual(file_stem("RX J1308.6+2127", "0844140101"),
                         "RXJ1308.6+2127_0844140101")

    def test_falls_back_to_the_obsid(self) -> None:
        self.assertEqual(file_stem("", "0163560101"), "0163560101")


class CanonicalNameTest(unittest.TestCase):
    """O nome que encabeça uma figura vem do arquivo, não da sessão."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.archive = Archive(self.root)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_observation_reports_the_source_it_belongs_to(self) -> None:
        directory = self.archive.observation_dir("0844140101", "RX J1308.6+2127",
                                                 197.2025, 21.4524)
        source = self.archive.source_of(directory)
        self.assertIsNotNone(source)
        self.assertEqual(source.name, "RX J1308.6+2127")

    def test_loose_observation_has_no_source(self) -> None:
        (self.root / "0412601301").mkdir()
        self.assertIsNone(self.archive.source_of(self.root / "0412601301"))

    def test_rename_moves_the_folder_and_keeps_the_old_name(self) -> None:
        directory = self.archive.observation_dir("0163560101", "RX J1308.6+2127",
                                                 197.2025, 21.4524)
        source = self.archive.source_of(directory)
        renamed = self.archive.rename(source, "RBS 1223")

        self.assertEqual(renamed.name, "RBS 1223")
        self.assertEqual(renamed.directory, self.root / "RBS1223")
        self.assertIn("RX J1308.6+2127", renamed.aliases)
        self.assertEqual(renamed.observations(), ["0163560101"])
        self.assertFalse((self.root / "RXJ1308.6+2127").exists())

    def test_rename_keeps_the_position_so_matching_still_works(self) -> None:
        self.archive.observation_dir("0163560101", "RX J1308.6+2127", 197.2025, 21.4524)
        source = self.archive.sources()[0]
        self.archive.rename(source, "RBS 1223")
        later = self.archive.observation_dir("0844140101", "1RXS J130848.6+212708",
                                             197.2029, 21.4522)
        self.assertEqual(later.parent.name, "RBS1223")
        self.assertEqual(self.archive.source_of(later).name, "RBS 1223")

    def test_rename_refuses_to_overwrite_another_source(self) -> None:
        self.archive.observation_dir("0163560101", "RBS 1223", 197.2, 21.45)
        self.archive.observation_dir("0412601301", "RX J1856.5-3754", 284.1, -37.9)
        other = self.archive.find(name="RX J1856.5-3754")
        with self.assertRaises(FileExistsError):
            self.archive.rename(other, "RBS 1223")


class PathSafetyTest(unittest.TestCase):
    """O SAS descarta o espaço no meio de um caminho.

    Um diretório ``RX J1308.6+2127`` chega ao odfingest como
    ``RXJ1308.6+2127`` e a tarefa falha dizendo que o ODF não existe. O nome
    legível fica no source.json; o caminho nunca tem espaço.
    """

    def test_compact_name_removes_spaces(self) -> None:
        self.assertEqual(compact_name("RX J1308.6+2127"), "RXJ1308.6+2127")

    def test_compact_name_keeps_catalogue_punctuation(self) -> None:
        self.assertEqual(compact_name("RX J1856.5-3754"), "RXJ1856.5-3754")

    def test_no_archived_path_ever_contains_a_space(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(Path(directory))
            work = archive.observation_dir("0844140101", "RX J1308.6+2127",
                                           197.2025, 21.4524)
            self.assertNotIn(" ", str(work.relative_to(directory)))

    def test_readable_name_survives_in_the_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(Path(directory))
            work = archive.observation_dir("0844140101", "RX J1308.6+2127",
                                           197.2025, 21.4524)
            source = archive.source_of(work)
            self.assertEqual(source.name, "RX J1308.6+2127")
            self.assertEqual(source.directory.name, "RXJ1308.6+2127")

    def test_rename_also_yields_a_path_without_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(Path(directory))
            work = archive.observation_dir("0163560101", "RBS1223", 197.2, 21.45)
            renamed = archive.rename(archive.source_of(work), "RX J1308.6+2127")
            self.assertEqual(renamed.name, "RX J1308.6+2127")
            self.assertEqual(renamed.directory.name, "RXJ1308.6+2127")
            self.assertEqual(renamed.observations(), ["0163560101"])

    def test_folders_with_spaces_are_reported_for_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "RX J1308.6+2127").mkdir()
            (root / "RX J1308.6+2127" / "source.json").write_text(
                '{"name": "RX J1308.6+2127"}', encoding="utf-8")
            (root / "RXJ1856.5-3754").mkdir()
            (root / "RXJ1856.5-3754" / "source.json").write_text(
                '{"name": "RX J1856.5-3754"}', encoding="utf-8")

            misnamed = Archive(root).misnamed()
            self.assertEqual([s.directory.name for s in misnamed], ["RX J1308.6+2127"])


if __name__ == "__main__":
    unittest.main()
