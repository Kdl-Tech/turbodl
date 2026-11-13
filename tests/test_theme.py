"""Thème de TurboDL : variable d'environnement, puis choix enregistré, puis système."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import turbodl  # noqa: E402


class TestTheme(unittest.TestCase):
    def test_resolution(self):
        sysd = lambda: "sombre"
        self.assertEqual(turbodl.resolve_theme(None, None, sysd), "sombre")
        self.assertEqual(turbodl.resolve_theme(None, "systeme", lambda: "clair"), "clair")
        self.assertEqual(turbodl.resolve_theme(None, "clair", sysd), "clair")
        self.assertEqual(turbodl.resolve_theme("clair", "sombre", sysd), "clair")
        self.assertEqual(turbodl.resolve_theme("n_importe", "inconnu", sysd), "sombre")

    def test_choix_proposes(self):
        self.assertEqual([v for v, _ in turbodl.THEME_CHOICES], ["systeme", "clair", "sombre"])
        self.assertTrue(all(v in turbodl.THEMES for v, _ in turbodl.THEME_CHOICES[1:]))


if __name__ == "__main__":
    unittest.main()
