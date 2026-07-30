"""Comment-quality heuristics shared by the Python and generic analyzers."""
import re

# Phrases that signal the model hedged instead of implementing.
HEDGING_PHRASES = (
    "in a real implementation",
    "in a real-world",
    "in a real app",
    "in a real application",
    "in a full implementation",
    "in production, you",
    "in production you",
    "for simplicity",
    "simplified version",
    "for demonstration",
    "demonstration purposes",
    "for the sake of",
    "you would typically",
    "this is a placeholder",
    "placeholder for",
    "this is a mock",
    "for now, we",
    "for now we",
    "as an ai",
    "left as an exercise",
    "actual implementation would",
    "real logic goes here",
    "logic would go here",
)

_STOPWORDS = {
    "the", "a", "an", "to", "of", "for", "and", "or", "is", "are", "be",
    "this", "that", "we", "it", "in", "on", "with", "then", "now", "if",
    "as", "will", "was", "by", "at", "from", "into", "its", "our", "you",
    "here", "there", "also", "just", "should", "can", "do", "does",
}

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _content_words(text):
    words = set()
    for w in _WORD_RE.findall(text):
        w = w.lower()
        if len(w) > 2 and w not in _STOPWORDS:
            words.add(w)
    return words


def _code_words(code):
    """Identifiers in a code line, with snake_case and camelCase split apart."""
    words = set()
    for ident in _WORD_RE.findall(code):
        words.add(ident.lower())
        for part in ident.split("_"):
            if part:
                words.add(part.lower())
        for part in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+", ident):
            words.add(part.lower())
    return words


_BANNER_RUN = re.compile(r"([─═━=\-#*~_.])\1{3,}")


def is_banner(comment_text):
    """Section-divider comments (`── Pick mode ────`) organize, not restate."""
    return bool(_BANNER_RUN.search(comment_text))


_CODEISH = re.compile(r"^[^A-Za-z\s]|[;{}]\s*$|=>|\)\s*;|::")


def looks_like_code(comment_text):
    """Commented-out code isn't a comment about code."""
    return bool(_CODEISH.search(comment_text.strip()))


def hedging_phrase(comment_text):
    low = comment_text.lower()
    for phrase in HEDGING_PHRASES:
        if phrase in low:
            return phrase
    return None


def redundancy(comment_text, code_line):
    """How much of the comment is already spelled out by the code line.

    Returns "full", "partial", or None.
    """
    cwords = _content_words(comment_text)
    if len(cwords) < 2:
        return None
    kwords = _code_words(code_line)
    if not kwords:
        return None
    covered = len(cwords & kwords) / len(cwords)
    if covered >= 0.999:
        return "full"
    if covered >= 0.75 and len(cwords) >= 3:
        return "partial"
    return None
