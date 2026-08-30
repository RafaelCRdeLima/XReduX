"""A seção do artigo só pode afirmar o que a sessão registra.

É o requisito que governa este módulo. Uma frase de método inventada numa seção
de artigo não é um defeito de programa: é um erro no registro científico, e não
há revisão que o pegue se o texto soar plausível.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xredux.export import latex  # noqa: E402
from xredux.session import Session  # noqa: E402


@dataclass
class FakeRegion:
    expression: str


@dataclass
class FakeEvents:
    instrument: str = "EPN"
    mode: str = "IMAGING"
    submode: str = "PRIMESMALLWINDOW"
    filter_name: str = "Thin1"
    ontime_s: float = 82514.0
    max_pattern: int = 4
    path: Path | None = None

    def time_resolution_us(self) -> float:
        return 5700.0


@dataclass
class FakeState:
    obsid: str = "0412601301"
    target: str = "RX J1856.5-3754"
    ra: float | None = None
    dec: float | None = None
    selected: object = None
    event_lists: list = field(default_factory=list)
    clean_events: Path | None = None
    source_region: object = None
    background_region: object = None
    background_curve: object = None
    threshold: float | None = None
    barycentered: Path | None = None
    corrected_light_curve: Path | None = None
    light_curve: object = None
    candidates: list = field(default_factory=list)
    period_s: float | None = None
    h_statistic: float | None = None
    h_harmonics: int | None = None
    pulsed_fraction: tuple | None = None
    pulsed_fraction_rms: tuple | None = None
    profile_bundle: object = None
    source_spectrum: object = None
    pileup: object = None
    event_count: int | None = None
    ccf_cif: Path | None = None


class RegionDescriptionTest(unittest.TestCase):
    def test_a_circle_becomes_a_radius_in_arcsec(self) -> None:
        text = latex.describe_region(FakeRegion("((X,Y) IN circle(1,2,600.0))"))
        self.assertIn("30$^{\\prime\\prime}$ radius", text)

    def test_an_annulus_becomes_two_radii(self) -> None:
        text = latex.describe_region(
            FakeRegion("((X,Y) IN annulus(1,2,1200.0,2400.0))"))
        self.assertIn("60$^{\\prime\\prime}$", text)
        self.assertIn("120$^{\\prime\\prime}$", text)

    def test_a_timing_band_becomes_rawx_columns(self) -> None:
        self.assertIn("RAWX 27--47",
                      latex.describe_region(FakeRegion("(RAWX IN [27:47])")))

    def test_an_unknown_shape_says_nothing(self) -> None:
        """Melhor omitir do que descrever errado."""
        self.assertEqual(latex.describe_region(FakeRegion("(PI>500)")), "")


class NoInventionTest(unittest.TestCase):
    """O texto encolhe com o que não foi feito; nunca preenche a lacuna."""

    def _section(self, state) -> str:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            session = Session.load_or_create(Path(directory), state.obsid,
                                             state.target)
            return latex.build(state, session, settings=None)

    def test_a_bare_state_claims_no_method(self) -> None:
        text = self._section(FakeState(selected=FakeEvents()))
        for claim in ("barycen", "epatplot", "rmfgen", "efsearch", "phasecalc",
                      "tabgtigen", "powspec"):
            self.assertNotIn(claim, text, f"afirmou {claim} sem ter rodado")

    def test_pile_up_is_only_mentioned_when_measured(self) -> None:
        self.assertNotIn("pile-up", self._section(FakeState(selected=FakeEvents())))

    def test_the_period_is_only_reported_when_found(self) -> None:
        text = self._section(FakeState(selected=FakeEvents()))
        self.assertNotIn("$Z^2_n$", text)
        self.assertNotIn("H$-test", text)

    def test_what_was_done_does_appear(self) -> None:
        state = FakeState(selected=FakeEvents(), period_s=7.055240,
                          h_statistic=38.2, h_harmonics=1,
                          pulsed_fraction=(0.0134, 0.0022),
                          barycentered=Path("bary.fits"))
        text = self._section(state)
        self.assertIn(r"\texttt{barycen}", text)
        self.assertIn("7.055240", text)
        self.assertIn("1.34", text)

    def test_unmeasured_values_are_loud(self) -> None:
        """Um número ausente tem de saltar aos olhos no PDF, não sumir."""
        text = self._section(FakeState(selected=FakeEvents(), target=""))
        self.assertGreater(latex.count_missing(text), 0)

    def test_the_marker_in_the_header_is_not_counted(self) -> None:
        self.assertEqual(latex.count_missing(f"% explica {latex.MISSING}\ntexto"), 0)


class CitationTest(unittest.TestCase):
    """Toda chave citada tem de existir no .bib, ou vira [?] no PDF."""

    def test_every_citation_has_an_entry(self) -> None:
        import tempfile

        state = FakeState(selected=FakeEvents(), period_s=7.05,
                          h_statistic=38.2, h_harmonics=1,
                          event_lists=[FakeEvents(instrument="EMOS1")])
        with tempfile.TemporaryDirectory() as directory:
            session = Session.load_or_create(Path(directory), "0", "alvo")
            text = latex.build(state, session, settings=None)
        for key in latex._citations(state, session, text):
            self.assertIn(key, latex.BIBLIOGRAPHY, f"{key} citado sem entrada")

    def test_citations_are_taken_from_the_text(self) -> None:
        keys = latex._citations(None, None, r"a \citep{jansen2001} b \citealt{x2000}")
        self.assertEqual(keys, ["jansen2001", "x2000"])


class EscapingTest(unittest.TestCase):
    def test_a_source_name_with_underscores_is_escaped(self) -> None:
        self.assertEqual(latex._escape("a_b"), r"a\_b")

    def test_the_plus_of_a_declination_survives(self) -> None:
        """RX J1308.6+2127 não pode virar outra coisa."""
        self.assertEqual(latex._escape("RX J1308.6+2127"), "RX J1308.6+2127")


class ModeNameTest(unittest.TestCase):
    def test_submodes_get_their_published_names(self) -> None:
        self.assertEqual(latex.mode_name("PRIMESMALLWINDOW"), "Small Window")
        self.assertEqual(latex.mode_name("PRIMEFULLWINDOW"), "Full Frame")
        self.assertEqual(latex.mode_name("FASTTIMING"), "Timing")

    def test_an_unknown_submode_is_passed_through(self) -> None:
        self.assertEqual(latex.mode_name("ALGONOVO"), "Algonovo")


if __name__ == "__main__":
    unittest.main()
