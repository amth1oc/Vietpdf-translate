"""Kiểm tra bản dịch ngay trên máy (không tốn token): số liệu, phủ định, chiều so sánh, độ dài,
và làm sạch đầu ra từ LLM (dữ liệu không tin cậy)."""

from __future__ import annotations

import re
import unicodedata

_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_PLACEHOLDER = re.compile(r"⟦[^⟧]{1,14}⟧")
_NEG_EN = re.compile(r"\b(?:not|no|never|without|cannot|neither|nor|none|fail(?:s|ed)?\s+to)\b|n't\b", re.IGNORECASE)
_NEG_VI = re.compile(r"\b(?:không|chưa|chẳng|chả|thiếu|đừng|chớ|vô|phi|bất|kém|ngoài|trừ|hề|nào)\b", re.IGNORECASE)
_UP_EN = re.compile(r"\b(?:increase[sd]?|higher|more|rise[sn]?|grow(?:s|th)?|improve[sd]?)\b", re.IGNORECASE)
_DOWN_EN = re.compile(r"\b(?:decrease[sd]?|lower|less|fewer|drop(?:s|ped)?|reduce[sd]?|decline[sd]?)\b", re.IGNORECASE)
_UP_VI = re.compile(r"\b(?:tăng|cao|nhiều|lớn|cải thiện|hơn)\b", re.IGNORECASE)
_DOWN_VI = re.compile(r"\b(?:giảm|thấp|ít|nhỏ|kém|suy)\b", re.IGNORECASE)
_SPACE_LIKE = {"\u2028", "\u2029", "\u0085", "\r", "\n", "\t", "\x0b", "\x0c"}
# Ký tự hiển thị TRỐNG nhưng không thuộc nhóm Cf (L1-015, 2.1.2): Hangul filler, Braille trống, combining grapheme
# joiner, variation selector, khoảng trắng Mongolian/Ogham... dùng được để giấu/đệm chữ.
BLANK_CHARS = frozenset(
    "\u3164\u115f\u1160\uffa0\u2800\u034f\u180e\u1680\u17b4\u17b5\u2065\u3000\u00ad"
    + "".join(chr(c) for c in range(0xFE00, 0xFE10))  # variation selectors VS1–VS16
    + "".join(chr(c) for c in range(0xE0100, 0xE01F0))  # VS17–VS256
)


def _strip_invisible(t: str) -> str:
    """Bỏ mọi ký tự điều khiển (Cc), định dạng (Cf: bidi, zero-width, BOM, U+061C...), riêng (Co) và chưa gán (Cn);
    ngắt dòng/đoạn Unicode thành dấu cách. Dùng danh mục Unicode thay vì danh sách cứng (L1-015)."""
    out = []
    for ch in t:
        if ch in _SPACE_LIKE:
            out.append(" ")
        elif unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cn") or ch in BLANK_CHARS:
            continue
        else:
            out.append(ch)
    return "".join(out)


def sanitize_output(text: str, source_len: int) -> str | None:
    """NFC, bỏ ký tự điều khiển/định dạng/bidi, giới hạn độ dài. None nếu bản dịch không dùng được."""
    t = _strip_invisible(unicodedata.normalize("NFC", text))
    t = re.sub(r" {2,}", " ", t).strip()
    if not t:
        return None
    if len(t) > source_len * 4 + 200:
        return None  # dài bất thường: nghi LLM bịa thêm hoặc bị prompt injection
    return t


def _numbers(text: str) -> list[str]:
    return [n.rstrip(".,") for n in _NUM.findall(_PLACEHOLDER.sub(" ", text))]


_POSSIBLE_EN = re.compile(r"\b(?:may|might|possibly|perhaps|likely|unlikely|presumably)\b", re.IGNORECASE)
_POSSIBLE_VI = re.compile(
    r"(?:có thể|có lẽ|khả năng|chưa chắc|dường như|e rằng|nhiều khả năng|ít khả năng)", re.IGNORECASE
)
_OBLIGE_EN = re.compile(
    r"\b(?:must|should|need to|needs to|have to|has to|is required to|are required to)\b", re.IGNORECASE
)
_OBLIGE_VI = re.compile(r"(?:phải|nên|cần|bắt buộc|yêu cầu|đòi hỏi)", re.IGNORECASE)
_EN_WORD = re.compile(r"\b[a-z][a-z\-]{3,}\b")
_PAREN = re.compile(r"\([^()]{0,120}\)")


def untranslated_words(source: str, translation: str, allowed: set[str] | None = None) -> list[str]:
    """Từ tiếng Anh (chữ thường, ≥4 chữ cái) của bản gốc còn nguyên trong bản dịch — bỏ qua phần trong ngoặc
    (chú thích thuật ngữ gốc), ký hiệu giữ chỗ, và các từ được phép giữ nguyên (thuật ngữ 'keep')."""
    allowed = {a.lower() for a in (allowed or set())}
    src_words = set(_EN_WORD.findall(_PLACEHOLDER.sub(" ", source)))
    tr = _PAREN.sub(" ", _PLACEHOLDER.sub(" ", translation))
    return [w for w in _EN_WORD.findall(tr) if w in src_words and w not in allowed]


def check_meaning(
    source: str, translation: str, source_lang: str = "en", keep_terms: set[str] | None = None
) -> list[str]:
    flags: list[str] = []
    src_nums = _numbers(source)
    tgt = translation
    if any(n not in tgt for n in src_nums):
        flags.append("qa:number")
    if source_lang.split("-", maxsplit=1)[0] != "en":
        return flags  # các kiểm tra dưới dựa trên từ tiếng Anh
    leftover = untranslated_words(source, translation, keep_terms)
    if len(leftover) >= 3 and len(leftover) / max(len(translation.split()), 1) > 0.15:
        flags.append("qa:untranslated")
    if (_POSSIBLE_EN.search(source) and not _POSSIBLE_VI.search(translation)) or (
        _OBLIGE_EN.search(source) and not _OBLIGE_VI.search(translation)
    ):
        flags.append("qa:modality")
    if _NEG_EN.search(source) and not _NEG_VI.search(translation):
        flags.append("qa:negation")
    up_en, down_en = bool(_UP_EN.search(source)), bool(_DOWN_EN.search(source))
    up_vi, down_vi = bool(_UP_VI.search(translation)), bool(_DOWN_VI.search(translation))
    if (up_en and not down_en and down_vi and not up_vi) or (down_en and not up_en and up_vi and not down_vi):
        flags.append("qa:direction")
    if len(source) > 40:
        ratio = len(translation) / len(source)
        if ratio < 0.5 or ratio > 3.0:
            flags.append("qa:length")
    return flags


# Ký tự đồng hình (khung xương UTS #39, phần hay gặp) -> Latin, áp SAU NFKC + casefold. NFKC đã xử lý chữ toàn
# chiều rộng, chữ toán học, chữ khoanh tròn...
_CONFUSABLES = str.maketrans(
    {
        # Kirin
        "а": "a", "в": "b", "е": "e", "ё": "e", "з": "3", "і": "i", "ї": "i", "ј": "j", "к": "k", "м": "m", "н": "h",
        "о": "o", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s", "ԁ": "d", "һ": "h", "ӏ": "l", "ԛ": "q",
        "ԝ": "w", "ѵ": "v", "ү": "y", "ɡ": "g",
        # Hy Lạp
        "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
        "υ": "u", "χ": "x", "ω": "w", "γ": "y",
        # Latin biến thể không tách dấu được
        "ı": "i", "ł": "l", "ø": "o", "đ": "d", "ð": "d", "ħ": "h", "ŧ": "t", "ƒ": "f", "ß": "ss",
    }
)  # fmt: skip
_NON_WORD = re.compile(r"[^0-9a-z]+")


def normalize_for_detection(text: str) -> str:
    """Chuẩn hoá trước khi so mẫu injection (L1-010, 2.1.2): NFKC, casefold, ký tự đồng hình -> Latin, bỏ dấu (gồm
    đ->d), bỏ ký tự Cf/điều khiển/"chữ trống", mọi ký tự không phải chữ-số thành 1 dấu cách. Chỉ dùng để PHÁT HIỆN,
    không dùng để dịch."""
    t = unicodedata.normalize("NFKC", text).casefold().translate(_CONFUSABLES)
    t = "".join(
        ch
        for ch in unicodedata.normalize("NFD", t)
        if unicodedata.category(ch) not in ("Mn", "Me", "Cf", "Cc", "Co", "Cn") and ch not in BLANK_CHARS
    )
    return _NON_WORD.sub(" ", t.translate(_CONFUSABLES)).strip()


_W = r"(?:\s+\w+){0,3}?\s+"  # tối đa 3 từ chen giữa
_INJECTION_RE = re.compile(
    # tiếng Anh (văn bản đã chuẩn hoá: chữ thường, không dấu, chỉ chữ-số và dấu cách)
    r"\b(ignore|disregard|forget|override|bypass)"
    + _W
    + r"(instructions?|prompts?|rules?|directions?|guidance|guidelines?|commands?|orders?)\b"
    r"|\bsystem\s+prompt|\byou\s+are\s+now\b|\bact\s+as\s+(an?\s+)?(ai|assistant|dan)\b|\bjailbreak"
    r"|\b(note|summary|instructions?|message|notice|reminder|memo|request)\s+(to|for)\s+(the\s+)?"
    r"(ai|translator|assistant|model|llm|language\s+model|chatbot|translation\s+(system|engine|model|tool))\b"
    r"|\b(to|dear|hey|attention|hello)\s+(the\s+)?(ai|llm|language\s+model|assistant|translator|chatbot"
    r"|translation\s+(system|engine|model|tool))\s+(reading|processing|translating|that\s+reads|who\s+reads)\b"
    r"|\btranslators?\s+note\b|\b(all|every)\s+(later|subsequent|following|other)\s+translations?\s+(must|should)\b"
    r"|\b(respond|reply|answer)\s+only\s+with\b|\boutput\s+(only\s+)?the\s+(text|string|word)\b"
    r"|\b(when|while)\s+translating\b|\bfrom\s+now\s+on\b.{0,60}\b(render|translate|output|swap)\b"
    r"|\bswap\s+every\s+(number|word|figure|value)\b"
    r"|\b(render|translate|write)\s+\w+\s+as\s+\w+\s+(instead|from\s+now)\b"
    # tiếng Việt đã bỏ dấu (bắt cả viết có dấu lẫn không dấu)
    r"|\bbo\s+qua(\s+(moi|tat\s+ca|cac|toan\s+bo))?\s+(chi\s+dan|huong\s+dan|yeu\s+cau|lenh|quy\s+tac)"
    r"|\b(luu\s+y|ghi\s+chu|chu\s+y|nhan|loi\s+nhan)\s+(cho|voi|danh\s+cho|gui)\s+(nguoi\s+dich|ai|mo\s+hinh|tro\s+ly"
    r"|bien\s+dich|he\s+thong(\s+dich)?|may\s+dich|cong\s+cu\s+dich)"
    r"|\b(hay|phai)\s+dich\s+.{1,40}\s+thanh\b|\bdoi\s+(moi|tat\s+ca|cac)\s+.{0,30}\s+thanh\b"
    r"|\bkhong\s+(duoc\s+)?nhac\s+(toi|den)\s+(luu\s+y|chi\s+dan)|\b(gui|nhan)\s+toi\s+(he\s+thong|mo\s+hinh)\s+dich",
)
# Dạng "dính liền" (bỏ cả dấu cách): bắt chữ bị chèn khoảng trắng/ký tự vô hình giữa từ (vd. "Ign ore").
_INJECTION_COMPACT = re.compile(
    r"(ignore|disregard|forget|override)(all|any|every|the|of|your)*(previous|prior|above|earlier|preceding|existing"
    r"|original)(instructions?|prompts?|rules?|directions?|guidance|guidelines?|commands?)"
    r"|systemprompt|boqua(moi|tatca|cac)?(chidan|huongdan|yeucau)|luuy(cho|danhcho)(nguoidich|hethong|mohinh)",
)


# Thẻ/vai trò kiểu chat: so trên văn bản NFKC + casefold + đồng hình (còn dấu câu)
_ROLE_RE = re.compile(
    r"<\s*/?\s*(system|assistant|instructions?)\s*>|\[\s*/?\s*(system|inst)\s*\]|(^|\n|\s)(assistant|system)\s*:"
)


def injection_like(text: str) -> bool:
    """Đoạn PDF trông như đang cố ra lệnh cho mô hình (prompt injection). So trên văn bản ĐÃ CHUẨN HOÁ
    (`normalize_for_detection`) và dạng dính liền. Đây chỉ là TÍN HIỆU, không phải rào chắn: đoạn khớp được gắn
    `security:injection-like`, dịch riêng không kèm ngữ cảnh và kiểm đầu ra (pipeline)."""
    norm = normalize_for_detection(text)
    if not norm:
        return False
    raw = unicodedata.normalize("NFKC", text).casefold().translate(_CONFUSABLES)
    return bool(_INJECTION_RE.search(norm) or _INJECTION_COMPACT.search(norm.replace(" ", "")) or _ROLE_RE.search(raw))


def injection_in_window(texts: list[str]) -> bool:
    """Câu lệnh bị tách qua 2–3 đoạn liền nhau (B8): chỉ đúng khi CẢ cửa sổ khớp mà từng đoạn riêng lẻ không khớp."""
    return injection_like(" ".join(texts))


_URL_EMAIL = re.compile(r"(?:https?://|www\.)\S+|[\w.+-]+@[\w-]+\.[\w.-]+", re.IGNORECASE)
_CODEISH = re.compile(r"`[^`]+`|<[a-z/][^>]{0,40}>|\{\{|\}\}|\$\{")
_VI_MARK = re.compile(r"[ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]", re.IGNORECASE)


def output_anomalies(source: str, translation: str, target_lang: str = "vi") -> list[str]:
    """Kiểm bản dịch của đoạn bị nghi injection (2.1.2): độ dài lệch, URL/email/mã mới xuất hiện, sai ngôn ngữ đích.
    Trả danh sách cờ `security:output-*` (rỗng = không thấy bất thường)."""
    flags: list[str] = []
    s, t = source.strip(), translation.strip()
    if not t:
        return flags
    ratio = len(t) / max(len(s), 1)
    if len(s) >= 20 and not 0.4 <= ratio <= 3.0:
        flags.append("security:output-length")
    src_links = {m.lower() for m in _URL_EMAIL.findall(s)}
    if any(m.lower() not in src_links for m in _URL_EMAIL.findall(t)) or (
        _CODEISH.search(t) and not _CODEISH.search(s)
    ):
        flags.append("security:output-new-link")
    if target_lang.startswith("vi") and len(re.findall(r"[^\W\d_]", t)) >= 20 and not _VI_MARK.search(t):
        flags.append("security:output-language")
    return flags
