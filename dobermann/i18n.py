"""Languages: one catalog per language, the Italian text as the key.

    _("Internet assente")                      -> "Internet down" when the language is English
    _("durato {d}", d="3m 05s")                -> "lasted 3m 05s"

The texts were written in Italian, so Italian needs no catalog: a key is its own Italian text. Every
other language has dobermann/locale/<code>.json, {"italian text": "translation"}. A text missing from
a catalog falls back to English, then to the Italian key, so a gap shows up as a readable sentence and
never as an error; tests/test_i18n.py checks that no catalog has gaps.

One language for the whole install (config.json -> language): the panel and the mails speak it. The
wizard sets it, the panel's language menu changes it.
"""
import json
import os
import re
from collections import OrderedDict

LANGUAGES = OrderedDict([("en", "English"), ("it", "Italiano"), ("es", "Español"), ("de", "Deutsch"),
                         ("fr", "Français")])
DEFAULT = "en"
SOURCE = "it"
LOCALE_DIR = os.path.join(os.path.dirname(__file__), "locale")

_catalogs = {}
_current = DEFAULT


def catalog(code):
    """The translations of one language ({} for Italian, or for a language without a file)."""
    if code == SOURCE or code not in LANGUAGES:
        return {}
    if code not in _catalogs:
        try:
            with open(os.path.join(LOCALE_DIR, code + ".json"), encoding="utf-8") as f:
                _catalogs[code] = json.load(f)
        except FileNotFoundError:
            _catalogs[code] = {}
    return _catalogs[code]


def available():
    """The languages that can be chosen: Italian, and those with a catalog. A language is offered only
    once all of it is there."""
    return OrderedDict((c, n) for c, n in LANGUAGES.items()
                       if c == SOURCE or os.path.exists(os.path.join(LOCALE_DIR, c + ".json")))


def normalize(code):
    """A supported language code from anything: 'it_IT.UTF-8' -> 'it', unknown -> the default."""
    code = (code or "").strip().lower().replace("-", "_").split("_")[0].split(".")[0]
    return code if code in available() else DEFAULT


def set_language(code):
    """The language from here on. A known language without its catalog yet speaks English (gettext's
    fallback); anything else is normalised."""
    global _current
    _current = code if code in LANGUAGES else normalize(code)


def language():
    return _current


def gettext(key, lang=None):
    lang = lang or _current
    if lang == SOURCE:
        return key
    return catalog(lang).get(key) or catalog(DEFAULT).get(key) or key


def _(key, **kw):
    text = gettext(key)
    return text.format(**kw) if kw else text


def N_(key):
    """Marks a text for translation where it is defined (a table read at import time), without
    translating it there: the code that shows it calls _() on it, in the language of that moment."""
    return key


def ngettext(one, many, n, **kw):
    """The singular or the plural text, by n. Every language here has just these two forms."""
    return _(one if n == 1 else many, n=n, **kw)


# --- the panel's templates ----------------------------------------------------------------------
# In HTML, [[text]] is replaced by its (HTML-escaped) translation. In the pages' JavaScript, _t('text')
# and _t("text") read a dictionary of exactly the texts that page uses, which js_catalog() builds.

HTML_MARK = re.compile(r"\[\[(.+?)\]\]", re.S)
JS_CALL = re.compile(r"""\b_t\((?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")""")


def _js_unescape(s):
    """The JavaScript string's value for the escapes a key may contain: quotes and the backslash."""
    return re.sub(r"\\(['\"\\])", r"\1", s)


def template_keys(tpl):
    """(HTML keys, JavaScript keys) used by a template."""
    html_keys = [m.group(1) for m in HTML_MARK.finditer(tpl)]
    js_keys = [_js_unescape(m.group(1) if m.group(1) is not None else m.group(2)) for m in JS_CALL.finditer(tpl)]
    return html_keys, js_keys


def translate_template(tpl, escape):
    """The template with every [[text]] translated (through `escape`) and, if the page has a
    /*__T__*/ slot, the dictionary its t() calls read."""
    out = HTML_MARK.sub(lambda m: escape(gettext(m.group(1))), tpl)
    if "/*__T__*/" in out:
        _, js_keys = template_keys(tpl)
        table = {k: gettext(k) for k in js_keys if gettext(k) != k}
        out = out.replace("/*__T__*/", json.dumps(table, ensure_ascii=False).replace("</", "<\\/"))
    return out
