"""Testes das estatísticas de timing.

O ponto central é verificar que Z²ₙ e o teste H recuperam um período injetado e
não inventam sinal em ruído puro. Nenhum destes testes precisa do SAS.
"""

from __future__ import annotations

import sys
import math
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xredux.tasks import timing  # noqa: E402

#: Período de RX J1856.5-3754, o alvo de validação do pipeline.
PERIOD = 7.055
RNG_SEED = 20260829


def modulated_events(period: float, count: int, amplitude: float,
                     duration: float = 40_000.0, seed: int = RNG_SEED) -> np.ndarray:
    """Tempos de chegada com modulação senoidal de amplitude conhecida.

    Amostragem por rejeição sobre a taxa ``1 + a·sin(2πt/P)``, que é o modelo mais
    simples compatível com um perfil quase senoidal como o de RX J1856.5-3754.
    """
    generator = np.random.default_rng(seed)
    accepted: list[np.ndarray] = []
    remaining = count
    while remaining > 0:
        trial = generator.uniform(0.0, duration, size=remaining * 3)
        probability = 1.0 + amplitude * np.sin(2.0 * np.pi * trial / period)
        keep = trial[generator.uniform(0.0, 1.0 + amplitude, size=trial.size) < probability]
        accepted.append(keep[:remaining])
        remaining -= keep[:remaining].size
    return np.sort(np.concatenate(accepted))


class ZSquaredTest(unittest.TestCase):
    def test_recovers_injected_period(self) -> None:
        times = modulated_events(PERIOD, count=60_000, amplitude=0.20)
        # Parte de um chute deslocado, como aconteceria após uma busca grosseira.
        result = timing.refine_period(times, PERIOD * 1.0001,
                                      span_fraction=5e-4, trials=1201, harmonics=2)
        self.assertAlmostEqual(result.best_period_s, PERIOD, delta=PERIOD * 2e-5)
        self.assertGreater(result.statistic, 100.0)

    def test_flat_on_pure_noise(self) -> None:
        generator = np.random.default_rng(RNG_SEED + 1)
        times = np.sort(generator.uniform(0.0, 40_000.0, size=60_000))
        frequencies = np.linspace(1.0 / 8.0, 1.0 / 6.0, 400)
        statistic = timing.z_squared_n(times, frequencies, harmonics=2)
        # Sob ruído, Z²₂ segue χ² com 4 graus de liberdade: média 4.
        self.assertLess(statistic.mean(), 8.0)
        self.assertLess(statistic.max(), 60.0)

    def test_matches_direct_definition(self) -> None:
        """A versão em blocos deve concordar com a fórmula escrita diretamente."""
        generator = np.random.default_rng(RNG_SEED + 2)
        times = np.sort(generator.uniform(0.0, 500.0, size=2_000))
        frequencies = np.linspace(0.10, 0.20, 37)

        expected = []
        for frequency in frequencies:
            phase = 2.0 * np.pi * ((times - times[0]) * frequency % 1.0)
            total = 0.0
            for harmonic in (1, 2):
                total += (np.cos(harmonic * phase).sum() ** 2
                          + np.sin(harmonic * phase).sum() ** 2)
            expected.append(2.0 * total / times.size)

        computed = timing.z_squared_n(times, frequencies, harmonics=2,
                                      memory_budget=1_000)
        np.testing.assert_allclose(computed, expected, rtol=1e-9, atol=1e-9)

    def test_empty_input_is_safe(self) -> None:
        result = timing.z_squared_n(np.empty(0), np.array([0.1, 0.2]))
        np.testing.assert_array_equal(result, np.zeros(2))


class HTest(unittest.TestCase):
    def test_detects_modulation(self) -> None:
        times = modulated_events(PERIOD, count=40_000, amplitude=0.20)
        statistic, harmonics = timing.h_test(times, 1.0 / PERIOD)
        self.assertGreater(statistic, 50.0)
        self.assertGreaterEqual(harmonics, 1)
        self.assertLess(timing.h_test_probability(statistic), 1e-6)

    def test_quiet_on_noise(self) -> None:
        generator = np.random.default_rng(RNG_SEED + 3)
        times = np.sort(generator.uniform(0.0, 40_000.0, size=40_000))
        statistic, _ = timing.h_test(times, 1.0 / PERIOD)
        self.assertLess(statistic, 20.0)
        self.assertGreater(timing.h_test_probability(statistic), 1e-4)


class BinningAliasTest(unittest.TestCase):
    """Um pico do epoch folding pode ser alias da grade da curva binada.

    Reproduz o que apareceu em RBS 1223: o efsearch achou χ² = 259 em
    7,000047 s — exatamente 14 bins de 0,5 s — enquanto os tempos de chegada
    não binados davam p = 0,89, isto é, nada.
    """

    def test_h_test_rejects_a_period_with_no_modulation(self) -> None:
        generator = np.random.default_rng(RNG_SEED + 21)
        times = np.sort(generator.uniform(0.0, 7600.0, size=84_000))
        statistic, _ = timing.h_test(times, 1.0 / 7.000047)
        self.assertLess(statistic, 10.0)
        self.assertGreater(timing.h_test_probability(statistic), 1.0e-3)

    def test_h_test_accepts_a_real_signal_in_the_same_exposure(self) -> None:
        """Mesma duração e contagem, agora com modulação injetada."""
        times = modulated_events(10.3125, count=84_000, amplitude=0.05,
                                 duration=7600.0, seed=RNG_SEED + 22)
        statistic, _ = timing.h_test(times, 1.0 / 10.3125)
        self.assertGreater(statistic, 30.0)
        self.assertLess(timing.h_test_probability(statistic), 1.0e-3)

    def test_the_threshold_separates_the_two_cases(self) -> None:
        """O critério do programa: p < 1e-3 confirma, acima disso não."""
        generator = np.random.default_rng(RNG_SEED + 23)
        ruido = np.sort(generator.uniform(0.0, 7600.0, size=84_000))
        sinal = modulated_events(10.3125, count=84_000, amplitude=0.05,
                                 duration=7600.0, seed=RNG_SEED + 24)
        limite = 1.0e-3
        self.assertFalse(
            timing.h_test_probability(timing.h_test(ruido, 1 / 7.000047)[0]) < limite)
        self.assertTrue(
            timing.h_test_probability(timing.h_test(sinal, 1 / 10.3125)[0]) < limite)


class PulsedFractionTest(unittest.TestCase):
    """O estimador de Fourier recupera a amplitude injetada; o do histograma não."""

    def test_fourier_estimator_recovers_a_small_amplitude(self) -> None:
        amplitude = 0.013  # da ordem da de RX J1856.5-3754
        times = modulated_events(PERIOD, count=400_000, amplitude=amplitude)
        z_single = float(timing.z_squared_n(times, np.array([1.0 / PERIOD]),
                                            harmonics=1)[0])
        estimated, uncertainty = timing.pulsed_fraction_from_z2(z_single, times.size)
        self.assertAlmostEqual(estimated, amplitude, delta=3.0 * uncertainty)
        self.assertAlmostEqual(uncertainty, np.sqrt(2.0 / times.size), places=6)

    def test_histogram_estimator_is_biased_high_on_noise(self) -> None:
        """Sem sinal nenhum, a razão entre extremos ainda acusa alguns por cento."""
        generator = np.random.default_rng(RNG_SEED + 7)
        times = np.sort(generator.uniform(0.0, 40_000.0, size=400_000))
        histogram, _ = timing.pulsed_fraction(times, PERIOD, phase_bins=16)
        fourier, _ = timing.pulsed_fraction_from_z2(
            float(timing.z_squared_n(times, np.array([1.0 / PERIOD]), harmonics=1)[0]),
            times.size)
        self.assertGreater(histogram, 3.0 * fourier)

    def test_rms_matches_the_fundamental_for_a_pure_sinusoid(self) -> None:
        """Com um harmônico só, RMS e fundamental medem a mesma amplitude."""
        amplitude = 0.20
        times = modulated_events(PERIOD, count=200_000, amplitude=amplitude)
        z1 = float(timing.z_squared_n(times, np.array([1.0 / PERIOD]), harmonics=1)[0])
        fundamental, _ = timing.pulsed_fraction_from_z2(z1, times.size)
        rms, _ = timing.rms_pulsed_fraction(z1, 1, times.size)
        # PF_rms = a/raiz(2) para uma senoide; o fundamental devolve a própria a.
        self.assertAlmostEqual(rms * np.sqrt(2.0), fundamental, delta=0.01)

    def test_rms_captures_power_the_fundamental_misses(self) -> None:
        """Perfil de pico duplo: quase toda a potência está no segundo harmônico."""
        generator = np.random.default_rng(RNG_SEED + 11)
        duration, count = 40_000.0, 300_000
        accepted, remaining = [], count
        while remaining > 0:
            trial = generator.uniform(0.0, duration, size=remaining * 3)
            # Só o segundo harmônico: dois picos por rotação, fundamental nulo.
            rate = 1.0 + 0.25 * np.cos(4.0 * np.pi * trial / PERIOD)
            keep = trial[generator.uniform(0.0, 1.25, size=trial.size) < rate]
            accepted.append(keep[:remaining]); remaining -= keep[:remaining].size
        times = np.sort(np.concatenate(accepted))

        frequency = np.array([1.0 / PERIOD])
        z1 = float(timing.z_squared_n(times, frequency, harmonics=1)[0])
        z2 = float(timing.z_squared_n(times, frequency, harmonics=2)[0])
        fundamental, _ = timing.pulsed_fraction_from_z2(z1, times.size)
        rms, _ = timing.rms_pulsed_fraction(z2, 2, times.size)

        self.assertLess(fundamental, 0.02)      # o fundamental quase não vê nada
        self.assertGreater(rms, 0.10)           # o RMS recupera a modulação
        self.assertAlmostEqual(rms * np.sqrt(2.0), 0.25, delta=0.02)

    def test_rms_uncertainty_is_the_noise_floor(self) -> None:
        _, uncertainty = timing.rms_pulsed_fraction(500.0, 3, 100_000)
        self.assertAlmostEqual(uncertainty, np.sqrt(1.0 / 100_000), places=9)

    def test_rms_is_zero_without_signal(self) -> None:
        self.assertEqual(timing.rms_pulsed_fraction(5.0, 3, 1000)[0], 0.0)

    def test_rms_is_safe_on_degenerate_input(self) -> None:
        for value, order, count in ((50.0, 0, 1000), (50.0, 2, 0)):
            fraction, error = timing.rms_pulsed_fraction(value, order, count)
            self.assertTrue(np.isnan(fraction) and np.isnan(error))

    def test_no_signal_gives_zero_fourier_fraction(self) -> None:
        self.assertEqual(timing.pulsed_fraction_from_z2(1.5, 1000)[0], 0.0)

    def test_empty_sample_is_safe(self) -> None:
        fraction, uncertainty = timing.pulsed_fraction_from_z2(50.0, 0)
        self.assertTrue(np.isnan(fraction) and np.isnan(uncertainty))


class ProfileTest(unittest.TestCase):
    def test_pulsed_fraction_tracks_amplitude(self) -> None:
        weak = timing.pulsed_fraction(
            modulated_events(PERIOD, 80_000, 0.05), PERIOD)[0]
        strong = timing.pulsed_fraction(
            modulated_events(PERIOD, 80_000, 0.40), PERIOD)[0]
        self.assertLess(weak, strong)
        self.assertGreater(strong, 0.25)

    def test_profile_shape_and_normalisation(self) -> None:
        times = modulated_events(PERIOD, 20_000, 0.30)
        phase, counts, error = timing.profile(times, PERIOD, phase_bins=16)
        self.assertEqual(phase.size, 16)
        self.assertEqual(counts.sum(), times.size)
        np.testing.assert_allclose(error, np.sqrt(np.maximum(counts, 1)))

    def test_profile_rejects_nothing_on_empty(self) -> None:
        fraction, uncertainty = timing.pulsed_fraction(np.empty(0), PERIOD)
        self.assertTrue(np.isnan(fraction))
        self.assertTrue(np.isnan(uncertainty))


class PhaseBinAdviceTest(unittest.TestCase):
    """Quantos bins o perfil suporta sai das contagens e da amplitude."""

    def test_more_photons_allow_more_bins(self) -> None:
        few = timing.suggested_phase_bins(10_000, 0.05)
        many = timing.suggested_phase_bins(100_000, 0.05)
        self.assertLess(few, many)

    def test_weaker_modulation_forces_fewer_bins(self) -> None:
        """Quadrático: metade da amplitude, um quarto dos bins."""
        # Longe do teto de 128, para medir a lei e não o corte.
        strong = timing.suggested_phase_bins(1_000_000, 0.02)
        weak = timing.suggested_phase_bins(1_000_000, 0.01)
        self.assertEqual((strong, weak), (100, 25))

    def test_each_bin_reaches_the_requested_significance(self) -> None:
        count, fraction = 404_892, 0.0134
        bins = timing.suggested_phase_bins(count, fraction)
        self.assertGreaterEqual(fraction * math.sqrt(count / bins), 2.0)

    def test_it_is_capped_and_floored(self) -> None:
        self.assertEqual(timing.suggested_phase_bins(10_000_000, 0.9), 128)
        self.assertEqual(timing.suggested_phase_bins(10, 0.001), 4)

    def test_nonsense_input_falls_back(self) -> None:
        self.assertEqual(timing.suggested_phase_bins(0, 0.1), 16)
        self.assertEqual(timing.suggested_phase_bins(1000, 0.0), 16)


class HarmonicAdviceTest(unittest.TestCase):
    """O teste H escolhe o número de harmônicos; não é convenção."""

    def _arrival_times(self, order: int, amplitude: float = 0.6,
                       draws: int = 300_000, period: float = 5.0) -> np.ndarray:
        """Tempos com um perfil que só tem potência no harmônico ``order``."""
        generator = np.random.default_rng(20260830)
        phase = generator.random(draws)
        accepted = phase[generator.random(draws)
                         < 0.5 * (1.0 + amplitude * np.cos(2 * np.pi * order * phase))]
        return accepted * period

    def test_it_reports_the_highest_harmonic_carrying_power(self) -> None:
        for order in (1, 2, 3):
            with self.subTest(order=order):
                times = self._arrival_times(order=order)
                self.assertEqual(timing.suggested_harmonics(times, 1.0 / 5.0), order)

    def test_it_does_not_follow_the_h_test_argmax(self) -> None:
        """A razão de existir desta função.

        Num seno puro de 300 mil fótons, Z² vale ~27 mil e os harmônicos 2 a 5
        somam uma unidade cada — ruído. Ainda assim o argmax de Z²ₘ−4m+4 escolhe
        m = 5, porque a diferença entre as opções é de poucas unidades. Seguir
        esse número como conselho mandaria dobrar em cima de nada.
        """
        times = self._arrival_times(order=1)
        _, argmax = timing.h_test(times, 1.0 / 5.0)
        self.assertGreater(argmax, 1)
        self.assertEqual(timing.suggested_harmonics(times, 1.0 / 5.0), 1)

    def test_it_never_returns_zero(self) -> None:
        noise = np.random.default_rng(3).random(5_000) * 100.0
        self.assertGreaterEqual(timing.suggested_harmonics(noise, 1.0 / 7.0), 1)


if __name__ == "__main__":
    unittest.main()
