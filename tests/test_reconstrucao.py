"""Testes das correções para a reconstrução dos produtos usados pelo MAGNUS.

Cinco frentes: referência temporal explícita (1), exposição por fase a partir
das GTIs (2), resolução temporal com procedência (3), registro do diagnóstico
de empilhamento (4) e identidade/manifesto do conjunto exportado (5). Nenhum
teste roda o SAS: as listas de eventos são sintéticas, com os cartões e as
extensões que o SAS produz.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from astropy.io import fits

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xredux.export import manifest as manifest_export  # noqa: E402
from xredux.export import pulsaris  # noqa: E402
from xredux.runner import ProcessRunner  # noqa: E402
from xredux.tasks import spectra  # noqa: E402
from xredux.tasks.epic import EventList, PileupCheck, UnknownTimeResolution  # noqa: E402
from xredux.timebase import (TimeReferenceError, reference_from_events,  # noqa: E402
                             sha256)

from test_export import write_rmf, write_spectrum  # noqa: E402

T0 = 2.66e8


def write_list(path: Path, times, *, pi=None, ccd=4, tstart=T0, tstop=None,
               timesys="TDB", timeref="SOLARSYSTEM", instrument="EPN", exposure="S003",
               submode="PrimeLargeWindow", frmtime=48, stdgti=None, filter_gti=None,
               ontime=None, livetime=None, frames=None) -> Path:
    """Lista de eventos com os cartões e as GTIs por CCD de uma lista do pn.

    ``stdgti`` e ``filter_gti`` mapeiam CCD → intervalos; a GTI de filtragem
    do CCD ``n`` sai como ``GTI00{n-1}05`` com ``CCDID = n``, como no SAS.
    """
    times = np.asarray(times, dtype=float)
    pi = np.full(times.size, 500.0) if pi is None else np.asarray(pi, dtype=float)
    primary = fits.PrimaryHDU()
    for key, value in (("INSTRUME", instrument), ("EXPIDSTR", exposure),
                       ("DATAMODE", "IMAGING"), ("SUBMODE", submode), ("FILTER", "Thin1")):
        primary.header[key] = value
    events = fits.BinTableHDU.from_columns([
        fits.Column(name="TIME", format="D", array=times),
        fits.Column(name="PI", format="E", array=pi),
        fits.Column(name="CCDNR", format="B", array=np.full(times.size, ccd))], name="EVENTS")
    header = events.header
    for key, value in (("INSTRUME", instrument), ("EXPIDSTR", exposure),
                       ("TIMESYS", timesys), ("TIMEREF", timeref), ("MJDREF", 50814.0),
                       ("TSTART", tstart), ("TSTOP", tstop or tstart + 1000.0),
                       ("FRMTIME", frmtime)):
        header[key] = value
    for chip, value in (ontime or {}).items():
        header[f"ONTIME{chip:02d}"] = value
    for chip, value in (livetime or {}).items():
        header[f"LIVETI{chip:02d}"] = value
    if livetime:
        header["LIVETIME"] = livetime.get(ccd, next(iter(livetime.values())))
        header["ONTIME"] = (ontime or {}).get(ccd, header["LIVETIME"])
    hdus = [primary, events]

    def gti(name, intervals, chip=None):
        intervals = np.asarray(intervals, dtype=float).reshape(-1, 2)
        hdu = fits.BinTableHDU.from_columns([
            fits.Column(name="START", format="D", array=intervals[:, 0]),
            fits.Column(name="STOP", format="D", array=intervals[:, 1])], name=name)
        hdu.header["TIMESYS"] = timesys
        if chip is not None:
            hdu.header["CCDID"] = chip
        return hdu

    for chip, intervals in (stdgti or {}).items():
        hdus.append(gti(f"STDGTI{chip:02d}", intervals))
    for chip, intervals in (filter_gti or {}).items():
        hdus.append(gti(f"GTI{chip - 1:03d}05", intervals, chip))
    # Tempo vivo quadro a quadro, como na EXPOSUnn do SAS: (inícios, FRACEXP, TIMEDEL).
    for chip, (starts, fraction, timedel) in (frames or {}).items():
        hdu = fits.BinTableHDU.from_columns([
            fits.Column(name="TIME", format="D", array=np.asarray(starts, dtype=float)),
            fits.Column(name="FRACEXP", format="E", array=np.asarray(fraction, dtype=float))],
            name=f"EXPOSU{chip:02d}")
        hdu.header["TIMEDEL"] = timedel
        hdu.header["TIMESYS"] = timesys
        hdus.append(hdu)
    fits.HDUList(hdus).writeto(path, overwrite=True)
    return path


class TemporaryDirectoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()


def rows_of(path: Path) -> np.ndarray:
    """Linhas numéricas de uma tabela CSV com cabeçalho em ``#`` e nomes de coluna."""
    lines = [line for line in path.read_text().splitlines() if not line.startswith("#")]
    return np.array([[float(item) for item in line.split(",")] for line in lines[1:]])


def metadata_of(path: Path) -> dict[str, str]:
    return dict(line[2:].split("=", 1) for line in path.read_text().splitlines()
                if line.startswith("# ") and "=" in line)


# ---------------------------------------------------------------------------
# 1. Referência temporal
# ---------------------------------------------------------------------------

class TimeReferenceTest(TemporaryDirectoryTest):
    """A origem vem da lista inteira e não muda com banda, região ou desbaste."""

    def setUp(self) -> None:
        super().setUp()
        generator = np.random.default_rng(3)
        times = np.sort(generator.uniform(T0 + 200.0, T0 + 1000.0, 4000))
        energy = generator.uniform(150.0, 3000.0, times.size)
        self.parent = write_list(self.directory / "bary.fits", times, pi=energy)
        # Duas "regiões": subconjuntos da mesma lista, com o mesmo cabeçalho.
        self.region_a = write_list(self.directory / "a.fits", times[::2], pi=energy[::2])
        self.region_b = write_list(self.directory / "b.fits", times[1::3], pi=energy[1::3])
        self.rmf = self.directory / "r.rmf"
        write_rmf(self.rmf)
        self.reference = reference_from_events(self.parent)

    def export(self, source, name, **overrides):
        parameters = dict(instrument="xmm_epn", time_resolution_us=47_700.0, rmf=self.rmf,
                          period_s=10.0, band_ev=(150, 1200),
                          time_reference=self.reference)
        parameters.update(overrides)
        return pulsaris.write(source, self.directory / name, **parameters)

    def test_band_region_and_sampling_keep_the_origin(self) -> None:
        reports = [
            self.export(self.region_a, "1.csv"),
            self.export(self.region_a, "2.csv", band_ev=(150, 3000)),
            self.export(self.region_b, "3.csv"),
            self.export(self.region_a, "4.csv", max_events=300, seed=9),
        ]
        keys = ("time_origin_s", "time_origin_file_sha256", "timesys", "timeref", "mjdref",
                "timezero", "timeunit", "phase_epoch_mission_s", "phase_epoch_mjd")
        headers = [metadata_of(report.path) for report in reports]
        for key in keys:
            self.assertEqual(len({header[key] for header in headers}), 1, key)
        self.assertTrue(reports[3].decimated)
        self.assertEqual(float(headers[0]["time_origin_s"]), T0)
        self.assertEqual(headers[0]["time_origin_file_sha256"], sha256(self.parent))
        # A origem não é o primeiro evento (que fica ~200 s depois do TSTART).
        self.assertGreater(rows_of(reports[0].path)[:, 0].min(), 199.0)

    def test_header_carries_the_full_reference(self) -> None:
        header = metadata_of(self.export(self.region_a, "x.csv").path)
        self.assertEqual(header["timesys"], "TDB")
        self.assertEqual(header["timeref"], "SOLARSYSTEM")
        self.assertEqual(header["timeunit"], "s")
        self.assertEqual(float(header["mjdref"]), 50814.0)
        self.assertEqual(float(header["timezero"]), 0.0)
        self.assertEqual(header["time_origin_file"], "bary.fits")
        self.assertIn("TSTART", header["time_origin_from"])
        self.assertAlmostEqual(float(header["time_origin_mjd"]), 50814.0 + T0 / 86400.0,
                               places=9)
        # Época e período separados da origem.
        self.assertEqual(header["period_s"], "10")
        self.assertEqual(float(header["phase_reference_s"]), 0.0)

    def test_different_scale_is_refused(self) -> None:
        tt = write_list(self.directory / "tt.fits", [T0 + 10.0, T0 + 20.0],
                        timesys="TT", timeref="LOCAL")
        with self.assertRaises(TimeReferenceError):
            self.export(tt, "tt.csv")
        self.assertFalse((self.directory / "tt.csv").exists())

    def test_missing_timesys_is_an_error(self) -> None:
        path = write_list(self.directory / "nada.fits", [T0 + 1.0], timesys="")
        with fits.open(path, mode="update") as hdus:
            del hdus["EVENTS"].header["TIMESYS"]
        with self.assertRaises(TimeReferenceError):
            reference_from_events(path)


# ---------------------------------------------------------------------------
# 2. Exposição por fase a partir das GTIs
# ---------------------------------------------------------------------------

class GoodTimeSelectionTest(TemporaryDirectoryTest):
    """GTI do CCD da fonte: a STDGTI dele e a GTI de filtragem dele, só."""

    def test_other_ccds_do_not_remove_source_time(self) -> None:
        path = write_list(self.directory / "e.fits", [T0 + 1.0],
                          stdgti={4: [(T0, T0 + 1000)], 5: [(T0, T0 + 1000)]},
                          filter_gti={4: [(T0, T0 + 600), (T0 + 700, T0 + 1000)],
                                      5: [(T0, T0 + 100)]})
        good = spectra.source_gti(path, 4)
        self.assertEqual(good.extensions, ["STDGTI04", "GTI00305"])
        self.assertAlmostEqual(good.total_s, 900.0)
        self.assertEqual(good.timesys, "TDB")

    def test_overlaps_count_once(self) -> None:
        path = write_list(self.directory / "e.fits", [T0 + 1.0],
                          stdgti={4: [(T0, T0 + 500), (T0 + 400, T0 + 800),
                                      (T0 + 800, T0 + 900)]},
                          filter_gti={4: [(T0, T0 + 1000)]})
        good = spectra.source_gti(path, 4)
        self.assertEqual(good.intervals.tolist(), [[T0, T0 + 900]])

    def test_unknown_ccd_is_not_guessed(self) -> None:
        path = write_list(self.directory / "e.fits", [T0 + 1.0],
                          stdgti={4: [(T0, T0 + 10)], 5: [(T0, T0 + 10)]})
        with self.assertRaises(ValueError):
            spectra.source_gti(path, None)
        with self.assertRaises(ValueError):
            spectra.source_gti(path, 7)


class PhaseCoverageTest(unittest.TestCase):
    """Solução conhecida, não uniforme, com sobreposição e passagem pela fase zero."""

    EDGES = np.linspace(0.0, 1.0, 5)
    # P = 10 s, época 0. [0, 2.5): bin 0. [7.5, 12.5): bin 3 e bin 0, cruzando
    # a fase zero. [21, 24) e [22, 23) sobrepostos: 0.1–0.4, 1.5 s no bin 0 e
    # 1.5 s no bin 1. [35, 36): bin 2, 1 s.
    INTERVALS = np.array([(0.0, 2.5), (7.5, 12.5), (21.0, 24.0), (22.0, 23.0),
                          (35.0, 36.0)])
    EXPECTED = np.array([6.5, 1.5, 1.0, 2.5])

    def test_known_solution(self) -> None:
        coverage = spectra.phase_coverage_s(self.INTERVALS, 10.0, 0.0, self.EDGES)
        self.assertTrue(np.allclose(coverage, self.EXPECTED), coverage)

    def test_epoch_after_the_intervals(self) -> None:
        # Época deslocada de meio período: os bins trocam de metade.
        coverage = spectra.phase_coverage_s(self.INTERVALS, 10.0, 105.0, self.EDGES)
        self.assertTrue(np.allclose(coverage, np.roll(self.EXPECTED, 2)), coverage)

    def test_matches_dense_sampling(self) -> None:
        generator = np.random.default_rng(11)
        starts = np.sort(generator.uniform(-300.0, 300.0, 12))
        intervals = np.column_stack([starts, starts + generator.uniform(0.3, 40.0, 12)])
        period, epoch = 7.3, -12.4
        edges = np.linspace(0.0, 1.0, 9)
        coverage = spectra.phase_coverage_s(intervals, period, epoch, edges)
        step = 1e-3
        merged = spectra.merge_intervals(intervals)
        samples = np.concatenate([np.arange(a, b, step) + step / 2 for a, b in merged])
        phase = np.mod((samples - epoch) / period, 1.0)
        dense = np.histogram(phase, bins=edges)[0] * step
        self.assertTrue(np.allclose(coverage, dense, atol=5 * step), coverage - dense)
        self.assertAlmostEqual(coverage.sum(), np.sum(merged[:, 1] - merged[:, 0]), places=9)


class PhaseExposureTableTest(TemporaryDirectoryTest):
    """A tabela exportada: soma, tempo morto aplicado uma vez, desbaste."""

    def setUp(self) -> None:
        super().setUp()
        origin = T0
        intervals = PhaseCoverageTest.INTERVALS + origin
        self.path = write_list(self.directory / "e.fits", [origin + 1.0], tstart=origin,
                               stdgti={4: intervals}, filter_gti={4: [(origin, origin + 100)]})
        self.good = spectra.source_gti(self.path, 4)
        self.reference = reference_from_events(self.path)

    def table(self, **overrides):
        parameters = dict(good_time=self.good, reference=self.reference, period_s=10.0,
                          phase_reference_s=0.0, exposure_total_s=0.9 * 11.5,
                          ontime_s=11.5, livetime_s=0.9 * 11.5, bins=4)
        parameters.update(overrides)
        return pulsaris.write_phase_exposure(self.directory / "fase.csv", **parameters)

    def test_sum_reproduces_the_effective_exposure(self) -> None:
        table = self.table()
        self.assertTrue(np.allclose(table.gti_s, PhaseCoverageTest.EXPECTED))
        self.assertTrue(np.allclose(table.exposure_s, 0.9 * PhaseCoverageTest.EXPECTED))
        self.assertAlmostEqual(table.exposure_s.sum(), 0.9 * 11.5, places=9)
        rows = rows_of(table.path)
        self.assertTrue(np.allclose(rows[:, 5].sum(), 0.9 * 11.5, atol=1e-5))
        self.assertEqual(table.method, "gti_constant_live_fraction")
        header = metadata_of(table.path)
        for key in ("recipe", "approximation", "live_fraction", "gti_vs_ontime_relative",
                    "time_origin_s", "timesys", "period_s", "phase_reference_s"):
            self.assertIn(key, header)

    def test_decimation_scales_the_exposure(self) -> None:
        table = self.table(exposure_total_s=0.5 * 0.9 * 11.5, decimation_probability=0.5)
        self.assertTrue(np.allclose(table.exposure_s, 0.45 * PhaseCoverageTest.EXPECTED))

    def test_double_dead_time_correction_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.table(exposure_total_s=0.9 * 0.9 * 11.5)

    def test_gti_that_does_not_match_ontime_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.table(ontime_s=20.0, livetime_s=18.0, exposure_total_s=18.0)

    def test_gti_in_another_scale_is_refused(self) -> None:
        other = spectra.GoodTime(intervals=self.good.intervals, ccd=4,
                                 extensions=["STDGTI04"], timesys="TT")
        with self.assertRaises(ValueError):
            self.table(good_time=other)


class FrameLivetimeTest(TemporaryDirectoryTest):
    """Tempo vivo quadro a quadro: FRACEXP que varia com a fase muda a exposição."""

    def setUp(self) -> None:
        super().setUp()
        # Quadros de 1 s (integração 0,9 s) de 0 a 100 s; P = 10 s. Os quadros
        # na primeira metade da fase têm FRACEXP 1,0, os da segunda, 0,5.
        starts = np.arange(100.0)
        fraction = np.where(np.mod(starts, 10.0) < 5.0, 1.0, 0.5)
        self.live = float(np.sum(fraction * 0.9))  # 67,5 s
        self.path = write_list(self.directory / "e.fits", [T0 + 1.0], tstart=T0,
                               stdgti={4: [(T0, T0 + 100.0)]},
                               filter_gti={4: [(T0, T0 + 100.0)]},
                               frames={4: (T0 + starts, fraction, 0.9)})
        self.good = spectra.source_gti(self.path, 4)
        self.reference = reference_from_events(self.path)

    def table(self, **overrides):
        parameters = dict(good_time=self.good, reference=self.reference, period_s=10.0,
                          phase_reference_s=0.0, exposure_total_s=self.live,
                          ontime_s=100.0, livetime_s=self.live, bins=2,
                          frames=spectra.frame_exposure(self.path, 4))
        parameters.update(overrides)
        return pulsaris.write_phase_exposure(self.directory / "fase.csv", **parameters)

    def test_frames_are_read_with_their_cycle(self) -> None:
        frames = spectra.frame_exposure(self.path, 4)
        self.assertAlmostEqual(frames.cycle_s, 1.0)
        self.assertAlmostEqual(frames.live_s.sum(), self.live)
        self.assertIsNone(spectra.frame_exposure(self.path, 5))

    def test_known_solution(self) -> None:
        table = self.table()
        self.assertEqual(table.method, "frame_livetime")
        self.assertTrue(np.allclose(table.frame_live_s, [45.0, 22.5]), table.frame_live_s)
        self.assertTrue(np.allclose(table.exposure_s, [45.0, 22.5]))
        # A receita da GTI, que supõe fração viva constante, divide meio a meio.
        self.assertTrue(np.allclose(table.exposure_gti_s, [33.75, 33.75]))
        rows = rows_of(table.path)
        self.assertTrue(np.allclose(rows[:, 3], [45.0, 22.5]))
        self.assertTrue(np.allclose(rows[:, 4], [33.75, 33.75]))
        self.assertTrue(np.allclose(rows[:, 5], [45.0, 22.5]))
        header = metadata_of(table.path)
        self.assertEqual(header["method"], "frame_livetime")
        self.assertEqual(header["frame_extension"], "EXPOSU04")

    def test_decimation_keeps_the_shape(self) -> None:
        table = self.table(exposure_total_s=0.5 * self.live, decimation_probability=0.5)
        self.assertTrue(np.allclose(table.exposure_s, [22.5, 11.25]))

    def test_gti_cuts_frames(self) -> None:
        # Sem os primeiros 20 s, os quadros cortados saem das duas metades.
        path = write_list(self.directory / "c.fits", [T0 + 1.0], tstart=T0,
                          stdgti={4: [(T0 + 20.0, T0 + 100.0)]},
                          filter_gti={4: [(T0, T0 + 100.0)]},
                          frames={4: (T0 + np.arange(100.0),
                                      np.where(np.mod(np.arange(100.0), 10.0) < 5.0, 1.0, 0.5),
                                      0.9)})
        good = spectra.source_gti(path, 4)
        table = self.table(good_time=good, ontime_s=80.0, livetime_s=54.0,
                           exposure_total_s=54.0, frames=spectra.frame_exposure(path, 4))
        self.assertTrue(np.allclose(table.frame_live_s, [36.0, 18.0]))

    def test_livetime_that_does_not_match_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.table(livetime_s=80.0, exposure_total_s=80.0)


# ---------------------------------------------------------------------------
# 3. Resolução temporal
# ---------------------------------------------------------------------------

class TimeResolutionTest(TemporaryDirectoryTest):
    def test_submode_and_frame_time_agree(self) -> None:
        events = EventList(Path("lista.ds"), "EPN", "IMAGING", submode="PrimeLargeWindow",
                           frame_time_ms=48.0)
        resolution = events.time_resolution()
        self.assertEqual(resolution.value_us, 47_700.0)
        self.assertIn("SUBMODE=PRIMELARGEWINDOW", resolution.provenance)
        self.assertIn("FRMTIME=48", resolution.provenance)
        self.assertEqual(resolution.metadata()["time_resolution_unit"], "microsecond")

    def test_frame_time_contradiction_is_an_error(self) -> None:
        events = EventList(Path("lista.ds"), "EPN", "IMAGING", submode="PrimeLargeWindow",
                           frame_time_ms=73.0)
        with self.assertRaises(UnknownTimeResolution):
            events.time_resolution()

    def test_discover_reads_the_frame_time(self) -> None:
        from xredux.tasks import epic

        write_list(self.directory / "1190_0402850301_EPN_S003_ImagingEvts.ds", [T0 + 1.0])
        found = epic.discover(self.directory)
        self.assertEqual(found[0].frame_time_ms, 48.0)
        self.assertEqual(found[0].time_resolution_us(), 47_700.0)

    def test_zero_or_missing_resolution_is_not_exported(self) -> None:
        path = write_list(self.directory / "e.fits", [T0 + 1.0, T0 + 2.0])
        rmf = self.directory / "r.rmf"
        write_rmf(rmf)
        for value in (0.0, None, -1.0, float("nan")):
            with self.assertRaises(ValueError):
                pulsaris.write(path, self.directory / "x.csv", instrument="x",
                               time_resolution_us=value, rmf=rmf)
        self.assertFalse((self.directory / "x.csv").exists())


# ---------------------------------------------------------------------------
# 4 e 5. Empilhamento registrado; conjunto exportado com manifesto
# ---------------------------------------------------------------------------

class ExportFixture(TemporaryDirectoryTest):
    """Uma redução sintética completa até os espectros, sem SAS."""

    LIVE = 0.95

    def pipeline(self, exposure="S003", work=None):
        from xredux.config import Settings
        from xredux.pipeline import Pipeline
        from xredux.session import Session
        from xredux.tasks.base import TaskContext
        from xredux.tasks.regions import annulus, circle

        work = work or self.directory
        session = Session.load_or_create(work, "0402850301", "RX J1308.6+2127")
        context = TaskContext(runner=ProcessRunner(), env={}, session=session, work_dir=work)
        pipeline = Pipeline(Settings(work_dir=work), session, context)
        state = pipeline.state
        state.target, state.ra, state.dec = "RX J1308.6+2127", 197.20292, 21.45222

        generator = np.random.default_rng(5)
        times = np.sort(generator.uniform(T0 + 150.0, T0 + 1000.0, 3000))
        energy = generator.uniform(150.0, 1500.0, times.size)
        stdgti = {4: [(T0, T0 + 1000.0)], 5: [(T0, T0 + 1000.0)]}
        filter_gti = {4: [(T0, T0 + 400.0), (T0 + 500.0, T0 + 1000.0)], 5: [(T0, T0 + 50.0)]}
        ontime = {4: 900.05, 5: 50.0}
        livetime = {4: self.LIVE * 900.05, 5: self.LIVE * 50.0}
        common = dict(exposure=exposure, stdgti=stdgti, filter_gti=filter_gti,
                      ontime=ontime, livetime=livetime)
        raw = write_list(work / f"1190_0402850301_EPN_{exposure}_ImagingEvts.ds", times,
                         pi=energy, timesys="TT", timeref="LOCAL", **common)
        prefix = f"epn_{exposure.lower()}"
        bary = write_list(work / f"{prefix}_clean_bary.fits", times, pi=energy, **common)
        source = write_list(work / f"{prefix}_source.fits", times[::2], pi=energy[::2],
                            **common)
        gti = write_list(work / f"{prefix}_gti.fits", [T0], timesys="TT", timeref="LOCAL")
        rmf, arf = work / f"{prefix}_src.rmf", work / f"{prefix}_src.arf"
        write_rmf(rmf)
        fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU.from_columns([
            fits.Column(name="ENERG_LO", format="E", array=[0.1]),
            fits.Column(name="ENERG_HI", format="E", array=[0.2]),
            fits.Column(name="SPECRESP", format="E", array=[100.0])], name="SPECRESP")]
        ).writeto(arf)
        src_spec, bkg_spec = work / f"{prefix}_src_spec.fits", work / f"{prefix}_bkg_spec.fits"
        write_spectrum(src_spec, np.full(400, 5), backscal=1.0, exposure=self.LIVE * 900.05)
        write_spectrum(bkg_spec, np.full(400, 2), backscal=4.0, exposure=self.LIVE * 900.05)

        events = EventList(path=raw, instrument="EPN", mode="IMAGING", exposure_id=exposure,
                           submode="PRIMELARGEWINDOW", filter_name="Thin1",
                           frame_time_ms=48.0)
        state.selected = events
        state.clean_events, state.gti, state.barycentered = bary, gti, bary
        pipeline.set_regions(circle(100.0, 100.0, 600.0), annulus(100.0, 100.0, 1200.0, 2400.0))
        state.source_spectrum = spectra.Spectrum(path=src_spec, instrument="EPN",
                                                 background=bkg_spec, rmf=rmf, arf=arf)
        state.background_spectrum = spectra.Spectrum(path=bkg_spec, instrument="EPN",
                                                     kind="background")
        state.period_s = 10.3127
        self.source = source
        return pipeline

    def export(self, pipeline, **overrides):
        parameters = dict(band_ev=(150, 1200), galactic_column=False)
        parameters.update(overrides)
        with patch.object(pipeline, "source_events", return_value=self.source):
            return pipeline.export_products(**parameters)


class ExportSetTest(ExportFixture):
    def test_set_is_named_by_observation_camera_and_exposure(self) -> None:
        first = self.export(self.pipeline("S003"))
        second = self.export(self.pipeline("U002"))
        self.assertNotEqual(first.set_id, second.set_id)
        self.assertIn("0402850301", first.set_id)
        self.assertTrue(first.set_id.endswith("_epn_s003"))
        self.assertTrue(first.profile_id.endswith("_s003"))
        self.assertTrue(first.report.path.is_file() and second.report.path.is_file())
        self.assertNotEqual(first.report.path, second.report.path)

    def test_csv_carries_reference_resolution_and_exposure(self) -> None:
        exported = self.export(self.pipeline())
        header = metadata_of(exported.report.path)
        self.assertEqual(float(header["time_origin_s"]), T0)
        self.assertEqual(header["time_origin_file"], "epn_s003_clean_bary.fits")
        self.assertEqual(header["timesys"], "TDB")
        self.assertEqual(header["time_resolution_us"], "47700")
        self.assertIn("FRMTIME=48", header["time_resolution_from"])
        self.assertAlmostEqual(float(header["exposure_s"]), self.LIVE * 900.05, places=5)
        self.assertEqual(header["source_ccd"], "4")
        self.assertEqual(header["phase_exposure_file"], exported.phase.path.name)
        phase = metadata_of(exported.phase.path)
        self.assertEqual(phase["gti_extensions"], "STDGTI04,GTI00305")
        self.assertAlmostEqual(float(phase["gti_total_s"]), 900.0, places=6)
        self.assertAlmostEqual(exported.phase.exposure_s.sum(), float(header["exposure_s"]),
                               places=5)

    def test_manifest_links_every_file_by_hash(self) -> None:
        exported = self.export(self.pipeline())
        document = json.loads(exported.manifest.read_text())
        roles = {entry["role"]: entry for entry in document["files"]}
        for role in ("events_raw", "gti_flare_filter", "events_barycentered",
                     "events_source_region", "spectrum_source", "spectrum_background",
                     "rmf", "arf", "csv_events", "csv_phase_exposure", "gti", "background",
                     "rmf_copy", "arf_copy", "regions"):
            self.assertIn(role, roles)
            self.assertEqual(roles[role]["sha256"], sha256(Path(roles[role]["path"])), role)
        self.assertEqual(document["time_reference"]["source_sha256"],
                         roles["events_barycentered"]["sha256"])
        self.assertEqual(document["identity"]["exposure_id"], "S003")
        self.assertEqual(document["pileup"]["status"], "not_run")
        self.assertTrue(document["checks"]["phase_exposure_sum_equals_total"])
        self.assertEqual(manifest_export.verify(exported.manifest), [])

    def test_region_change_invalidates_the_export(self) -> None:
        from xredux.tasks.regions import annulus, circle

        pipeline = self.pipeline()
        self.export(pipeline)
        self.assertTrue(pipeline.session.is_done("export"))
        pipeline.set_regions(circle(100.0, 100.0, 500.0), annulus(100.0, 100.0, 1000.0, 2000.0))
        self.assertEqual(pipeline.session.steps["export"].status, "stale")
        self.assertIsNone(pipeline.state.exported_csv)

    def test_period_change_invalidates_the_export(self) -> None:
        pipeline = self.pipeline()
        self.export(pipeline)
        pipeline.state.period_s = 10.3128
        pipeline._remember_timing("z2_refine")
        self.assertEqual(pipeline.session.steps["export"].status, "stale")

    def test_reopening_detects_a_changed_export(self) -> None:
        from xredux.config import Settings
        from xredux.pipeline import Pipeline
        from xredux.session import Session
        from xredux.tasks.base import TaskContext

        pipeline = self.pipeline()
        exported = self.export(pipeline)

        def reopen():
            session = Session.load_or_create(self.directory, "0402850301")
            context = TaskContext(runner=ProcessRunner(), env={}, session=session,
                                  work_dir=self.directory)
            again = Pipeline(Settings(work_dir=self.directory), session, context)
            return again, again.restore()

        _, restored = reopen()
        self.assertIn("exportação conferida pelo manifesto", restored)
        with exported.report.path.open("a") as stream:
            stream.write("0.0,1,0.5\n")
        again, restored = reopen()
        self.assertEqual(again.session.steps["export"].status, "stale")

    def test_unknown_resolution_blocks_the_export(self) -> None:
        pipeline = self.pipeline()
        pipeline.state.selected.submode = ""
        with self.assertRaises(UnknownTimeResolution):
            self.export(pipeline)
        self.assertFalse(list(self.directory.glob("pulsaris/*_events.csv")))


class PileupRecordTest(ExportFixture):
    def check(self, singles, doubles):
        return PileupCheck(plot=self.directory / "p.pdf", rate_ct_s=1.0, frame_time_s=0.0477,
                           singles=singles, doubles=doubles)

    def run_check(self, pipeline, result):
        with patch("xredux.pipeline.epic.check_pileup", return_value=result) as mocked:
            pipeline.check_pileup()
        return mocked

    def test_measured_clean_is_recorded_with_selection(self) -> None:
        pipeline = self.pipeline()
        mocked = self.run_check(pipeline, self.check((1.0, 0.01), (1.0, 0.02)))
        self.assertEqual(mocked.call_args.kwargs["gti"], pipeline.state.gti)
        record = pipeline.session.steps["pileup"]
        self.assertEqual(record.status, "done")
        self.assertEqual(record.parameters["outcome"], "measured")
        self.assertEqual(record.parameters["verdict"], "clean")
        self.assertEqual(record.parameters["region"], pipeline.state.source_region.expression)
        self.assertEqual(record.parameters["gti"], str(pipeline.state.gti))

    def test_unmeasured_is_never_clean(self) -> None:
        pipeline = self.pipeline()
        unmeasured = self.check(None, None)
        unmeasured.raw_tail = "sigmaTooLarge"
        self.run_check(pipeline, unmeasured)
        record = pipeline.session.steps["pileup"]
        self.assertEqual(record.parameters["outcome"], "unmeasured")
        self.assertEqual(record.parameters["verdict"], "unmeasured")
        self.assertEqual(record.parameters["result"]["raw_tail"], "sigmaTooLarge")

    def test_excess_without_core_test_is_inconclusive(self) -> None:
        pipeline = self.pipeline()
        self.run_check(pipeline, self.check((0.9, 0.01), (1.2, 0.02)))
        self.assertEqual(pipeline.session.steps["pileup"].parameters["outcome"],
                         "inconclusive")

    def test_failure_is_recorded_as_failure(self) -> None:
        pipeline = self.pipeline()
        with patch("xredux.pipeline.epic.check_pileup", side_effect=RuntimeError("epatplot")):
            with self.assertRaises(RuntimeError):
                pipeline.check_pileup()
        self.assertEqual(pipeline.session.steps["pileup"].status, "failed")
        self.assertIsNone(pipeline.state.pileup)

    def test_restore_brings_the_measurement_back(self) -> None:
        pipeline = self.pipeline()
        self.run_check(pipeline, self.check((1.0, 0.01), (1.0, 0.02)))
        pipeline.state.pileup = None
        pipeline._restore_pileup()
        self.assertIsNotNone(pipeline.state.pileup)
        self.assertEqual(pipeline.state.pileup.verdict(), "clean")

    def test_region_change_invalidates_the_measurement(self) -> None:
        from xredux.tasks.regions import annulus, circle

        pipeline = self.pipeline()
        self.run_check(pipeline, self.check((1.0, 0.01), (1.0, 0.02)))
        pipeline.set_regions(circle(100.0, 100.0, 500.0), annulus(100.0, 100.0, 1000.0, 2000.0))
        self.assertEqual(pipeline.session.steps["pileup"].status, "stale")
        self.assertIsNone(pipeline.state.pileup)

    def test_manifest_carries_the_pileup_record(self) -> None:
        pipeline = self.pipeline()
        self.run_check(pipeline, self.check((1.0, 0.01), (1.0, 0.02)))
        document = json.loads(self.export(pipeline).manifest.read_text())
        self.assertEqual(document["pileup"]["status"], "done")
        self.assertEqual(document["pileup"]["verdict"], "clean")


class RandomSeedTest(TemporaryDirectoryTest):
    """Sem SAS_RAND_SEED o SAS semeia pelo relógio e o epproc sorteia diferente."""

    def environment(self, variables=None):
        from xredux.env import SasEnvironment

        return SasEnvironment(variables=dict(variables or {}), sas_dir=self.directory,
                              headas=self.directory, ccf_path=self.directory)

    def test_seed_comes_from_the_obsid(self) -> None:
        from xredux.env import rand_seed_for

        self.assertEqual(rand_seed_for("0402850301"), 402850301)
        self.assertEqual(rand_seed_for("0402850301"), rand_seed_for("0402850301"))
        self.assertNotEqual(rand_seed_for("0402850301"), rand_seed_for("0402850401"))

    def test_seed_goes_to_the_environment(self) -> None:
        variables = self.environment().for_observation(self.directory, rand_seed=42)
        self.assertEqual(variables["SAS_RAND_SEED"], "42")

    def test_user_seed_prevails(self) -> None:
        variables = self.environment({"SAS_RAND_SEED": "7"}).for_observation(
            self.directory, rand_seed=42)
        self.assertEqual(variables["SAS_RAND_SEED"], "7")

    def test_session_keeps_the_seed_for_the_script(self) -> None:
        from xredux.session import Session

        session = Session(self.directory, "0402850301")
        session.environment["SAS_RAND_SEED"] = "402850301"
        session.save()
        again = Session.load_or_create(self.directory, "0402850301")
        self.assertEqual(again.environment, {"SAS_RAND_SEED": "402850301"})
        self.assertIn("export SAS_RAND_SEED=402850301",
                      again.write_script().read_text(encoding="utf-8"))


class EpatplotPlotFailureTest(TemporaryDirectoryTest):
    """Só o desenho falhou: as razões que o epatplot imprimiu continuam medida."""

    OUTPUT = ("epatplot:- 0.5-2.0 keV observed-to-model fractions:\n"
              "epatplot:- s: 0.967 +/- 0.024   d: 1.124 +/- 0.042\n"
              "  File \"/opt/sas/bin/epatplot_graph.py\", line 35, in <module>\n"
              "ModuleNotFoundError: No module named 'beautifultable'\n"
              " ERROR while running epatplot_graph.py.\nSTOP 1\n")

    def run_check(self, output: str):
        from xredux.runner import CommandResult, TaskFailed
        from xredux.tasks import epic

        directory = self.directory

        class Context:
            work_dir = directory

            def sas(self, step, task, parameters, cwd=None, timeout=None):
                result = CommandResult([task], 1 if task == "epatplot" else 0, output,
                                       0.1, str(directory))
                if task == "evselect":
                    write_list(Path(parameters["filteredset"]), [T0 + 1.0, T0 + 2.0])
                    return result
                raise TaskFailed(result)

            def require(self, *paths):
                raise AssertionError("o gráfico não existe; require não deveria ser chamado")

        events = EventList(Path("raw.ds"), "EPN", "IMAGING", submode="PrimeLargeWindow")
        return epic.check_pileup(Context(), events, "((X,Y) IN circle(1,1,600))",
                                 with_core_test=False, gti=Path("gti.fits"))

    def test_ratios_survive_a_plot_failure(self) -> None:
        check = self.run_check(self.OUTPUT)
        self.assertTrue(check.measured())
        self.assertEqual(check.singles, (0.967, 0.024))
        self.assertIn("beautifultable", check.plot_error)
        self.assertIn("gti(gti.fits,TIME)", check.selection)
        record = check.as_record()
        self.assertFalse(record["plot_written"])
        self.assertTrue(record["selected_events"].endswith("_pileup_evts.ds"))

    def test_failure_without_ratios_is_still_a_failure(self) -> None:
        from xredux.runner import TaskFailed

        with self.assertRaises(TaskFailed):
            self.run_check("Traceback\nModuleNotFoundError: x\nepatplot_graph.py\n")


if __name__ == "__main__":
    unittest.main()
