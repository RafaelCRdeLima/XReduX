"""Testes da exportação para o PULSARIS.

O teste que importa de verdade não é o formato do CSV em si, mas se o leitor do
próprio PULSARIS (``scripts/mcmc_fit.py`` e ``scripts/heasoft_fold.py``) consegue
consumir o que escrevemos. Por isso esses módulos são importados de fato, e o
teste é pulado quando o repositório vizinho não está presente.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from astropy.io import fits

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xredux.config import DEFAULT_PULSARIS_ROOT  # noqa: E402
from xredux.export import pulsaris  # noqa: E402

PULSARIS_ROOT = DEFAULT_PULSARIS_ROOT
CHANNEL_WIDTH_KEV = 0.005  # binagem de 5 eV do EPIC-pn


def write_events(path: Path, count: int = 5_000, seed: int = 7) -> None:
    """Lista de eventos parecida com a saída baricentrada do SAS."""
    generator = np.random.default_rng(seed)
    time = np.sort(generator.uniform(0.0, 20_000.0, size=count)) + 1.2e8
    pi_ev = generator.uniform(200.0, 11_000.0, size=count)

    columns = fits.ColDefs([
        fits.Column(name="TIME", format="D", array=time),
        fits.Column(name="PI", format="E", array=pi_ev),
    ])
    hdu = fits.BinTableHDU.from_columns(columns, name="EVENTS")
    hdu.header["TIMEREF"] = "SOLARSYSTEM"
    hdu.header["MJDREF"] = 50814.0
    hdu.header["TSTART"] = float(time[0])
    hdu.header["EXPOSURE"] = 20_000.0
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path, overwrite=True)


def write_rmf(path: Path, channels: int = 2_400) -> None:
    """RMF mínima com EBOUNDS e MATRIX, como a que o ``rmfgen`` produz."""
    channel = np.arange(channels, dtype=np.int32)
    e_min = channel * CHANNEL_WIDTH_KEV
    e_max = e_min + CHANNEL_WIDTH_KEV

    ebounds = fits.BinTableHDU.from_columns(fits.ColDefs([
        fits.Column(name="CHANNEL", format="J", array=channel),
        fits.Column(name="E_MIN", format="E", array=e_min),
        fits.Column(name="E_MAX", format="E", array=e_max),
    ]), name="EBOUNDS")

    rows = 200
    energ_lo = np.linspace(0.1, 12.0, rows, dtype=np.float32)
    energ_hi = energ_lo + (12.0 - 0.1) / rows
    width = 8
    f_chan = np.clip((energ_lo / CHANNEL_WIDTH_KEV).astype(np.int32) - width // 2,
                     0, channels - width)
    n_chan = np.full(rows, width, dtype=np.int32)
    matrix = np.tile(np.full(width, 1.0 / width, dtype=np.float32), (rows, 1))

    response = fits.BinTableHDU.from_columns(fits.ColDefs([
        fits.Column(name="ENERG_LO", format="E", array=energ_lo),
        fits.Column(name="ENERG_HI", format="E", array=energ_hi),
        fits.Column(name="N_GRP", format="J", array=np.ones(rows, dtype=np.int32)),
        fits.Column(name="F_CHAN", format="J", array=f_chan),
        fits.Column(name="N_CHAN", format="J", array=n_chan),
        fits.Column(name="MATRIX", format=f"{width}E", array=matrix),
    ]), name="MATRIX")
    response.header["TLMIN4"] = 0

    fits.HDUList([fits.PrimaryHDU(), response, ebounds]).writeto(path, overwrite=True)


def load_pulsaris_module(name: str):
    """Importa um script do PULSARIS, ou devolve ``None`` se ele não existir."""
    script = PULSARIS_ROOT / "scripts" / f"{name}.py"
    if not script.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"pulsaris_{name}", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_spectrum(path: Path, counts, backscal: float, exposure: float) -> None:
    """PHA OGIP mínimo, com o BACKSCAL e a exposição que a escala do fundo exige."""
    channel = np.arange(len(counts), dtype=np.int32)
    hdu = fits.BinTableHDU.from_columns(fits.ColDefs([
        fits.Column(name="CHANNEL", format="J", array=channel),
        fits.Column(name="COUNTS", format="J", array=np.asarray(counts, dtype=np.int32)),
    ]), name="SPECTRUM")
    hdu.header["BACKSCAL"] = backscal
    hdu.header["EXPOSURE"] = exposure
    hdu.header["HDUCLAS1"] = "SPECTRUM"
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path, overwrite=True)


class BackgroundExportTest(unittest.TestCase):
    """A escala do fundo é a receita OGIP: contagens x BACKSCAL / exposição."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.rmf = self.directory / "response.rmf"
        write_rmf(self.rmf, channels=200)
        # A fonte é 10x menor em área que a região de fundo.
        write_spectrum(self.directory / "src.fits", [0] * 200, 1.0e6, 1000.0)
        write_spectrum(self.directory / "bkg.fits", [100] * 200, 1.0e7, 1000.0)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def export(self, **kwargs):
        return pulsaris.write_background(
            self.directory / "src.fits", self.directory / "bkg.fits", self.rmf,
            self.directory / "bkg.csv", **kwargs)

    def rows(self, path: Path):
        return [tuple(float(x) for x in line.split(","))
                for line in path.read_text(encoding="utf-8").splitlines()
                if not line.startswith("#")]

    def test_rate_uses_the_backscal_ratio_and_exposure(self) -> None:
        rows = self.rows(self.export())
        self.assertTrue(rows)
        # 100 contagens x (1e6/1e7) / (1000 s x 0,005 keV) = 2,0 ct/s/keV.
        # A tolerância é frouxa de propósito: os limites de canal da EBOUNDS são
        # float32, então a largura do canal não é exatamente 5 eV.
        for _, rate in rows:
            self.assertAlmostEqual(rate, 2.0, places=4)

    def test_header_records_the_scaling_it_applied(self) -> None:
        text = self.export().read_text(encoding="utf-8")
        self.assertIn("scale_factor=0.1", text)
        self.assertIn("energy_keV,rate_per_keV_per_s", text)

    def test_energy_band_is_honoured(self) -> None:
        rows = self.rows(self.export(band_ev=(200, 400)))
        self.assertTrue(all(0.2 <= energy <= 0.4 for energy, _ in rows))

    def test_missing_backscal_is_refused(self) -> None:
        """Sem BACKSCAL a escala seria arbitrária; melhor falhar do que inventar."""
        from astropy.io import fits as pyfits
        with pyfits.open(self.directory / "bkg.fits", mode="update") as hdus:
            del hdus["SPECTRUM"].header["BACKSCAL"]
        with self.assertRaises(ValueError):
            self.export()

    def test_pulsaris_reads_the_table(self) -> None:
        if not (PULSARIS_ROOT / "scripts" / "mcmc_fit.py").is_file():
            self.skipTest("repositório do PULSARIS não encontrado")
        module = load_pulsaris_module("mcmc_fit")
        rows = module.read_background(self.export())
        self.assertGreater(len(rows), 10)
        self.assertTrue(all(rate > 0 for _, rate in rows))


class EventExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.events = self.directory / "events.fits"
        self.rmf = self.directory / "response.rmf"
        write_events(self.events)
        write_rmf(self.rmf)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def export(self, **overrides):
        parameters = dict(
            instrument="xmm_epn_timing_0106260101",
            obsid="0106260101", target="RX J1856.5-3754",
            period_s=7.055, time_resolution_us=29.52,
            band_ev=(150, 12_000), rmf=self.rmf,
            region="RAWX 27-47",
        )
        parameters.update(overrides)
        return pulsaris.write(self.events, self.directory / "out.csv", **parameters)

    def test_writes_expected_columns_and_metadata(self) -> None:
        report = self.export()
        lines = report.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], f"# {pulsaris.FORMAT_TAG}")

        metadata = dict(line[1:].strip().split("=", 1)
                        for line in lines if line.startswith("#") and "=" in line)
        self.assertEqual(metadata["obsid"], "0106260101")
        self.assertEqual(metadata["period_s"], "7.055")
        self.assertEqual(metadata["barycentric"], "SOLARSYSTEM")
        self.assertEqual(metadata["time_resolution_us"], "29.52")

        header = next(line for line in lines if not line.startswith("#"))
        self.assertEqual(header, "TIME,PI,DETECTED_ENERGY_KEV")

    def test_times_start_at_zero(self) -> None:
        report = self.export()
        rows = [line for line in report.path.read_text(encoding="utf-8").splitlines()
                if not line.startswith("#")][1:]
        first = float(rows[0].split(",")[0])
        self.assertAlmostEqual(first, 0.0, places=6)

    def test_channel_comes_from_ebounds(self) -> None:
        """O canal exportado tem de ser o índice da EBOUNDS, não o PI em eV."""
        report = self.export()
        rows = [line for line in report.path.read_text(encoding="utf-8").splitlines()
                if not line.startswith("#")][1:]
        for row in rows[:200]:
            _, channel, energy = row.split(",")
            expected = int(float(energy) / CHANNEL_WIDTH_KEV)
            self.assertLessEqual(abs(int(channel) - expected), 1)

    def test_energy_band_is_respected(self) -> None:
        narrow = self.export(band_ev=(1_000, 2_000))
        rows = [line for line in narrow.path.read_text(encoding="utf-8").splitlines()
                if not line.startswith("#")][1:]
        energies = np.array([float(row.split(",")[2]) for row in rows])
        self.assertTrue((energies >= 1.0).all() and (energies <= 2.0).all())

        wide = self.export(band_ev=(150, 12_000))
        self.assertLess(narrow.events_written, wide.events_written)
        # Sem decimação, "disponíveis" é a contagem já dentro da banda pedida.
        self.assertEqual(narrow.events_written, narrow.events_available)

    def test_decimation_is_reproducible_and_reported(self) -> None:
        first = self.export(max_events=500)
        second = self.export(max_events=500)
        self.assertTrue(first.decimated)
        self.assertEqual(first.events_written, 500)
        self.assertEqual(first.decimation_seed, 1234)
        self.assertTrue(any("decimada" in message for message in first.warnings))
        self.assertEqual(first.path.read_text(encoding="utf-8").count("\n"),
                         second.path.read_text(encoding="utf-8").count("\n"))

    def test_rejects_empty_selection(self) -> None:
        with self.assertRaises(ValueError):
            self.export(band_ev=(19_000, 20_000))

    def test_upload_budget_is_consistent(self) -> None:
        budget = pulsaris.max_events_for_upload()
        self.assertLessEqual(pulsaris.estimate_size(budget), pulsaris.MAX_UPLOAD_BYTES)


@unittest.skipUnless((PULSARIS_ROOT / "scripts" / "mcmc_fit.py").is_file(),
                     "repositório do PULSARIS não encontrado")
class PulsarisInteroperabilityTest(EventExportTest):
    """Lê o CSV exportado com os próprios leitores do PULSARIS."""

    def test_mcmc_fit_reads_the_export(self) -> None:
        module = load_pulsaris_module("mcmc_fit")
        report = self.export()
        metadata, events = module.read_events(report.path)
        self.assertEqual(len(events), report.events_written)
        self.assertEqual(metadata["instrument"], "xmm_epn_timing_0106260101")
        self.assertEqual(metadata["period_s"], "7.055")
        times = [moment for moment, _ in events]
        self.assertEqual(times, sorted(times))

    def test_heasoft_fold_reads_the_export(self) -> None:
        module = load_pulsaris_module("heasoft_fold")
        if module is None:
            self.skipTest("heasoft_fold.py ausente")
        report = self.export()
        result = module.read_events(report.path, 0.15, 12.0)
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
