"""The item analysis page: plain-language findings, chart geometry and the redesigned page."""

from django.test import Client, override_settings
from django.urls import reverse

from . import analysis, charts
from .marking import right_id
from .test_upgrades import PLAIN_STATIC
from .tests import SECRET_FILL, ExamTestBase


class ChartGeometryTests(ExamTestBase):
    def bins(self, counts):
        return [{"label": f"{i}–{i + 1}", "count": c} for i, c in enumerate(counts)]

    def test_histogram_bars_are_scaled_to_the_axis_and_markers_sit_on_the_same_scale(self):
        h = charts.histogram(self.bins([0, 1, 2, 0, 4, 0, 0, 0, 0, 1]), 10, mean=5, median=9.5)
        self.assertEqual(len(h["bars"]), 10)
        self.assertEqual(max(b["h"] for b in h["bars"]), 100.0)         # the axis tops out at the tallest bar (4)
        self.assertEqual([b["h"] for b in h["bars"]][1:3], [25.0, 50.0])
        self.assertEqual([t["label"] for t in h["yticks"]], [0, 1, 2, 3, 4])
        self.assertEqual(h["xticks"][0]["label"], "0%")
        self.assertEqual(h["xticks"][-1]["label"], "100%")
        self.assertEqual((h["mean"]["left"], h["mean"]["flip"]), (50.0, False))
        self.assertTrue(h["median"]["flip"])                             # near the right edge: label flips inwards
        self.assertTrue(all(0 <= b["left"] and b["left"] + b["width"] <= 100 for b in h["bars"]))

    def test_histogram_axis_stays_readable_for_big_groups(self):
        h = charts.histogram(self.bins([0, 3, 40, 7, 0, 0, 0, 0, 0, 0]), 20)
        self.assertLessEqual(len(h["yticks"]), 7)
        self.assertEqual(h["yticks"][-1]["label"], 40)
        self.assertIsNone(h["mean"])

    def test_histogram_and_scatter_are_none_without_data(self):
        self.assertIsNone(charts.histogram([], 10))
        self.assertIsNone(charts.histogram(self.bins([1] * 10), 0))
        self.assertIsNone(charts.scatter([{"n": 1, "score_pct": 50, "disc": None, "band": "ok"}]))

    def test_scatter_clamps_and_keeps_only_questions_with_both_figures(self):
        items = [{"n": 1, "score_pct": 50, "disc": 0.5, "band": "ok"}, {"n": 2, "score_pct": 120, "disc": -9, "band": "easy"},
                 {"n": 3, "score_pct": None, "disc": 0.2, "band": ""}]
        s = charts.scatter(items)
        self.assertEqual([p["n"] for p in s["points"]], [1, 2])
        self.assertEqual((s["points"][1]["left"], s["points"][1]["bottom"]), (100, 0.0))
        self.assertTrue(all(0 <= p["bottom"] <= 100 for p in s["points"]))


class PlainLanguageTests(ExamTestBase):
    def test_bands_and_discrimination_labels(self):
        self.assertEqual([analysis.band_for(v) for v in (None, 0, 29, 30, 49, 50, 84, 85, 100)],
                         ["", "vhard", "vhard", "hard", "hard", "ok", "ok", "easy", "easy"])
        self.assertEqual([analysis.disc_label(v) for v in (None, 0.5, 0.3, 0.2, 0.05, -0.1)],
                         ["", "Good", "Good", "Fair", "Weak", "Reversed"])


@override_settings(STORAGES=PLAIN_STATIC)
class AnalysisPageTests(ExamTestBase):
    def run_candidate(self, reg, answers):
        c = Client()
        _, cfg = self.open_page(c, reg_no=reg, name=f"Cand {reg}")
        self.call(c, cfg, "start")
        self.call(c, cfg, "submit", answers=answers)

    def candidates(self, n, strong_share=0.5):
        m1, m2, f, mt = self.qs[("mcq", 1)], self.qs[("mcq", 2)], self.qs[("fill", 1)], self.qs[("match", 1)]
        for i in range(n):
            strong = i < n * strong_share
            ans = {str(m1.id): 1, str(m2.id): 1 if strong else 2,
                   str(mt.id): {"0": right_id(mt.id, 0), "1": right_id(mt.id, 1 if strong else 2)}}
            if strong:
                ans[str(f.id)] = SECRET_FILL
            self.run_candidate(f"A{i}", ans)

    def page(self):
        t = Client()
        t.force_login(self.trainer)
        return t.get(reverse("assessments:exam_analysis", args=[self.exam.pk]))

    def test_summary_sections_mix_and_findings(self):
        self.candidates(12)
        summary, items = analysis.analyse(self.exam)
        self.assertEqual(summary["n"], 12)
        self.assertEqual(summary["mean_pct"], round(100 * summary["mean"] / summary["total"], 1))
        self.assertLessEqual(summary["low_pct"], summary["median_pct"])
        self.assertLessEqual(summary["median_pct"], summary["high_pct"])
        self.assertIsNotNone(summary["std_pct"])
        self.assertIsNotNone(summary["overall"])
        banded = [i for i in items if i["band"]]
        self.assertEqual(summary["mix_total"], len(banded))
        self.assertEqual(sum(m["count"] for m in summary["mix"]), len(banded))
        self.assertTrue(all(0 <= s["pct"] <= 100 for s in summary["sections"]))
        texts = " ".join(t for _k, t in summary["insights"])
        self.assertIn("Candidates averaged", texts)
        self.assertIsNotNone(summary["hist"])
        self.assertIsNotNone(summary["scatter"])                         # 12 candidates: discrimination exists
        mcq2 = items[1]
        self.assertEqual(mcq2["disc"], 1.0)
        self.assertEqual(mcq2["disc_label"], "Good")
        self.assertTrue(mcq2["short"])

    def test_a_wrongly_keyed_question_is_called_out_in_plain_english(self):
        self.candidates(8, strong_share=0.0)                             # nobody picks the keyed answer on mcq 2
        summary, items = analysis.analyse(self.exam)
        self.assertTrue(items[1]["needs_review"])
        self.assertTrue(any(k == "danger" and "Check the answer key" in t for k, t in summary["insights"]))
        self.assertTrue(any(a["n"] == 2 for a in summary["attention"]))

    def test_small_groups_get_a_caution_not_a_verdict(self):
        self.candidates(2)
        summary, _ = analysis.analyse(self.exam)
        self.assertFalse(summary["judged"])
        self.assertTrue(any("Only 2 submitted" in t for _k, t in summary["insights"]))
        self.assertEqual(summary["attention"], [])
        self.assertIsNone(summary["scatter"])

    def test_the_page_shows_the_charts_and_explanations(self):
        self.candidates(12)
        page = self.page()
        for needle in ("What this tells you", "Score distribution", 'class="hbar"', "Performance by question type",
                       "Question difficulty mix", "Every question at a glance", "Which questions are working?",
                       'class="dot dot-', "Question by question", "How to read this page", "Print / save as PDF",
                       'id="q1"', "Show as a table", "of marks earned"):
            self.assertContains(page, needle)
        self.assertNotContains(page, "<svg")                              # plain HTML charts, no SVG dependency

    def test_the_scatter_waits_for_enough_candidates_and_says_why(self):
        self.candidates(6)
        page = self.page()
        self.assertNotContains(page, "Which questions are working?</h2>")
        self.assertContains(page, "at least 10 candidates")

    def test_csv_export_is_unchanged(self):
        self.candidates(6)
        t = Client()
        t.force_login(self.trainer)
        body = t.get(reverse("assessments:exam_analysis", args=[self.exam.pk]) + "?format=csv").content.decode()
        self.assertIn("% fully correct", body.splitlines()[0])
