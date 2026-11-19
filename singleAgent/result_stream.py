"""将 LLM 流式 Markdown 拆为 chunk（正文）与 aux（附属说明）。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Generator, Iterable, List, Optional, Tuple

# (标记文本, aux_kind)
AUX_MARKERS: List[Tuple[str, str]] = [
    ("【结语】", "conclusion"),
    ("**结语：**", "conclusion"),
    ("**结语:**", "conclusion"),
    ("【总结】", "recap"),
    ("**总结：**", "recap"),
    ("**总结:**", "recap"),
    ("【小结】", "summary"),
    ("**小结：**", "summary"),
    ("**小结:**", "summary"),
    ("【免责声明】", "disclaimer"),
    ("**免责声明：**", "disclaimer"),
    ("**免责声明:**", "disclaimer"),
    ("**免责声明**", "disclaimer"),
    ("【备注】", "remark"),
    ("**备注：**", "remark"),
    ("**备注:**", "remark"),
    ("**备注**", "remark"),
]

_MARKER_STRINGS = [m for m, _ in AUX_MARKERS]

SECTION_FOUR_HINTS = (
    "## 四、处置建议",
    "## 四、",
    "## 四 ",
)

# 仅在见过第四章标题后启用的结构分界（引用块、分隔线等）
STRUCTURAL_AUX_RULES: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\n\n>\s"), "remark"),
    (re.compile(r"\n>\s"), "remark"),
    (re.compile(r"。\s*\n?\s*>\s"), "remark"),
    (re.compile(r"\n---\n"), "unknown"),
    (re.compile(r"\n\n\*\*备注"), "remark"),
    (re.compile(r"\n\n\*\*免责声明"), "disclaimer"),
]

# 第四章之后，引用块/页脚可能尚未完整到达，需 holdback
_POST_SECTION_FOUR_TAIL = re.compile(
    r"(\n\n|\n|。)\s*$|(\n\n?)?>\s*$|(\n\n?)?>\s*\**\s*$"
)

_HOLDABACK_STRINGS = _MARKER_STRINGS + [
    "\n\n>",
    "\n---\n",
    "\n\n**备",
    "\n\n**免",
    "\n\n**结",
    "\n\n**总",
    "\n\n**小",
    "\n\n【",
]


@dataclass(frozen=True)
class StreamPiece:
    stream_type: str  # chunk | aux
    text: str
    aux_kind: Optional[str] = None


def _holdback_len(text: str, *, section_four_seen: bool = False) -> int:
    best = 0
    for prefix in _HOLDABACK_STRINGS:
        for i in range(1, len(prefix) + 1):
            if text.endswith(prefix[:i]):
                best = max(best, i)
    if section_four_seen and _POST_SECTION_FOUR_TAIL.search(text):
        best = max(best, len(text))
    return best


def _find_aux_split(text: str, *, section_four_seen: bool) -> Optional[Tuple[int, str, int]]:
    """返回 (位置, aux_kind, 跳过的标记长度)。"""
    best_pos: Optional[int] = None
    best_kind = "unknown"
    best_skip = 0

    for marker, kind in AUX_MARKERS:
        pos = text.find(marker)
        if pos == -1:
            continue
        if best_pos is None or pos < best_pos or (pos == best_pos and len(marker) > best_skip):
            best_pos = pos
            best_kind = kind
            best_skip = len(marker)

    if section_four_seen:
        for pattern, kind in STRUCTURAL_AUX_RULES:
            m = pattern.search(text)
            if not m:
                continue
            pos = m.start()
            if best_pos is None or pos < best_pos:
                best_pos = pos
                best_kind = kind
                best_skip = 0

    if best_pos is None:
        return None
    return best_pos, best_kind, best_skip


class ResultStreamClassifier:
    """流式分类：正文 chunk vs 附属 aux（带 aux_kind）。灵活/模板模式均启用。"""

    def __init__(self) -> None:
        self._buffer = ""
        self._in_aux = False
        self._aux_kind = "unknown"
        self._section_four_seen = False

    def feed(self, delta: str) -> List[StreamPiece]:
        if not delta:
            return []
        self._buffer += delta
        self._mark_section_four(self._buffer)
        return self._drain(force=False)

    def flush(self) -> List[StreamPiece]:
        return self._drain(force=True)

    def _mark_section_four(self, text: str) -> None:
        if self._section_four_seen:
            return
        for hint in SECTION_FOUR_HINTS:
            if hint in text:
                self._section_four_seen = True
                return

    def _drain(self, *, force: bool) -> List[StreamPiece]:
        outputs: List[StreamPiece] = []

        while self._buffer:
            if not self._in_aux:
                split = _find_aux_split(self._buffer, section_four_seen=self._section_four_seen)
                if split is not None:
                    pos, kind, skip = split
                    before = self._buffer[:pos]
                    if before:
                        outputs.append(StreamPiece("chunk", before))
                    self._buffer = self._buffer[pos + skip :]
                    self._in_aux = True
                    self._aux_kind = kind
                    self._mark_section_four(self._buffer)
                    continue

                if not force:
                    hold = _holdback_len(self._buffer, section_four_seen=self._section_four_seen)
                    if hold == len(self._buffer):
                        break
                    safe_len = len(self._buffer) - hold
                else:
                    safe_len = len(self._buffer)

                if safe_len <= 0:
                    break
                outputs.append(StreamPiece("chunk", self._buffer[:safe_len]))
                self._buffer = self._buffer[safe_len:]
                continue

            # aux 模式：若出现新标记则更新 aux_kind
            switched = False
            for marker, kind in AUX_MARKERS:
                pos = self._buffer.find(marker)
                if pos == 0:
                    self._buffer = self._buffer[len(marker) :]
                    self._aux_kind = kind
                    switched = True
                    break
                if pos > 0:
                    outputs.append(StreamPiece("aux", self._buffer[:pos], self._aux_kind))
                    self._buffer = self._buffer[pos:]
                    switched = True
                    break
            if switched:
                continue

            if not force:
                hold = _holdback_len(self._buffer, section_four_seen=True)
                if hold == len(self._buffer):
                    break
                safe_len = len(self._buffer) - hold
            else:
                safe_len = len(self._buffer)

            if safe_len <= 0:
                break
            outputs.append(StreamPiece("aux", self._buffer[:safe_len], self._aux_kind))
            self._buffer = self._buffer[safe_len:]

        return outputs


def iter_classified_result_chunks(
    deltas: Iterable[str],
) -> Generator[StreamPiece, None, None]:
    classifier = ResultStreamClassifier()
    for delta in deltas:
        for piece in classifier.feed(delta):
            yield piece
    for piece in classifier.flush():
        yield piece


def extract_body_markdown(full_text: str) -> str:
    """从完整文本中提取纯正文（chunk 部分，不含 aux）。"""
    if not full_text:
        return ""
    classifier = ResultStreamClassifier()
    body_parts: List[str] = []
    for piece in classifier.feed(full_text):
        if piece.stream_type == "chunk":
            body_parts.append(piece.text)
    for piece in classifier.flush():
        if piece.stream_type == "chunk":
            body_parts.append(piece.text)
    return "".join(body_parts).strip()
