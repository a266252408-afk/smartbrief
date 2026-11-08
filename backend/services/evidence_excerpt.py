"""证据按段落或句末选取；目标预算可让完整句适度超出。"""
import re


def select_excerpt(text, target=500, ceiling=4000):
    text = str(text or "").strip()
    if len(text) <= target:
        return text
    paragraphs = [m.start() for m in re.finditer(r"\n\s*\n", text)]
    sentences = [m.end() for m in re.finditer(
        r'[。！？!?][”’"）)]*|(?<!\d)\.(?=\s+[A-Z]|$)|(?<=\d)\.(?=\s+[A-Z]|$)', text)]
    for boundaries in (paragraphs, sentences):
        after = [n for n in boundaries if target <= n <= ceiling]
        if after:
            return text[:min(after)].rstrip()
    if len(text) <= ceiling:
        return text
    before = [n for n in paragraphs + sentences if n <= ceiling]
    return text[:max(before)].rstrip() if before else ""
