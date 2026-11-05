"""人工更正 → 自动记忆：把签名格的 OCR 误读记成该人员的别名。

背景：检验员那一列是手写签名，识别模型基本认不准。实测同一格「杜文杰」被读成
「定生」，置信度只有 0.35；换一张单子又可能读成「村文走」之类。逐次人工纠正成本高，
但如果连纠正结果都不留下，下次还得再纠一遍。

所以在这里做一件很小的事：**人工把名字改正的那一刻，把签名格原文记进该人员别名**。
下次同样的写法直接命中，不再报「检验员未匹配」，也不用点开原图去认那三个字。

三条克制（缺一条都会把名册搞脏）：
- 只在人工确认后写入。识别侧绝不自己造别名，否则错一次就永久错下去。
- 原文已被别人占用（等于另一个人的名字或别名）时不动，避免张冠李戴。
- 每人最多 `_MAX_ALIASES` 条，超出丢最早的，防止手误把名册灌满。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Worker
from ..ocr.normalize import clean_text, match_worker

_MAX_ALIASES = 20


def _split(text: str) -> list[str]:
    return [a for a in re.split(r"[/、|;,，\s]+", text or "") if a]


def _roster(db: Session) -> list[tuple[int, str, str]]:
    return [
        (w.id, w.name, w.aliases or "")
        for w in db.scalars(select(Worker).where(Worker.active)).all()
    ]


def remember(db: Session, raw: str, name: str, user: str = "") -> str:
    """把签名原文 `raw` 记成 `name` 的别名。返回真正写入的别名，没写返回空串。

    调用方负责留痕（这里的留痕写在 worker 的 aliases 字段上）。
    """
    raw = clean_text(raw)
    name = clean_text(name)
    if len(raw) < 2 or not name or raw == name:
        return ""
    target = db.scalar(select(Worker).where(Worker.name == name))
    if not target:
        return ""

    aliases = _split(target.aliases)
    if raw in aliases:
        return ""

    # 原文已经被别人占了（另一个人的真名或别名）→ 不抢。
    # 逐字比对 2 字对 3 字必然低分，所以先看全名/别名精确命中，再看模糊分。
    for wid, other_name, other_aliases in _roster(db):
        if wid == target.id:
            continue
        if raw in [other_name, *(_split(other_aliases))]:
            return ""
    wid, matched_name, score = match_worker(raw, _roster(db))
    if wid and matched_name != name and score >= 0.6:
        return ""

    aliases.append(raw)
    target.aliases = ",".join(aliases[-_MAX_ALIASES:])
    return raw
