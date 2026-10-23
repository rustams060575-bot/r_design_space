"""Language helpers: names, a cheap script-based detector and code normalisation."""
from __future__ import annotations

LANG_NAMES = {  # code -> English name (used in the pitch prompt)
    "en": "English", "ru": "Russian", "es": "Spanish", "de": "German", "fr": "French", "pt": "Portuguese",
    "it": "Italian", "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "uk": "Ukrainian", "ja": "Japanese",
    "ko": "Korean", "zh": "Chinese", "ar": "Arabic", "hi": "Hindi", "id": "Indonesian", "th": "Thai",
    "vi": "Vietnamese", "kk": "Kazakh",
}
LANG_RU = {  # code -> name shown in Telegram cards
    "en": "английский", "ru": "русский", "es": "испанский", "de": "немецкий", "fr": "французский",
    "pt": "португальский", "it": "итальянский", "nl": "нидерландский", "pl": "польский", "tr": "турецкий",
    "uk": "украинский", "ja": "японский", "ko": "корейский", "zh": "китайский", "ar": "арабский",
    "hi": "хинди", "id": "индонезийский", "th": "тайский", "vi": "вьетнамский", "kk": "казахский",
}
CYRILLIC_HINTS = {"uk", "kk", "be", "bg", "sr"}


def normalize(code: str | None) -> str:
    """'pt-BR' -> 'pt'; unknown codes fall back to English."""
    code = (code or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in LANG_NAMES else "en"


def detect_language(text: str, hint: str | None = None) -> str:
    """Script-based guess; Latin-script text falls back to the region hint, then English."""
    letters = [c for c in text if c.isalpha()]
    if letters:
        share = lambda pred: sum(1 for c in letters if pred(c)) / len(letters)
        if share(lambda c: "Ѐ" <= c <= "ӿ") > .3:
            return normalize(hint) if (hint or "").split("-")[0] in CYRILLIC_HINTS else "ru"
        if share(lambda c: "가" <= c <= "힯") > .2:
            return "ko"
        if share(lambda c: "぀" <= c <= "ヿ") > .1:
            return "ja"
        if share(lambda c: "一" <= c <= "鿿") > .3:
            return "zh"
        if share(lambda c: "؀" <= c <= "ۿ") > .3:
            return "ar"
        if share(lambda c: "ऀ" <= c <= "ॿ") > .3:
            return "hi"
        if share(lambda c: "฀" <= c <= "๿") > .3:
            return "th"
    return normalize(hint)
