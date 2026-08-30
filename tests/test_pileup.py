"""A seleção do diagnóstico de empilhamento não filtra padrões.

É a distribuição de padrões que está sendo diagnosticada. Cortar em
``PATTERN<=4`` — o corte certo para espectro e curva de luz — joga fora
triplos e quádruplos, faz o epatplot avisar ``sigmaTooLarge, not enough
statistics for PAT = 3`` porque não sobrou nenhum, e desloca as próprias
razões que se quer medir, já que são frações do total.

Medido na 0844140101: com o corte, s 0,975 e d 1,125; sem ele, s 0,968 e
d 1,115, com triplos em 0,39% e quádruplos em 0,10%.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xredux.tasks import epic, regions  # noqa: E402


class RecordingContext:
    """Contexto que anota os comandos em vez de rodá-los."""

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self.calls: list[tuple[str, dict]] = []

    def sas(self, step, task, parameters=None, **_):
        self.calls.append((task, dict(parameters or {})))
        # O epatplot precisa achar o produto; o conteúdo não importa aqui.
        for value in (parameters or {}).values():
            if isinstance(value, Path) or (isinstance(value, str)
                                           and value.endswith((".ds", ".pdf"))):
                candidate = self.work_dir / Path(str(value)).name
                candidate.write_text("x", encoding="utf-8")

        class Result:
            output = ""
        return Result()

    def require(self, *paths) -> None:
        pass


class PileupSelectionTest(unittest.TestCase):
    def _expression(self) -> str:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            context = RecordingContext(work)
            events = epic.EventList(path=work / "eventos.ds", instrument="EPN",
                                    mode="IMAGING")
            epic.check_pileup(context, events,
                              regions.circle(25952.6, 23860.4, 30.0),
                              with_core_test=False)
            selections = [p for task, p in context.calls if task == "evselect"]
            self.assertTrue(selections)
            return str(selections[0]["expression"])

    def test_the_selection_keeps_every_pattern(self) -> None:
        self.assertNotIn("PATTERN", self._expression())

    def test_the_region_is_still_applied(self) -> None:
        self.assertIn("circle(25952.6,23860.4,600.0)", self._expression())

    def test_the_quality_filter_is_still_applied(self) -> None:
        self.assertIn("XMMEA_EP", self._expression())


class PileupVerdictTest(unittest.TestCase):
    """O veredito compara amostras independentes, não uma com o seu subconjunto."""

    def _check(self, doubles, core=None, wings=None) -> epic.PileupCheck:
        def part(value):
            return None if value is None else epic.PileupCheck(
                plot=Path("x.pdf"), rate_ct_s=1.0, frame_time_s=0.0734,
                singles=(1.0, 0.02), doubles=value)
        return epic.PileupCheck(plot=Path("x.pdf"), rate_ct_s=2.7,
                                frame_time_s=0.0734, singles=(0.97, 0.013),
                                doubles=doubles, core=part(core), wings=part(wings))

    def test_no_excess_is_clean(self) -> None:
        self.assertEqual(self._check((1.01, 0.023)).verdict(), "clean")

    def test_an_excess_only_in_the_core_is_pile_up(self) -> None:
        check = self._check((1.30, 0.023), core=(1.50, 0.02), wings=(1.02, 0.03))
        self.assertEqual(check.verdict(), "pileup")

    def test_a_uniform_excess_is_not_pile_up(self) -> None:
        """O caso da 0844140101: sobra real, mas igual no núcleo e nas asas."""
        check = self._check((1.115, 0.023), core=(1.120, 0.027), wings=(1.103, 0.042))
        self.assertLess(abs(check.gradient_sigma()), 1.0)
        self.assertEqual(check.verdict(), "unexplained")

    def test_without_the_two_halves_it_says_so(self) -> None:
        self.assertEqual(self._check((1.30, 0.023)).verdict(), "inconclusive")


if __name__ == "__main__":
    unittest.main()
