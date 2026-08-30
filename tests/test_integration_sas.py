"""Integração real com o SAS: as tarefas rodam de verdade.

Diferente dos demais testes, estes executam o SAS instalado. Não substituem a
validação científica sobre uma observação real — isso leva horas — mas provam o
encanamento: ambiente montado, expressões de seleção aceitas, produtos gerados,
sessão registrada e ``reproduce.sh`` executável.

Pulam-se sozinhos quando o SAS não está instalado.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from astropy.io import fits

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xredux import env as sas_env  # noqa: E402
from xredux.config import Settings  # noqa: E402
from xredux.pipeline import build_context  # noqa: E402
from xredux.session import Session  # noqa: E402
from xredux.tasks import filtering, regions, spectra  # noqa: E402
from xredux.tasks.epic import EventList  # noqa: E402

SETTINGS = Settings.load()
try:
    sas_env.build(SETTINGS)
    HAS_SAS = True
except sas_env.EnvironmentError_:
    HAS_SAS = False


def write_event_list(path: Path, count: int = 40_000, seed: int = 11) -> None:
    """Lista de eventos com a estrutura mínima que o ``evselect`` exige.

    Não é uma observação: é o bastante para que as tarefas aceitem o arquivo e
    exerçam o caminho completo de seleção, GTI, imagem e espectro.
    """
    generator = np.random.default_rng(seed)
    time = np.sort(generator.uniform(0.0, 10_000.0, count)) + 1.2e8
    # Um surto de fundo no meio da exposição, para o corte de flares ter o que cortar.
    flare = generator.uniform(4_000.0, 4_600.0, count // 8) + 1.2e8
    time = np.sort(np.concatenate([time, flare]))
    total = time.size

    columns = fits.ColDefs([
        fits.Column(name="TIME", format="D", unit="s", array=time),
        fits.Column(name="RAWX", format="I", array=generator.integers(0, 64, total)),
        fits.Column(name="RAWY", format="I", array=generator.integers(0, 200, total)),
        fits.Column(name="X", format="J", array=generator.integers(20_000, 32_000, total)),
        fits.Column(name="Y", format="J", array=generator.integers(20_000, 32_000, total)),
        fits.Column(name="PI", format="J", unit="eV",
                    array=generator.integers(200, 14_000, total)),
        fits.Column(name="PHA", format="J", array=generator.integers(0, 4_000, total)),
        fits.Column(name="PATTERN", format="B", array=generator.integers(0, 5, total)),
        fits.Column(name="FLAG", format="J", array=np.zeros(total, dtype=np.int32)),
    ])
    # Sem CCDNR o evselect não tenta recalcular a exposição por CCD — cálculo que,
    # sobre um arquivo sintético, produz NaN e faz o CFITSIO recusar a escrita.
    events = fits.BinTableHDU.from_columns(columns, name="EVENTS")
    events.header["TELESCOP"] = "XMM"
    events.header["INSTRUME"] = "EPN"
    events.header["DATAMODE"] = "TIMING"
    events.header["SUBMODE"] = "PrimeFullWindow"
    events.header["FILTER"] = "Thin1"
    events.header["TSTART"] = float(time[0])
    events.header["TSTOP"] = float(time[-1])
    events.header["ONTIME"] = float(time[-1] - time[0])
    events.header["LIVETIME"] = float(time[-1] - time[0]) * 0.98
    events.header["EXPOSURE"] = float(time[-1] - time[0]) * 0.98
    events.header["MJDREF"] = 50814.0
    events.header["DATE-OBS"] = "2026-01-01T00:00:00"
    events.header["DATE-END"] = "2026-01-01T02:46:40"
    events.header["DATE"] = "2026-01-01T03:00:00"
    events.header["CREATOR"] = "xredux-test"
    events.header["TIMESYS"] = "TT"
    events.header["TIMEUNIT"] = "s"
    events.header["TIMEZERO"] = 0.0
    events.header["TIMEDEL"] = 2.952e-5
    events.header["TELAPSE"] = float(time[-1] - time[0])
    # O evselect copia os atributos de tempo morto e fração de contagem para o
    # arquivo de taxa; ausentes, ele os grava como NaN e o CFITSIO recusa.
    for keyword, value in (("DEADC", 1.0), ("DLMEAN", 1.0), ("DLMAX", 1.0),
                           ("DLMIN", 1.0), ("FCMEAN", 1.0), ("FCMAX", 1.0),
                           ("FCMIN", 1.0), ("SEQPNUM", 1), ("REVOLUT", 4321),
                           ("OBS_ID", "0000000000"), ("EXP_ID", "0000000000PNS003")):
        events.header[keyword] = value

    gti = fits.BinTableHDU.from_columns(fits.ColDefs([
        fits.Column(name="START", format="D", array=np.array([time[0]])),
        fits.Column(name="STOP", format="D", array=np.array([time[-1]])),
    ]), name="STDGTI")

    primary = fits.PrimaryHDU()
    primary.header["TELESCOP"] = "XMM"
    primary.header["INSTRUME"] = "EPN"
    fits.HDUList([primary, events, gti]).writeto(path, overwrite=True)


class SyntheticEventList(EventList):
    """Lista de eventos sintética, sem a macro de qualidade da câmera.

    ``#XMMEA_EP`` não é uma coluna: é uma macro de atributo que o ``epproc``
    grava no bloco de seleção da lista calibrada. Um arquivo montado à mão não a
    tem, e o ``evselect`` recusa a expressão. Nos dados reais o filtro continua
    sendo aplicado — é aqui, e só aqui, que ele sai de cena.
    """

    @property
    def quality_flag(self) -> str:
        return ""


@unittest.skipUnless(HAS_SAS, "SAS não instalado")
class SasPipelineTest(unittest.TestCase):
    """Roda as tarefas do pipeline contra o SAS instalado."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.work = Path(cls._temporary.name)
        cls.raw = cls.work / "events.fits"
        write_event_list(cls.raw)

        cls.session = Session(cls.work, "0000000000", "teste de integração")
        cls.context = build_context(SETTINGS, cls.session, on_line=lambda line: None)
        cls.events = SyntheticEventList(path=cls.raw, instrument="EPN",
                                        mode="TIMING", filter_name="Thin1")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_01_background_curve(self) -> None:
        curve = filtering.background_curve(self.context, self.events, binsize_s=100.0)
        self.assertTrue(curve.path.is_file())
        self.assertGreater(curve.time.size, 10)
        threshold = curve.suggested_threshold()
        self.assertGreater(threshold, 0.0)
        self.assertGreaterEqual(curve.good_fraction(threshold), 0.0)
        type(self).curve = curve

    def test_02_gti_and_clean_events(self) -> None:
        gti = filtering.make_gti(self.context, self.curve,
                                 self.curve.suggested_threshold())
        self.assertTrue(gti.is_file())
        clean = filtering.filter_events(self.context, self.events, gti=gti,
                                        energy_min_ev=150, energy_max_ev=12_000)
        self.assertTrue(clean.is_file())

        with fits.open(clean) as hdus:
            pi = np.asarray(hdus["EVENTS"].data["PI"], dtype=float)
            pattern = np.asarray(hdus["EVENTS"].data["PATTERN"], dtype=int)
        self.assertTrue((pi > 150).all() and (pi < 12_000).all())
        self.assertTrue((pattern <= 4).all())
        type(self).clean = clean

    def test_03_image(self) -> None:
        image = regions.extract_image(self.context, self.events)
        self.assertTrue(image.is_file())
        with fits.open(image) as hdus:
            self.assertIsNotNone(hdus[0].data)

    def test_04_spectrum_counts_per_channel(self) -> None:
        """O produto central: contagens por canal, somando o que foi selecionado."""
        source = regions.rawx_band(27, 47)
        spectrum = spectra.extract(self.context, self.events, self.clean,
                                   source.expression, name="src")
        self.assertTrue(spectrum.path.is_file())

        channel, counts = spectra.read_channel_counts(spectrum.path)
        self.assertGreater(channel.size, 100)
        self.assertGreater(counts.sum(), 0)

        # A soma sobre os canais tem de bater com os eventos que passaram no filtro.
        with fits.open(self.clean) as hdus:
            data = hdus["EVENTS"].data
            selected = np.count_nonzero(
                (data["RAWX"] >= 27) & (data["RAWX"] <= 47)
                & (data["PATTERN"] <= 4) & (data["FLAG"] == 0))
        self.assertEqual(int(counts.sum()), selected)

    def test_05_session_records_a_runnable_script(self) -> None:
        import subprocess

        script = self.session.write_script()
        self.assertTrue(script.is_file())
        content = script.read_text(encoding="utf-8")
        self.assertIn("evselect", content)
        self.assertIn("tabgtigen", content)
        self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
