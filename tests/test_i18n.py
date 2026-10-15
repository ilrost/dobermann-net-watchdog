"""Languages: every catalog complete and consistent, and pages and mails really in that language."""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import i18n_keys  # noqa: E402
from dobermann import i18n  # noqa: E402

PLACEHOLDER = re.compile(r"\{[a-z0-9_]*(?:![rs])?\}")


class TestCatalogs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = i18n_keys.all_keys()

    def test_every_offered_language_has_every_text(self):
        self.assertIn("en", i18n.available())
        for code in i18n.available():
            if code == i18n.SOURCE:
                continue
            with self.subTest(code=code):
                self.assertEqual(i18n_keys.missing(code, self.keys), [])
                self.assertEqual(i18n_keys.unused(code, self.keys), [])

    def test_translations_keep_placeholders_and_markup(self):
        for code in i18n.LANGUAGES:
            for key, text in i18n.catalog(code).items():
                with self.subTest(code=code, key=key):
                    self.assertEqual(set(PLACEHOLDER.findall(key)), set(PLACEHOLDER.findall(text)))
                    self.assertEqual(key.count("<b>"), text.count("<b>"))
                    self.assertEqual(key.endswith(": "), text.endswith(": "))    # prompts keep their space

    def test_language_codes(self):
        self.assertEqual(i18n.normalize("it_IT.UTF-8"), "it")
        self.assertEqual(i18n.normalize("de-CH"), "de" if "de" in i18n.available() else "en")
        self.assertEqual(i18n.normalize("pt_BR"), "en")
        self.assertEqual(i18n.normalize(None), "en")


class TestInUse(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("it")

    def test_a_missing_text_falls_back_to_english_then_to_the_key(self):
        i18n.set_language("en")
        self.assertEqual(i18n._("Salva"), "Save")
        self.assertEqual(i18n._("testo che non esiste {v}", v=1), "testo che non esiste 1")

    def test_report_mail_in_english(self):
        from dobermann import narrative
        from dobermann.chain import Chain
        i18n.set_language("en")
        chain = Chain({"nodes": [{"id": "router", "label": "Router", "ip": "192.0.2.1", "adapter": "upnp_igd"}]})
        line, sentence = narrative.where("GATEWAY_UNRESPONSIVE", chain)
        self.assertEqual(sentence, "The fault was at home, between this device and Router.")
        self.assertEqual(narrative.fmt_dur(65), "1 minute and 5 s")
        self.assertEqual(narrative.fmt_dur(7260), "2 hours and 1 minute")
        self.assertIn("operator", line)

    def test_template_texts_are_translated_and_escaped(self):
        i18n.set_language("en")
        out = i18n.translate_template("<b>[[Salva]]</b><script>const T = /*__T__*/; _t('Oggi')</script>",
                                      lambda s: s.replace("<", "&lt;"))
        self.assertIn("<b>Save</b>", out)
        self.assertEqual(json.loads(out.split("const T = ")[1].split(";")[0]), {"Oggi": "Today"})

    def test_german_labels_keep_their_capital_mid_sentence(self):
        from dobermann import alerts
        i18n.set_language("de")
        self.assertEqual(alerts.lower_first("Internet"), "Internet")
        i18n.set_language("it")
        self.assertEqual(alerts.lower_first("Internet assente"), "internet assente")


if __name__ == "__main__":
    unittest.main()
