"""The tests read the texts in Italian, the language they are written in (the catalogs' keys): an
install with no language chosen speaks English, so the tests choose Italian. tests/test_i18n.py
checks the other languages."""
from dobermann import config, i18n

config.DEFAULTS["language"] = "it"
i18n.set_language("it")
