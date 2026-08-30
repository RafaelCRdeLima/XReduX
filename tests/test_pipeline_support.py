"""Testes da infraestrutura: sessão, execução de comandos e leitura do ODF."""

from __future__ import annotations

import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xredux.runner import Cancelled, ProcessRunner, TaskFailed, sas_command  # noqa: E402
from xredux.session import Session  # noqa: E402
from xredux.tasks import calibration  # noqa: E402
from xredux.tasks.base import selection_expression  # noqa: E402
from xredux.tasks.epic import EventList  # noqa: E402
from xredux.tasks.filtering import BackgroundCurve  # noqa: E402


class SasCommandTest(unittest.TestCase):
    def test_builds_parameter_pairs(self) -> None:
        command = sas_command("evselect", {"table": "events.fits", "timebinsize": 100})
        self.assertEqual(command, ["evselect", "table=events.fits", "timebinsize=100"])

    def test_translates_booleans_to_yes_and_no(self) -> None:
        command = sas_command("arfgen", {"withrmfset": True, "extendedsource": False})
        self.assertEqual(command[1:], ["withrmfset=yes", "extendedsource=no"])

    def test_drops_unset_parameters(self) -> None:
        self.assertEqual(sas_command("cifbuild", {"a": None, "b": 1}),
                         ["cifbuild", "b=1"])

    def test_keeps_an_expression_as_a_single_argument(self) -> None:
        expression = "(FLAG==0) && (PATTERN<=4)"
        command = sas_command("evselect", {"expression": expression})
        self.assertEqual(command[1], f"expression={expression}")


class SelectionExpressionTest(unittest.TestCase):
    def test_joins_and_parenthesises(self) -> None:
        self.assertEqual(selection_expression(["FLAG==0", "PATTERN<=4"]),
                         "(FLAG==0) && (PATTERN<=4)")

    def test_ignores_empty_parts(self) -> None:
        self.assertEqual(selection_expression(["FLAG==0", "", None, "  "]),
                         "(FLAG==0)")


class ProcessRunnerTest(unittest.TestCase):
    def test_captures_output_and_streams_lines(self) -> None:
        lines: list[str] = []
        runner = ProcessRunner(on_line=lines.append)
        result = runner.run(["printf", "alpha\\nbeta\\n"])
        self.assertTrue(result.ok)
        self.assertIn("alpha", result.output)
        self.assertIn("beta", lines)

    def test_detects_a_sas_error_despite_exit_zero(self) -> None:
        """O SAS costuma relatar erro no texto e ainda assim sair com código 0."""
        runner = ProcessRunner()
        result = runner.run(["printf", "** evselect: error (badDataSet), oops\\n"])
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.ok)
        self.assertTrue(result.errors)

    def test_collects_warnings_without_failing(self) -> None:
        runner = ProcessRunner()
        result = runner.run(["printf", "** epproc: warning (foo), careful\\n"])
        self.assertTrue(result.ok)
        self.assertEqual(len(result.warnings), 1)

    def test_check_raises_on_failure(self) -> None:
        runner = ProcessRunner()
        with self.assertRaises(TaskFailed):
            runner.check(["false"])

    def test_missing_program_is_reported_not_raised(self) -> None:
        runner = ProcessRunner()
        result = runner.run(["xredux-nao-existe-mesmo"])
        self.assertEqual(result.returncode, 127)
        self.assertFalse(result.ok)

    def test_cancel_blocks_further_commands(self) -> None:
        runner = ProcessRunner()
        runner.cancel()
        with self.assertRaises(Cancelled):
            runner.run(["true"])
        runner.reset()
        self.assertTrue(runner.run(["true"]).ok)


class SessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_survives_a_reload(self) -> None:
        session = Session(self.directory, "0106260101", "RX J1856.5-3754")
        session.begin("acquisition", {"level": "ODF"})
        session.finish("acquisition", message="pronto")

        reloaded = Session.load_or_create(self.directory, "0106260101")
        self.assertEqual(reloaded.target, "RX J1856.5-3754")
        self.assertTrue(reloaded.is_done("acquisition"))
        self.assertEqual(reloaded.steps["acquisition"].parameters["level"], "ODF")

    def test_failure_is_recorded(self) -> None:
        session = Session(self.directory, "0106260101")
        session.begin("calibration")
        session.fail("calibration", "cifbuild não encontrou CCF")
        reloaded = Session.load_or_create(self.directory, "0106260101")
        self.assertEqual(reloaded.steps["calibration"].status, "failed")
        self.assertIn("cifbuild", reloaded.steps["calibration"].message)

    def test_script_is_executable_and_replays_the_commands(self) -> None:
        session = Session(self.directory, "0106260101")
        session.begin("filtering")
        runner = ProcessRunner()
        session.record_command("filtering", runner.run(["printf", "ok\\n"]))
        session.finish("filtering")

        script = self.directory / "reproduce.sh"
        self.assertTrue(script.is_file())
        self.assertTrue(script.stat().st_mode & stat.S_IXUSR)
        content = script.read_text(encoding="utf-8")
        self.assertIn("printf", content)
        self.assertIn("0106260101", content)
        # Um shell tem de conseguir ao menos analisar o que foi gerado.
        self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)

    def test_path_parameters_are_serialisable(self) -> None:
        session = Session(self.directory, "0106260101")
        session.begin("regions", {"image": Path("/tmp/image.fits")})
        reloaded = Session.load_or_create(self.directory, "0106260101")
        self.assertEqual(reloaded.steps["regions"].parameters["image"],
                         "/tmp/image.fits")


class OdfReadingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_identifies_instruments_and_modes_from_filenames(self) -> None:
        """Nomes reais de ODF, colhidos da observação 0412601301."""
        for name in ("2062_0412601301_PNS00304IME.FIT",
                     "2062_0412601301_PNS00300AUX.FIT",
                     "2062_0412601301_M1S00110IME.FIT",
                     "2062_0412601301_R1S00400SPE.FIT",
                     "leiame.txt"):
            (self.directory / name).write_bytes(b"")

        exposures = calibration._read_exposures(self.directory)
        found = {(item.instrument, item.mode) for item in exposures}
        self.assertIn(("EPN", "IMAGING"), found)
        self.assertIn(("EMOS1", "IMAGING"), found)
        self.assertIn(("RGS1", "SPECTRUM"), found)
        # O AUX e o IME do PN S003 são a mesma exposição, não duas.
        self.assertEqual(sum(1 for item in exposures
                             if item.instrument == "EPN" and item.exposure_id == "S003"), 1)

    def test_recognises_a_timing_mode_exposure(self) -> None:
        (self.directory / "0123_0106260101_PNS00301TIE.FIT").write_bytes(b"")
        exposures = calibration._read_exposures(self.directory)
        self.assertEqual([(item.instrument, item.mode) for item in exposures],
                         [("EPN", "TIMING")])

    def test_reads_target_and_converts_right_ascension(self) -> None:
        """O sumário traz a AR em horas; usá-la em graus erra o alvo por dezenas de graus."""
        summary = self.directory / "2062_0412601301_SCX00000SUM.SAS"
        summary.write_text(
            "RXJ1856.6-3754       / Target Name\n"
            "18.9430833 / Target Right Ascension\n"
            "-37.9084722 / Target Declination\n"
            "18.9432500 / Boresight Right Ascension\n",
            encoding="latin-1")

        setup = calibration.read_setup(None, self.directory / "ccf.cif",
                                       summary, self.directory)
        self.assertEqual(setup.target, "RXJ1856.6-3754")
        self.assertAlmostEqual(setup.ra, 284.1462495, places=5)
        self.assertAlmostEqual(setup.dec, -37.9084722, places=5)

    def test_timing_and_burst_are_flagged_as_fast(self) -> None:
        self.assertTrue(calibration.Exposure("EPN", "TIMING").is_fast)
        self.assertTrue(calibration.Exposure("EPN", "BURST").is_fast)
        self.assertFalse(calibration.Exposure("EPN", "IMAGING").is_fast)


class ImageTransformTest(unittest.TestCase):
    """A ida e volta detector ↔ pixel é o que liga o clique à coordenada."""

    def transform(self):
        from xredux.tasks.regions import ImageTransform
        # Valores reais de uma imagem do evselect com binning de 80 unidades.
        return ImageTransform(0.0125, 0.49375, 0.0125, 0.49375)

    def test_round_trip_returns_the_original_coordinate(self) -> None:
        transform = self.transform()
        for detector in (25826.0, 23857.0, 12345.6):
            self.assertAlmostEqual(
                transform.inverse_x(transform.x(detector)), detector, places=6)
            self.assertAlmostEqual(
                transform.inverse_y(transform.y(detector)), detector, places=6)

    def test_known_pixel_matches_the_header_arithmetic(self) -> None:
        """X = 25826 cai no pixel 323, que é onde a fonte aparece."""
        self.assertAlmostEqual(self.transform().x(25826.0), 323.319, places=3)

    def test_length_converts_both_ways(self) -> None:
        transform = self.transform()
        # 30 segundos de arco = 600 unidades de detector = 7,5 pixels.
        self.assertAlmostEqual(transform.length(600.0), 7.5)
        self.assertAlmostEqual(transform.inverse_length(7.5), 600.0)


class ImagePlotZoomTest(unittest.TestCase):
    """Mexer na região não pode custar o enquadramento.

    Redesenhar a figura inteira a cada ajuste descartava o zoom — justamente
    quando o usuário aproximava para posicionar com precisão.
    """

    @classmethod
    def setUpClass(cls) -> None:
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "minimal")
        from PySide6.QtWidgets import QApplication
        cls.application = QApplication.instance() or QApplication(["test"])

    def plot(self):
        import numpy as np
        from xredux.gui.widgets.plots import ImagePlot
        widget = ImagePlot()
        widget.show_image(np.ones((64, 64)),
                          source={"kind": "circle", "x": 32, "y": 32, "radius": 5},
                          background={"kind": "annulus", "x": 32, "y": 32,
                                      "inner": 10, "outer": 20})
        return widget

    def test_moving_the_region_keeps_the_view(self) -> None:
        widget = self.plot()
        widget.axes.set_xlim(20, 40)
        widget.axes.set_ylim(20, 40)
        widget.set_regions(source={"kind": "circle", "x": 30, "y": 30, "radius": 7},
                           background={"kind": "annulus", "x": 30, "y": 30,
                                       "inner": 12, "outer": 24})
        self.assertEqual(widget.axes.get_xlim(), (20.0, 40.0))
        self.assertEqual(widget.axes.get_ylim(), (20.0, 40.0))

    def test_updating_does_not_pile_up_artists(self) -> None:
        """Sem remover os antigos, cada ajuste deixaria um círculo para trás."""
        widget = self.plot()
        for radius in (6, 7, 8, 9):
            widget.set_regions(source={"kind": "circle", "x": 32, "y": 32,
                                       "radius": radius},
                               background={"kind": "annulus", "x": 32, "y": 32,
                                           "inner": 10, "outer": 20})
        self.assertEqual(len(widget._patches), 3)

    def test_regions_before_an_image_are_remembered_not_drawn(self) -> None:
        from xredux.gui.widgets.plots import ImagePlot
        widget = ImagePlot()
        widget.set_regions(source={"kind": "circle", "x": 1, "y": 1, "radius": 1})
        self.assertEqual(widget._patches, [])
        self.assertEqual(widget._geometry["source"]["radius"], 1)


class SessionRestoreTest(unittest.TestCase):
    """Reabrir uma sessão tem de trazer o estado de volta, não só as marcas.

    Antes disto a janela mostrava as etapas concluídas e as páginas vazias — as
    coordenadas da fonte voltavam zeradas —, o que engana mais do que não marcar
    nada.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def pipeline(self):
        from xredux.config import Settings
        from xredux.pipeline import Pipeline
        from xredux.runner import ProcessRunner
        from xredux.tasks.base import TaskContext

        session = Session.load_or_create(self.directory, "0000000000", "alvo")
        context = TaskContext(runner=ProcessRunner(), env={}, session=session,
                              work_dir=self.directory)
        return Pipeline(Settings(), session, context)

    def test_regions_survive_a_reopen_with_their_description(self) -> None:
        from xredux.tasks import regions

        first = self.pipeline()
        source = regions.circle(25000.0, 24000.0, 30.0)
        background = regions.annulus(25000.0, 24000.0, 60.0, 120.0)
        first.set_regions(source, background)

        second = self.pipeline()
        second.restore()
        self.assertEqual(second.state.source_region.expression, source.expression)
        self.assertEqual(second.state.source_region.description, source.description)
        self.assertEqual(second.state.source_region.kind, "circle")
        self.assertEqual(second.state.background_region.kind, "annulus")

    def test_period_survives_a_reopen(self) -> None:
        first = self.pipeline()
        first.session.step("timing").parameters["period_s"] = 10.312538
        first.session.save()

        second = self.pipeline()
        second.restore()
        self.assertAlmostEqual(second.state.period_s, 10.312538)

    def test_restore_tolerates_products_deleted_to_free_space(self) -> None:
        """Apagar o ODF para liberar disco não pode impedir o resto de voltar."""
        first = self.pipeline()
        first.session.begin("acquisition")
        first.session.finish("acquisition", outputs=[self.directory / "odf-que-sumiu"])
        first.session.begin("calibration")
        first.session.finish("calibration", outputs=[self.directory / "ccf.cif"])

        second = self.pipeline()
        restored = second.restore()
        self.assertIsNone(second.state.odf_dir)
        self.assertIsNone(second.state.ccf_cif)
        self.assertEqual(restored, [])

    def test_restore_of_an_untouched_session_reports_nothing(self) -> None:
        self.assertEqual(self.pipeline().restore(), [])


class GuideTest(unittest.TestCase):
    """O guia é aberto pelo botão da barra; se faltar, o botão não faz nada."""

    def test_exists_in_both_languages(self) -> None:
        from xredux.config import GUIDE
        for language in ("pt_BR", "en"):
            page = GUIDE / f"guide.{language}.html"
            self.assertTrue(page.is_file(), f"falta {page.name}; rode tools/build_guide.py")
            self.assertIn("<title>", page.read_text(encoding="utf-8")[:400])

    def test_every_referenced_image_is_present(self) -> None:
        """Uma figura quebrada só aparece quando alguém abre o guia."""
        import re

        from xredux.config import GUIDE
        page = GUIDE / "guide.pt_BR.html"
        names = re.findall(r'src="img/([^"]+)"', page.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(names), 8)
        for name in names:
            self.assertTrue((GUIDE / "img" / name).is_file(), f"figura ausente: {name}")

    def test_lookup_falls_back_to_portuguese(self) -> None:
        from xredux.config import guide_page
        self.assertIsNotNone(guide_page("pt_BR"))
        self.assertIsNotNone(guide_page("idioma-inexistente"))


class EventListTest(unittest.TestCase):
    def test_pn_and_mos_get_different_filters(self) -> None:
        pn = EventList(Path("pn.ds"), "EPN", "TIMING")
        mos = EventList(Path("mos.ds"), "EMOS1", "IMAGING")
        self.assertEqual(pn.quality_flag, "#XMMEA_EP")
        self.assertEqual(mos.quality_flag, "#XMMEA_EM")
        self.assertEqual(pn.max_pattern, 4)
        self.assertEqual(mos.max_pattern, 12)

    def test_time_resolution_reflects_the_mode(self) -> None:
        self.assertAlmostEqual(
            EventList(Path("a"), "EPN", "TIMING").time_resolution_us(), 29.52)
        self.assertAlmostEqual(
            EventList(Path("a"), "EPN", "BURST").time_resolution_us(), 7.0)
        self.assertGreater(
            EventList(Path("a"), "EPN", "IMAGING").time_resolution_us(), 1000.0)

    def test_submode_decides_the_resolution(self) -> None:
        """Small Window e Full Frame são ambos IMAGING e diferem por 13x."""
        small = EventList(Path("a"), "EPN", "IMAGING", submode="PrimeSmallWindow")
        full = EventList(Path("a"), "EPN", "IMAGING", submode="PrimeFullWindow")
        extended = EventList(Path("a"), "EPN", "IMAGING",
                             submode="PrimeFullWindowExtended")
        self.assertAlmostEqual(small.time_resolution_us(), 5_700.0)
        self.assertAlmostEqual(full.time_resolution_us(), 73_400.0)
        self.assertAlmostEqual(extended.time_resolution_us(), 199_200.0)

    def test_mos_submodes_are_covered(self) -> None:
        self.assertAlmostEqual(
            EventList(Path("a"), "EMOS2", "IMAGING",
                      submode="PrimeFullWindow").time_resolution_us(), 2_600_000.0)
        self.assertAlmostEqual(
            EventList(Path("a"), "EMOS2", "TIMING",
                      submode="FastUncompressed").time_resolution_us(), 1_750.0)

    def test_unknown_mode_falls_back_to_imaging(self) -> None:
        resolution = EventList(Path("a"), "EPN", "ESQUISITO").time_resolution_us()
        self.assertAlmostEqual(resolution, 73_400.0)


class BackgroundThresholdTest(unittest.TestCase):
    def curve(self, rate) -> BackgroundCurve:
        import numpy as np
        rate = np.asarray(rate, dtype=float)
        return BackgroundCurve(path=Path("bkg.fits"), time=np.arange(rate.size,
                               dtype=float) * 100.0, rate=rate,
                               instrument="EPN", binsize_s=100.0)

    def test_uses_the_esa_value_for_a_quiet_background(self) -> None:
        import numpy as np
        quiet = self.curve(np.full(100, 0.10))
        self.assertAlmostEqual(quiet.suggested_threshold(), 0.4)

    def test_falls_back_to_a_robust_estimate_when_the_field_is_bright(self) -> None:
        import numpy as np
        generator = np.random.default_rng(3)
        bright = self.curve(generator.normal(2.0, 0.1, size=200))
        threshold = bright.suggested_threshold()
        self.assertGreater(threshold, 0.4)
        self.assertLess(threshold, 3.0)

    def test_a_long_flare_does_not_raise_the_threshold(self) -> None:
        """Metade da exposição em surto puxa a mediana para dentro do surto.

        Reproduz a observação 0844140101 de RBS 1223: quiescente em 0,24 ct/s e
        um surto ocupando quase metade do tempo. Tomar a mediana como referência
        sugeria 1,52 ct/s — afrouxando o corte justamente onde ele precisa
        apertar, e deixando bins contaminados passarem.
        """
        import numpy as np
        generator = np.random.default_rng(5)
        quiet = generator.normal(0.24, 0.05, size=160)
        flare = generator.uniform(1.5, 12.0, size=136)
        curve = self.curve(np.concatenate([quiet, flare]))

        self.assertAlmostEqual(curve.quiescent_level(), 0.24, delta=0.06)
        self.assertAlmostEqual(curve.suggested_threshold(), 0.4, places=6)

    def test_a_genuinely_bright_field_still_raises_the_threshold(self) -> None:
        """Quando o próprio quiescente passa do recomendado, o corte sobe."""
        import numpy as np
        generator = np.random.default_rng(6)
        curve = self.curve(generator.normal(2.0, 0.1, size=200))
        self.assertGreater(curve.suggested_threshold(), 1.5)

    def test_bin_size_sets_how_far_the_cut_sits_from_the_noise(self) -> None:
        """Bin curto aproxima o corte do ruído; longo afasta. É a escolha inteira."""
        import numpy as np
        generator = np.random.default_rng(7)
        values = np.concatenate([generator.normal(0.24, 0.05, size=160),
                                 generator.uniform(1.5, 12.0, size=136)])
        curto, longo = self.curve(values), self.curve(values)
        curto.binsize_s, longo.binsize_s = 10.0, 200.0
        self.assertLess(curto.separation(0.4), 2.0)
        self.assertGreater(longo.separation(0.4), 4.0)

    def test_suggested_bin_reaches_three_sigma(self) -> None:
        import numpy as np
        generator = np.random.default_rng(8)
        curve = self.curve(np.concatenate([generator.normal(0.24, 0.05, size=160),
                                           generator.uniform(1.5, 12.0, size=136)]))
        curve.binsize_s = curve.suggested_binsize(0.4)
        self.assertAlmostEqual(curve.separation(0.4), 3.0, places=6)

    def test_separation_is_infinite_without_a_quiescent_level(self) -> None:
        import numpy as np
        curve = self.curve(np.zeros(50))
        self.assertEqual(curve.separation(0.4), float("inf"))
        self.assertEqual(curve.suggested_binsize(0.4), curve.binsize_s)

    def test_good_time_reports_seconds_not_only_a_fraction(self) -> None:
        """50% de 80 ks e 50% de 8 ks são decisões diferentes."""
        import numpy as np
        curve = self.curve(np.concatenate([np.full(80, 0.1), np.full(20, 5.0)]))
        self.assertAlmostEqual(curve.good_time(0.4), 80 * 100.0)

    def test_good_fraction_counts_the_surviving_bins(self) -> None:
        import numpy as np
        mixed = self.curve(np.concatenate([np.full(80, 0.1), np.full(20, 5.0)]))
        self.assertAlmostEqual(mixed.good_fraction(0.4), 0.8)


class TimingPersistenceTest(unittest.TestCase):
    """Os resultados do timing têm de sobreviver ao fechamento da sessão."""

    def _pipeline(self, work: Path):
        from xredux.config import Settings
        from xredux.pipeline import Pipeline
        from xredux.tasks.base import TaskContext

        session = Session.load_or_create(work, "0412601301", "RX J1856.5-3754")
        context = TaskContext(runner=ProcessRunner(), env={}, session=session,
                              work_dir=work)
        return Pipeline(Settings(), session, context)

    def test_results_survive_a_new_barycentric_correction(self) -> None:
        """O barycen reescreve o passo que antes guardava o período."""
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            pipeline = self._pipeline(work)
            pipeline.state.period_s = 7.055
            pipeline.state.h_statistic = 41.2
            pipeline.state.h_harmonics = 2
            pipeline.state.pulsed_fraction = (0.012, 0.003)
            pipeline.state.search_confirmed = True
            pipeline._remember_timing()

            # Refazer a correção baricêntrica reinicia o passo "timing".
            pipeline.session.begin("timing", {"ra": 284.1, "dec": -37.9})

            reopened = self._pipeline(work)
            reopened.restore()
            self.assertAlmostEqual(reopened.state.period_s, 7.055)
            self.assertAlmostEqual(reopened.state.h_statistic, 41.2)
            self.assertEqual(reopened.state.h_harmonics, 2)
            self.assertEqual(reopened.state.pulsed_fraction, (0.012, 0.003))
            self.assertIs(reopened.state.search_confirmed, True)

    def test_sessions_written_before_the_change_are_still_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            pipeline = self._pipeline(work)
            pipeline.session.step("timing").parameters["period_s"] = 10.31
            pipeline.session.save()

            reopened = self._pipeline(work)
            reopened.restore()
            self.assertAlmostEqual(reopened.state.period_s, 10.31)


if __name__ == "__main__":
    unittest.main()
