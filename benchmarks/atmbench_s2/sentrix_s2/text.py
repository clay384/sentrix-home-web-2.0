"""Lossless text spans and explicit multilingual lexical tokens, without generated facts."""

import hashlib
import re
import unicodedata

TOKEN = re.compile(r"[\u3400-\u9fff]|[A-Za-zÀ-ÿ0-9]+(?:[-_./:@][A-Za-zÀ-ÿ0-9]+)*|[^\s]", re.UNICODE)
WORDS = re.compile(r"[\u3400-\u9fff]+|[a-zà-ÿ0-9]+(?:[-_./:@][a-zà-ÿ0-9]+)*")
FACT = re.compile(
    r"\d|改期|取消|不是|未|否认|金额|订单|地点|时间|refund|cancel|reschedul|instead|not\b|booking|invoice|reference|subject:|from:|to:",
    re.I,
)


def tokens(text):
    """CJK uni/bigrams plus exact codes and their components; no number stopword removal."""
    result = []
    for match in WORDS.finditer(unicodedata.normalize("NFKC", text).lower()):
        word = match.group()
        if "\u3400" <= word[0] <= "\u9fff":
            result.extend(word)
            result.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            result.append(word)
            parts = re.split(r"[-_./:@]", word)
            if len(parts) > 1:
                result.extend(parts)
    return result


def chunks(text, target=600, overlap=80):
    """Estimated multilingual lexical tokens; exact offsets into unchanged published text."""
    spans = list(TOKEN.finditer(text))
    if not spans:
        return []
    result, begin = [], 0
    while begin < len(spans):
        end = min(begin + target, len(spans))
        # Prefer a paragraph/sentence ending in the last quarter of the target.
        if end < len(spans):
            for i in range(end - 1, begin + target * 3 // 4, -1):
                stop = spans[i].end()
                if text[stop - 1 : stop] in ".!?。！？" or "\n" in text[stop : spans[i + 1].start()]:
                    end = i + 1
                    break
        start_char = 0 if begin == 0 else spans[begin].start()
        end_char = len(text) if end == len(spans) else spans[end].start()
        result.append((start_char, end_char, "paragraph", text[start_char:end_char]))
        if end == len(spans):
            break
        begin = max(begin + 1, end - overlap)
    return result


def fact_spans(text):
    """Extractive evidence cards: original sentences only; dates are unclassified mentions."""
    found = []
    for match in re.finditer(r"[^\n。！？!?]+[。！？!?]?|[^\n]+$", text):
        value = match.group()
        if FACT.search(value) and len(value.strip()) > 2:
            # Preserve source offsets and bound unusually unpunctuated paragraphs via normal chunks.
            if len(value) <= 1600:
                found.append((match.start(), match.end(), "extractive_fact", value))
    return found


def child_id(source_id, start, end, kind, text):
    return "chunk_" + hashlib.sha256(f"{source_id}\0{start}\0{end}\0{kind}\0{text}".encode()).hexdigest()[:24]


def preview(text, query, length=1400):
    terms = [x for x in dict.fromkeys(tokens(query)) if len(x) > 1 or x.isdigit()]
    lower = text.lower()
    positions = [lower.find(term.lower()) for term in terms]
    positions = [p for p in positions if p >= 0]
    # Select a window covering the most distinct query terms, not just the first common word.
    starts = [0] + [max(0, p - length // 4) for p in positions]
    start = max(starts, key=lambda p: (sum(term in lower[p : p + length] for term in terms), -p))
    return {
        "text": text[start : start + length],
        "offset": start,
        "end": min(len(text), start + length),
        "total_chars": len(text),
        "truncated": len(text) > length,
        "basis": "query_matched_released_text",
    }
