"""Load JSON catalogs with English fallback and named formatting arguments."""

import json
from pathlib import Path

LANGUAGES = Path(__file__).resolve().parent.parent / "languages"


class Translator:
    def __init__(self, language="en"):
        if language not in ("en", "es"):
            language = "en"
        self.language = language
        self.english = json.loads((LANGUAGES / "en.json").read_text(encoding="utf-8"))
        self.messages = json.loads(
            (LANGUAGES / f"{language}.json").read_text(encoding="utf-8")
        )

    def __call__(self, key, **values):
        text = self.messages.get(key, self.english.get(key, key))
        return text.format_map(values) if values else text
