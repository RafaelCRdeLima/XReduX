"""Regressões dos achados da auditoria de código (auditoria/auditoria-codigo.md).

Cada classe corresponde a um achado XR-nn e reproduz o gatilho descrito lá.
Nada aqui roda o SAS: as tarefas externas são simuladas quando é preciso.
"""

from __future__ import annotations

import io
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xredux.runner import Cancelled, ProcessRunner  # noqa: E402
from xredux.tasks import acquisition  # noqa: E402
from xredux.tasks.epic import PileupCheck  # noqa: E402


class TemporaryDirectoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="xredux-test-")
        self.directory = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()


class TimeoutTest(unittest.TestCase):
    """XR-05: o prazo vale mesmo para um processo que não escreve nada."""

    def test_silent_process_is_stopped_at_the_deadline(self) -> None:
        runner = ProcessRunner()
        started = time.monotonic()
        result = runner.run(["sleep", "5"], timeout=0.3)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)
        self.assertIn("tempo limite", result.summary())

    def test_partial_line_without_newline_does_not_block_the_deadline(self) -> None:
        runner = ProcessRunner()
        result = runner.run(["bash", "-c", "printf 'sem quebra'; sleep 5"], timeout=0.3)
        self.assertTrue(result.timed_out)

    def test_timeout_does_not_cancel_the_next_command(self) -> None:
        """Antes, o prazo chamava cancel() e todo comando seguinte falhava."""
        runner = ProcessRunner()
        runner.run(["sleep", "5"], timeout=0.2)
        self.assertTrue(runner.run(["true"]).ok)

    def test_process_ignoring_sigterm_is_killed(self) -> None:
        runner = ProcessRunner()
        started = time.monotonic()
        result = runner.run(["bash", "-c", "trap '' TERM; sleep 30"], timeout=0.2)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 15.0)

    def test_cancelled_command_carries_its_partial_result(self) -> None:
        runner = ProcessRunner()
        import threading
        threading.Timer(0.3, runner.cancel).start()
        with self.assertRaises(Cancelled) as raised:
            runner.run(["bash", "-c", "echo comecou; sleep 5"])
        self.assertIsNotNone(raised.exception.result)
        self.assertIn("comecou", raised.exception.result.output)

    def test_output_is_still_complete_for_a_normal_command(self) -> None:
        runner = ProcessRunner()
        result = runner.run(["bash", "-c", "for i in 1 2 3; do echo linha$i; done"],
                            timeout=10)
        self.assertTrue(result.ok)
        self.assertEqual(result.output.splitlines(), ["linha1", "linha2", "linha3"])


class TarExtractionTest(TemporaryDirectoryTest):
    """XR-06: nada do arquivo pode ser escrito fora do destino."""

    def _archive(self, members: list[tarfile.TarInfo], payload: bytes = b"x") -> Path:
        path = self.directory / "pacote.tar"
        with tarfile.open(path, "w") as tar:
            for member in members:
                if member.isfile():
                    member.size = len(payload)
                    tar.addfile(member, io.BytesIO(payload))
                else:
                    tar.addfile(member)
        return path

    def test_sibling_directory_with_common_prefix_is_refused(self) -> None:
        destination = self.directory / "odf"
        destination.mkdir()
        archive = self._archive([tarfile.TarInfo("../odf_extra/probe.txt")])
        with self.assertRaises(acquisition.ArchiveError):
            acquisition._safe_extract(archive, destination)
        self.assertFalse((self.directory / "odf_extra" / "probe.txt").exists())

    def test_link_pointing_outside_is_refused(self) -> None:
        destination = self.directory / "odf"
        destination.mkdir()
        link = tarfile.TarInfo("escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../fora"
        archive = self._archive([link])
        with self.assertRaises(acquisition.ArchiveError):
            acquisition._safe_extract(archive, destination)

    def test_regular_members_are_extracted(self) -> None:
        destination = self.directory / "odf"
        destination.mkdir()
        archive = self._archive([tarfile.TarInfo("0412_SUM.ASC")], payload=b"ok")
        acquisition._safe_extract(archive, destination)
        self.assertEqual((destination / "0412_SUM.ASC").read_bytes(), b"ok")


class PileupMeasurementTest(unittest.TestCase):
    """XR-07: sem medida não é o mesmo que limpo."""

    def _check(self, singles, doubles) -> PileupCheck:
        return PileupCheck(plot=Path("p.pdf"), rate_ct_s=1.0, frame_time_s=0.07,
                           singles=singles, doubles=doubles)

    def test_missing_ratios_are_unmeasured(self) -> None:
        self.assertEqual(self._check(None, None).verdict(), "unmeasured")

    def test_non_positive_uncertainty_is_unmeasured(self) -> None:
        self.assertEqual(self._check((1.0, 0.0), (1.0, 0.0)).verdict(), "unmeasured")

    def test_compatible_ratios_are_clean(self) -> None:
        self.assertEqual(self._check((0.99, 0.02), (1.02, 0.03)).verdict(), "clean")

    def test_singles_deficit_alone_is_suspicious(self) -> None:
        check = self._check((0.90, 0.01), (1.02, 0.03))
        self.assertTrue(check.suspicious())
        self.assertEqual(check.verdict(), "inconclusive")

    def test_core_wings_gradient_decides(self) -> None:
        check = self._check((0.95, 0.005), (1.10, 0.01))
        check.core = self._check((0.90, 0.01), (1.20, 0.02))
        check.wings = self._check((0.99, 0.01), (1.01, 0.02))
        self.assertEqual(check.verdict(), "pileup")
        check.core = self._check((0.95, 0.01), (1.10, 0.02))
        check.wings = self._check((0.95, 0.01), (1.10, 0.02))
        self.assertEqual(check.verdict(), "unexplained")



# ---------------------------------------------------------------------------
# Infraestrutura para os testes de estado, retomada e sessão
# ---------------------------------------------------------------------------

def write_events(path: Path, instrument: str = "EPN", exposure: str = "S003",
                 timeref: str = "LOCAL", count: int = 50, start: float = 1.0e8,
                 gti: list[tuple[float, float]] | None = None) -> Path:
    """Lista de eventos mínima, com os cartões que o SAS propaga."""
    import numpy as np
    from astropy.io import fits

    time = start + np.arange(count, dtype=float)
    columns = fits.ColDefs([
        fits.Column(name="TIME", format="D", array=time),
        fits.Column(name="PI", format="J", array=np.full(count, 500)),
        fits.Column(name="CCDNR", format="B", array=np.full(count, 4)),
    ])
    primary = fits.PrimaryHDU()
    for key, value in (("INSTRUME", instrument), ("EXPIDSTR", exposure),
                       ("DATAMODE", "IMAGING"), ("SUBMODE", "PrimeSmallWindow")):
        primary.header[key] = value
    events = fits.BinTableHDU.from_columns(columns, name="EVENTS")
    for key, value in (("INSTRUME", instrument), ("EXPIDSTR", exposure),
                       ("TIMEREF", timeref), ("TSTART", start),
                       ("TSTOP", start + count), ("LIVETIME", float(count)),
                       ("ONTIME", float(count))):
        events.header[key] = value
    hdus = [primary, events]
    if gti is not None:
        intervals = np.array(gti, dtype=float)
        hdus.append(fits.BinTableHDU.from_columns([
            fits.Column(name="START", format="D", array=intervals[:, 0]),
            fits.Column(name="STOP", format="D", array=intervals[:, 1])], name="STDGTI04"))
    fits.HDUList(hdus).writeto(path, overwrite=True)
    return path


class PipelineTest(TemporaryDirectoryTest):
    def pipeline(self):
        from xredux.config import Settings
        from xredux.pipeline import Pipeline
        from xredux.session import Session
        from xredux.tasks.base import TaskContext

        session = Session.load_or_create(self.directory, "0000000001", "alvo")
        context = TaskContext(runner=ProcessRunner(), env={}, session=session,
                              work_dir=self.directory)
        return Pipeline(Settings(work_dir=self.directory), session, context)

    def events(self, instrument="EPN", exposure="S003", mode="IMAGING"):
        from xredux.tasks.epic import EventList

        name = f"0000_0000000001_{instrument}_{exposure}_ImagingEvts.ds"
        path = write_events(self.directory / name, instrument, exposure)
        return EventList(path=path, instrument=instrument, mode=mode,
                         exposure_id=exposure, submode="PRIMESMALLWINDOW")


class StaleStateTest(PipelineTest):
    """XR-01: mudar uma entrada descarta tudo o que dependia dela."""

    def _with_products(self, pipeline) -> None:
        from xredux.tasks import regions

        state = pipeline.state
        state.source_region = regions.circle(100, 100, 30)
        state.background_region = regions.annulus(100, 100, 60, 120)
        state.clean_events = self.directory / "old_clean.fits"
        state.barycentered = self.directory / "old_bary.fits"
        state.source_event_list = self.directory / "old_source.fits"
        state.period_s = 7.0
        state.h_statistic = 40.0
        pipeline._remember_timing("z2_refine")

    def test_region_change_discards_what_was_extracted_with_the_old_one(self) -> None:
        from xredux.tasks import regions

        pipeline = self.pipeline()
        pipeline.select_events(self.events())
        self._with_products(pipeline)
        pipeline.set_regions(regions.circle(100, 100, 20), regions.annulus(100, 100, 60, 120))
        self.assertIsNone(pipeline.state.source_event_list)
        self.assertIsNone(pipeline.state.period_s)
        self.assertIsNone(pipeline.state.h_statistic)
        # A lista baricêntrica não depende da região e fica.
        self.assertIsNotNone(pipeline.state.barycentered)
        self.assertEqual(pipeline.session.steps["period_search"].status, "stale")
        self.assertEqual(pipeline.session.steps["period_search"].parameters, {})

    def test_same_regions_again_keep_the_products(self) -> None:
        pipeline = self.pipeline()
        pipeline.select_events(self.events())
        self._with_products(pipeline)
        state = pipeline.state
        pipeline.set_regions(state.source_region, state.background_region)
        self.assertEqual(state.period_s, 7.0)

    def test_new_filtering_discards_the_barycentred_list(self) -> None:
        from unittest.mock import patch

        import numpy as np

        from xredux.tasks import filtering

        pipeline = self.pipeline()
        pipeline.select_events(self.events())
        self._with_products(pipeline)
        pipeline.state.background_curve = filtering.BackgroundCurve(
            self.directory / "rate.fits", np.array([0.0]), np.array([0.1]), "EPN", 100.0)
        with patch.object(filtering, "make_gti", return_value=self.directory / "gti.fits"), \
                patch.object(filtering, "filter_events",
                             return_value=self.directory / "new_clean.fits"):
            pipeline.filter_flares(0.2)
        self.assertEqual(pipeline.state.clean_events, self.directory / "new_clean.fits")
        self.assertIsNone(pipeline.state.barycentered)
        self.assertIsNone(pipeline.state.source_event_list)
        self.assertIsNone(pipeline.state.period_s)

    def test_another_exposure_discards_everything_derived(self) -> None:
        pipeline = self.pipeline()
        pipeline.select_events(self.events())
        self._with_products(pipeline)
        pipeline.select_events(self.events("EMOS1", "S001"))
        state = pipeline.state
        self.assertIsNone(state.clean_events)
        self.assertIsNone(state.barycentered)
        self.assertIsNone(state.period_s)
        # Regiões no céu valem entre câmeras em modo de imagem.
        self.assertIsNotNone(state.source_region)

    def test_switching_to_timing_mode_drops_the_sky_regions(self) -> None:
        pipeline = self.pipeline()
        pipeline.select_events(self.events())
        self._with_products(pipeline)
        pipeline.select_events(self.events("EPN", "U002", mode="TIMING"))
        self.assertIsNone(pipeline.state.source_region)

    def test_new_light_curve_keeps_the_candidate_but_drops_its_statistics(self) -> None:
        from unittest.mock import patch

        import numpy as np

        from xredux.tasks import timing

        pipeline = self.pipeline()
        pipeline.select_events(self.events())
        self._with_products(pipeline)
        curve = timing.LightCurve(self.directory / "lc.fits", np.array([0.0]),
                                  np.array([1.0]), None, 1.0, (150, 1200))
        with patch.object(timing, "extract_light_curve", return_value=curve), \
                patch.object(timing, "correct_light_curve",
                             return_value=self.directory / "lc_corr.fits"):
            pipeline.light_curve(band_ev=(150, 1200))
        self.assertEqual(pipeline.state.period_s, 7.0)
        self.assertIsNone(pipeline.state.h_statistic)
        record = pipeline.session.steps["lightcurve"]
        self.assertTrue(record.parameters["background_subtracted"])


class SourceEventCacheTest(PipelineTest):
    """XR-02: a seleção da fonte é só espacial; a banda entra na leitura."""

    def test_selection_ignores_the_band_and_is_reused(self) -> None:
        from unittest.mock import patch

        from xredux.tasks import filtering, regions

        pipeline = self.pipeline()
        pipeline.select_events(self.events())
        pipeline.state.barycentered = write_events(self.directory / "bary.fits",
                                                   timeref="SOLARSYSTEM")
        pipeline.set_regions(regions.circle(100, 100, 30), regions.annulus(100, 100, 60, 120))
        calls = []

        def fake(context, events, table, region, band_ev=None, output=None):
            calls.append(band_ev)
            return write_events(self.directory / "source.fits", timeref="SOLARSYSTEM")

        with patch.object(filtering, "extract_region_events", side_effect=fake):
            pipeline.source_events(band_ev=(150, 1200))
            pipeline.source_events(band_ev=(150, 10_000))
        self.assertEqual(calls, [None])


class RestoreProvenanceTest(PipelineTest):
    """XR-03: só volta o que veio de etapa concluída e desta exposição."""

    def _prepared(self, timeref="SOLARSYSTEM", timing_done=True, exposure="S003"):
        pipeline = self.pipeline()
        events = self.events()
        pipeline.select_events(events)
        prefix = events.product_prefix
        write_events(self.directory / f"{prefix}_clean.fits", exposure=exposure)
        write_events(self.directory / f"{prefix}_clean_bary.fits", exposure=exposure,
                     timeref=timeref)
        session = pipeline.session
        session.begin("filtering")
        session.finish("filtering")
        session.begin("timing")
        if timing_done:
            session.finish("timing")
        else:
            session.fail("timing", "barycen interrompido")
        return events

    def test_valid_products_come_back(self) -> None:
        self._prepared()
        reopened = self.pipeline()
        reopened.restore()
        self.assertIsNotNone(reopened.state.clean_events)
        self.assertIsNotNone(reopened.state.barycentered)

    def test_uncorrected_copy_is_not_taken_for_a_barycentred_list(self) -> None:
        self._prepared(timeref="LOCAL")
        reopened = self.pipeline()
        reopened.restore()
        self.assertIsNone(reopened.state.barycentered)

    def test_failed_barycentring_is_not_restored(self) -> None:
        self._prepared(timing_done=False)
        reopened = self.pipeline()
        reopened.restore()
        self.assertIsNone(reopened.state.barycentered)

    def test_product_of_another_exposure_is_refused(self) -> None:
        self._prepared(exposure="U002")
        reopened = self.pipeline()
        reopened.restore()
        self.assertIsNone(reopened.state.clean_events)

    def test_interrupted_barycen_leaves_no_product_behind(self) -> None:
        from unittest.mock import patch

        from xredux.runner import CommandResult, TaskFailed
        from xredux.tasks import timing

        pipeline = self.pipeline()
        clean = write_events(self.directory / "epn_s003_clean.fits")
        failure = TaskFailed(CommandResult(["barycen"], 1, "", 0.0, "."))
        with patch.object(pipeline.context, "sas", side_effect=failure):
            with self.assertRaises(TaskFailed):
                timing.barycenter(pipeline.context, clean, 10.0, 20.0)
        self.assertFalse((self.directory / "epn_s003_clean_bary.fits").exists())
        self.assertEqual(list(self.directory.glob("*.parcial.fits")), [])

    def test_recorded_exposure_is_selected_again(self) -> None:
        pipeline = self.pipeline()
        pn, mos = self.events(), self.events("EMOS1", "S001")
        pipeline.select_events(mos)
        reopened = self.pipeline()
        reopened.restore()
        self.assertEqual(reopened.state.selected.instrument, "EMOS1")
        del pn


class RestoreParametersTest(PipelineTest):
    """XR-10: geometria, bin e limiar voltam como foram usados."""

    def test_region_geometry_survives(self) -> None:
        from xredux.tasks import regions

        pipeline = self.pipeline()
        pipeline.set_regions(regions.circle(100, 100, 30), regions.annulus(100, 100, 60, 120))
        reopened = self.pipeline()
        reopened.restore()
        self.assertIsNotNone(reopened.state.source_region.core())
        self.assertIsNotNone(reopened.state.source_region.excluding_core())

    def test_legacy_region_without_geometry_is_rebuilt_from_its_expression(self) -> None:
        from xredux.tasks import regions

        kind, geometry = regions.geometry_of("((X,Y) IN circle(24756.4,25352.9,600.0))")
        self.assertEqual(kind, "circle")
        self.assertEqual(geometry, {"x": 24756.4, "y": 25352.9, "radius": 600.0})
        kind, geometry = regions.geometry_of("((X,Y) IN annulus(1.0,2.0,3.0,4.0))")
        self.assertEqual(geometry["outer"], 4.0)
        self.assertEqual(regions.geometry_of("(RAWX IN [27:47])"), ("", {}))

        pipeline = self.pipeline()
        pipeline.session.schema = 1
        pipeline.session.begin("regions", {
            "source": "((X,Y) IN circle(100.0,100.0,600.0))",
            "background": "((X,Y) IN annulus(100.0,100.0,1200.0,2400.0))"})
        pipeline.session.finish("regions")
        reopened = self.pipeline()
        reopened.restore()
        self.assertIsNotNone(reopened.state.source_region.core())

    def test_background_bin_and_threshold_come_from_the_record(self) -> None:
        import numpy as np
        from astropy.io import fits

        pipeline = self.pipeline()
        events = self.events()
        pipeline.select_events(events)
        rate = fits.BinTableHDU.from_columns([
            fits.Column(name="TIME", format="D", array=np.arange(5.0) * 50),
            fits.Column(name="RATE", format="E", array=np.full(5, 0.1))], name="RATE")
        rate.header["TIMEDEL"] = 50.0
        primary = fits.PrimaryHDU()
        primary.header["INSTRUME"], primary.header["EXPIDSTR"] = "EPN", "S003"
        fits.HDUList([primary, rate]).writeto(
            self.directory / f"{events.product_prefix}_bkg_rate.fits")
        pipeline.session.begin("filtering", {"threshold": 0.27})
        pipeline.session.finish("filtering")

        reopened = self.pipeline()
        reopened.restore()
        self.assertEqual(reopened.state.background_curve.binsize_s, 50.0)
        self.assertEqual(reopened.state.threshold, 0.27)


class JournalTest(TemporaryDirectoryTest):
    """XR-09: o reproduce.sh segue a ordem real e não esconde nada."""

    def _session(self):
        from xredux.session import Session

        return Session(self.directory, "0000000001")

    def test_interleaved_steps_keep_their_order(self) -> None:
        session = self._session()
        runner = ProcessRunner()
        session.record_command("spectrum", runner.run(["echo", "A"]))
        session.record_command("response", runner.run(["echo", "B"]))
        session.record_command("spectrum", runner.run(["echo", "C"]))
        session.save()
        text = (self.directory / "reproduce.sh").read_text(encoding="utf-8")
        self.assertLess(text.index("echo A"), text.index("echo B"))
        self.assertLess(text.index("echo B"), text.index("echo C"))

    def test_redoing_a_step_keeps_the_earlier_attempt_in_the_journal(self) -> None:
        session = self._session()
        runner = ProcessRunner()
        session.begin("filtering")
        session.record_command("filtering", runner.run(["echo", "primeira"]))
        session.begin("filtering")
        session.record_command("filtering", runner.run(["echo", "segunda"]))
        session.save()
        text = (self.directory / "reproduce.sh").read_text(encoding="utf-8")
        self.assertIn("primeira", text)
        self.assertIn("segunda", text)

    def test_actions_and_failures_are_marked(self) -> None:
        session = self._session()
        session.record_action("timing", "cópia de trabalho", shell=["cp", "a", "b"])
        session.record_action("export", "CSV escrito pelo XreduX")
        session.record_command("x", ProcessRunner().run(["false"]))
        session.save()
        text = (self.directory / "reproduce.sh").read_text(encoding="utf-8")
        self.assertIn("( cd", text)
        self.assertIn("cp a b", text)
        self.assertIn("# [ação do XreduX] CSV escrito pelo XreduX", text)
        self.assertIn("# [falhou, código 1]", text)

    def test_cancelled_command_is_recorded(self) -> None:
        import threading

        from xredux.tasks.base import TaskContext

        session = self._session()
        runner = ProcessRunner()
        context = TaskContext(runner=runner, env={}, session=session, work_dir=self.directory)
        threading.Timer(0.2, runner.cancel).start()
        with self.assertRaises(Cancelled):
            context.run("efsearch", ["sleep", "5"])
        self.assertEqual(session.journal[-1]["command"], ["sleep", "5"])


class SessionPersistenceTest(TemporaryDirectoryTest):
    def test_save_leaves_no_temporary_file(self) -> None:
        from xredux.session import Session

        Session(self.directory, "0000000001").save()
        self.assertEqual([path.name for path in self.directory.iterdir()
                          if path.name.startswith(".")], [])

    def test_unreadable_session_is_kept_aside(self) -> None:
        from xredux.session import Session

        (self.directory / "session.json").write_text("{ truncado", encoding="utf-8")
        session = Session.load_or_create(self.directory, "0000000001")
        self.assertIsNotNone(session.recovered_from)
        self.assertEqual(session.recovered_from.read_text(encoding="utf-8"), "{ truncado")


class ArchiveIdentityTest(TemporaryDirectoryTest):
    """XR-12: fontes diferentes não dividem pasta nem descritor."""

    def test_names_with_the_same_slug_get_separate_folders(self) -> None:
        from xredux.archive import Archive

        archive = Archive(self.directory)
        first = archive.source_for("A B", 0.0, 0.0)
        second = archive.source_for("AB", 180.0, 0.0)
        self.assertNotEqual(first.directory, second.directory)
        self.assertEqual(Archive(self.directory).find(ra=0.0, dec=0.0).name, "A B")

    def test_nearby_sources_with_different_names_stay_apart(self) -> None:
        from xredux.archive import Archive

        archive = Archive(self.directory)
        first = archive.source_for("Fonte 1", 10.0, 0.0)
        second = archive.source_for("Fonte 2", 10.0 + 2.0 / 60, 0.0)
        self.assertNotEqual(first.directory, second.directory)

    def test_same_position_under_another_name_is_the_same_source(self) -> None:
        from xredux.archive import Archive

        archive = Archive(self.directory)
        first = archive.source_for("RBS1223", 197.2029, 21.4522)
        second = archive.source_for("RX J1308.6+2127", 197.2030, 21.4521)
        self.assertEqual(first.directory, second.directory)


class ProfileRawDirectoryTest(unittest.TestCase):
    """XR-13: cada perfil tem a sua pasta de respostas."""

    def test_two_profiles_of_one_observation_do_not_share_files(self) -> None:
        from xredux.export.profile import raw_subdirectory

        pn = raw_subdirectory("RX J1856.5-3754", "0412601301", "xmm_epn_x_0412601301")
        mos = raw_subdirectory("RX J1856.5-3754", "0412601301", "xmm_emos1_x_0412601301")
        self.assertNotEqual(pn, mos)
        self.assertTrue(pn.startswith("RXJ1856.5-3754/0412601301/"))


class PhaseExposureTest(TemporaryDirectoryTest):
    """Exposição de cada fatia de fase: a cobertura real das GTIs."""

    def test_whole_cycles_cover_every_phase_equally(self) -> None:
        import numpy as np

        from xredux.tasks import spectra

        path = write_events(self.directory / "e.fits", gti=[(100.0, 100.0 + 10 * 7.0)])
        fractions = spectra.phase_exposure_fractions(path, 1 / 7.0, 100.0,
                                                     np.linspace(0, 1, 5))
        self.assertTrue(np.allclose(fractions, 0.25))

    def test_partial_coverage_is_uneven(self) -> None:
        import numpy as np

        from xredux.tasks import spectra

        # Um ciclo inteiro e mais meio: a primeira metade da fase recebe mais tempo.
        path = write_events(self.directory / "e.fits", gti=[(0.0, 1.5 * 8.0)])
        fractions = spectra.phase_exposure_fractions(path, 1 / 8.0, 0.0,
                                                     np.array([0.0, 0.5, 1.0]))
        self.assertTrue(np.allclose(fractions, [2 / 3, 1 / 3]))


if __name__ == "__main__":
    unittest.main()
