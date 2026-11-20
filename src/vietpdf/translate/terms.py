"""Dịch đúng thuật ngữ: trích ứng viên trên máy, đề xuất bằng LLM, kèm gốc ở lần đầu, kiểm tra sau dịch,
báo cáo nhất quán. Xem docs/v1.0.md, mục "Thuật ngữ"."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from vietpdf.core.ir import DocTerm, Segment
from vietpdf.translate.base import Translator, TranslatorError
from vietpdf.translate.glossary import Glossary, GlossaryEntry
from vietpdf.translate.prompts import TERMS_SCHEMA, terms_messages
from vietpdf.translate.qa import sanitize_output

STOPWORDS = set(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "from",
        "by",
        "with",
        "without",
        "and",
        "or",
        "nor",
        "but",
        "if",
        "then",
        "than",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "we",
        "our",
        "you",
        "your",
        "they",
        "their",
        "he",
        "she",
        "his",
        "her",
        "them",
        "which",
        "who",
        "whom",
        "whose",
        "what",
        "when",
        "where",
        "why",
        "how",
        "all",
        "any",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "not",
        "only",
        "own",
        "same",
        "so",
        "too",
        "very",
        "can",
        "will",
        "just",
        "should",
        "now",
        "also",
        "may",
        "might",
        "must",
        "shall",
        "would",
        "could",
        "do",
        "does",
        "did",
        "done",
        "has",
        "have",
        "had",
        "having",
        "into",
        "over",
        "under",
        "again",
        "further",
        "once",
        "here",
        "there",
        "about",
        "above",
        "below",
        "between",
        "through",
        "during",
        "before",
        "after",
        "up",
        "down",
        "out",
        "off",
        "et",
        "al",
        "i",
        "e",
        "g",
        "ie",
        "eg",
        "using",
        "use",
        "used",
        "based",
        "via",
        "new",
        "one",
        "two",
        "three",
        "first",
        "second",
        "however",
        "thus",
        "therefore",
        "while",
        "where",
        "whereas",
        "show",
        "shows",
        "shown",
        "proposed",
        "paper",
        "work",
        "results",
        "result",
        "method",
        "methods",
        "approach",
        "section",
        "figure",
        "table",
        "equation",
        "see",
        "given",
    ]
)
_ACRONYM_RE = re.compile(r"\b((?:[A-Z][a-z]+[\s-]){1,6}[A-Z]?[a-z]+)\s*\(([A-Z][A-Za-z]{1,7}?)s?\)")
_DEF_RE = re.compile(
    r"\b([A-Za-z][\w-]{0,40}(?:\s[\w-]{1,40}){0,3})\s+(?:is defined as|is called|refers to|denotes|is termed)\b", re.I
)
_STYLED_RE = re.compile(r"⟦([BI])⟧([^⟦]{2,60})⟦/\1⟧")
_INNER_OK = {"of", "and", "to"}
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]*[A-Za-z]|[A-Za-z]")


@dataclass
class Candidate:
    term: str
    count: int
    example: str
    page: int
    kind: str  # ngram | acronym | definition | styled


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def extract_candidates(segments: list[Segment], limit: int = 60, min_count: int = 2) -> list[Candidate]:
    """Tìm ứng viên thuật ngữ trên máy (không tốn token)."""
    counts: Counter[str] = Counter()
    example: dict[str, tuple[str, int]] = {}
    special: dict[str, str] = {}

    def note(term: str, sent: str, page: int, kind: str | None = None) -> None:
        key = term.strip().lower()
        if not key or key in STOPWORDS or len(key) < 3:
            return
        counts[key] += 1
        example.setdefault(key, (sent[:300], page))
        if kind:
            special.setdefault(key, kind)

    for seg in segments:
        if seg.role not in ("body", "title", "caption"):
            continue
        raw = seg.source
        for m in _STYLED_RE.finditer(raw):
            words = m.group(2).split()
            if 1 <= len(words) <= 4:
                note(m.group(2), seg.plain_source(), seg.page, "styled")
        text = seg.plain_source()
        for m in _ACRONYM_RE.finditer(text):
            note(m.group(1), text, seg.page, "acronym")
            note(m.group(2), text, seg.page, "acronym")
        for m in _DEF_RE.finditer(text):
            note(m.group(1), text, seg.page, "definition")
        for sent in re.split(r"(?<=[.!?])\s+", text):
            words = [w.lower() for w in _WORD_RE.findall(sent)]
            for n in (2, 3, 4):
                for i in range(len(words) - n + 1):
                    gram = words[i : i + n]
                    if gram[0] in STOPWORDS or gram[-1] in STOPWORDS or any(len(w) < 2 for w in gram):
                        continue
                    # bên trong cụm chỉ cho phép vài hư từ nối ("degree of freedom", "trial and error")
                    if any(w in STOPWORDS and w not in _INNER_OK for w in gram[1:-1]):
                        continue
                    note(" ".join(gram), sent, seg.page)
    # bỏ n-gram con nếu một n-gram dài hơn 1 từ chứa nó (ở đầu hoặc cuối) có cùng số lần xuất hiện — O(n)
    for longer in [k for k in counts if counts[k] >= min_count and " " in k]:
        words = longer.split()
        for sub in (" ".join(words[:-1]), " ".join(words[1:])):
            if sub in counts and counts[sub] <= counts[longer] and sub not in special:
                counts[sub] = 0
    scored = [
        (counts[k] * (len(k.split()) + (2 if k in special else 0)), k)
        for k in counts
        if counts[k] >= min_count or k in special
    ]
    scored.sort(reverse=True)
    out = []
    for _, k in scored[:limit]:
        sent, page = example[k]
        out.append(Candidate(k, counts[k] or 1, sent, page, special.get(k, "ngram")))
    return out


async def propose_terms(
    translator: Translator, candidates: list[Candidate], glossary: Glossary, domain: str, target_lang: str = "vi"
) -> list[DocTerm]:
    """Ứng viên chưa có trong glossary -> nhờ LLM đề xuất bản dịch, chính sách, định nghĩa."""
    known = {e.en.lower() for e in glossary.entries}
    unknown = [c for c in candidates if c.term.lower() not in known]
    terms: list[DocTerm] = []
    for c in candidates:
        if c.term.lower() in known:
            e = next(x for x in glossary.entries if x.en.lower() == c.term.lower())
            terms.append(
                DocTerm(en=e.en, vi=e.vi, policy=e.policy, count=c.count, definition=e.note, source="glossary")
            )
    if not unknown:
        return terms
    payload = [{"term": c.term, "count": c.count, "example": c.example} for c in unknown]
    try:
        data = await translator.complete_json(terms_messages(payload, domain, target_lang), "terms", TERMS_SCHEMA)
    except TranslatorError:
        return terms
    allowed = {c.term.lower(): c for c in unknown}
    for row in (data or {}).get("terms", []) if isinstance(data, dict) else []:
        if not isinstance(row, dict):
            continue
        en = str(row.get("en", "")).strip()
        cand = allowed.get(en.lower())
        vi = sanitize_output(str(row.get("vi", "")), max(len(en), 20))
        if cand is None or not vi or len(vi) > 120:
            continue  # chỉ nhận thuật ngữ nằm trong danh sách đã gửi
        policy = "keep" if row.get("policy") == "keep" else "translate"
        conf = row.get("confidence", 0)
        definition = sanitize_output(str(row.get("definition", "")), 400) or ""
        terms.append(
            DocTerm(
                en=c.term,
                vi=c.term if policy == "keep" else vi,
                policy=policy,
                count=cand.count,
                definition=definition[:1000],
                source="llm",
                approved=isinstance(conf, (int, float)) and conf >= 0.5,
            )
        )
    return terms


def doc_terms_to_entries(terms: list[DocTerm]) -> list[GlossaryEntry]:
    return [GlossaryEntry(t.en, t.vi, t.definition[:300], t.policy) for t in terms if t.approved]


def strict_term_tokens(text: str, glossary: Glossary, strict_all: bool) -> dict[str, str]:
    """Chế độ nghiêm ngặt: dạng nguyên văn -> bản dịch chuẩn, cho mục 'keep' (luôn) và mọi mục (khi bật)."""
    out: dict[str, str] = {}
    for surface, e in glossary.find(text):
        if e.policy == "keep" or strict_all:
            out[surface] = e.en if e.policy == "keep" else e.vi
    return out


def missing_in(source_plain: str, translation_plain: str, glossary: Glossary) -> list[GlossaryEntry]:
    """Thuật ngữ có trong bản gốc nhưng bản dịch không dùng đúng bản dịch chuẩn."""
    tr = _norm(translation_plain)
    out = []
    for e in glossary.match(source_plain):
        expected = e.en if e.policy == "keep" else e.vi
        if _norm(expected) not in tr:
            out.append(e)
    return out


def missing_terms(seg: Segment, glossary: Glossary) -> list[GlossaryEntry]:
    if not seg.translation:
        return []
    return missing_in(seg.plain_source(), seg.plain_translation(), glossary)


def apply_first_mention(segments: list[Segment], glossary: Glossary, scope: str = "document") -> int:
    """Kèm thuật ngữ gốc trong ngoặc: 'học máy (machine learning)'.
    scope="document": chỉ lần xuất hiện đầu tiên trên toàn tài liệu; scope="paragraph": lần đầu trong MỖI đoạn
    (chế độ học tiếng Anh — người đọc luôn thấy từ gốc ngay cạnh bản dịch)."""
    seen: set[str] = set()
    changed = 0
    for seg in segments:
        if seg.status != "translated" or not seg.translation or seg.role not in ("body", "caption", "ocr", "title"):
            continue
        if scope == "paragraph":
            seen = set()
        for e in glossary.match(seg.plain_source()):
            key = e.en.lower()
            if key in seen or e.policy == "keep" or _norm(e.vi) == _norm(e.en):
                seen.add(key)
                continue
            seen.add(key)
            idx = _norm(seg.translation).find(_norm(e.vi))
            if idx < 0:
                continue
            end = idx + len(e.vi)
            if seg.translation[end : end + 2] == " (":
                continue
            seg.translation = f"{seg.translation[:end]} ({e.en}){seg.translation[end:]}"
            changed += 1
    return changed


def consistency_report(units: list[tuple[int, str, str]], glossary: Glossary) -> list[dict[str, Any]]:
    """units: (trang, bản gốc, bản dịch) — một đơn vị dịch (đoạn hoặc chuỗi đoạn nối).
    Trả về thuật ngữ nào không được dịch đúng bản chuẩn, ở những trang nào."""
    pages: dict[str, set[int]] = defaultdict(set)
    occurrences: Counter[str] = Counter()
    entry: dict[str, GlossaryEntry] = {}
    for page, src, tr in units:
        for e in glossary.match(src):
            occurrences[e.en] += 1
            entry[e.en] = e
        for e in missing_in(src, tr, glossary):
            pages[e.en].add(page + 1)
    return [
        {
            "term": en,
            "expected": entry[en].en if entry[en].policy == "keep" else entry[en].vi,
            "occurrences": occurrences[en],
            "pages_missing": sorted(p),
        }
        for en, p in sorted(pages.items())
    ]
