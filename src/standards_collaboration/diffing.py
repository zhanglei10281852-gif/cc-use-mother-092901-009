"""条款文本差异：unified diff 与结构化变更行。"""

from __future__ import annotations

import difflib


def unified_diff(from_text: str, to_text: str, from_label: str, to_label: str) -> str:
    lines = difflib.unified_diff(
        from_text.splitlines(keepends=True),
        to_text.splitlines(keepends=True),
        fromfile=from_label,
        tofile=to_label,
    )
    return "".join(lines)


def change_blocks(from_text: str, to_text: str) -> list[dict]:
    """按行给出 replace/insert/delete/equal 变更块，便于 API 消费。"""
    matcher = difflib.SequenceMatcher(
        None, from_text.splitlines(), to_text.splitlines()
    )
    blocks: list[dict] = []
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        if op == "equal":
            continue
        blocks.append(
            {
                "op": op,
                "from_lines": from_text.splitlines()[a0:a1],
                "to_lines": to_text.splitlines()[b0:b1],
            }
        )
    return blocks
