#!/usr/bin/env python3
"""Vocabulary model shared by validate.py and build_epub.py.

Loads the leveled lists (HSK base + personal overlay + chengyu), merges them
into a single known-words model, and configures jieba so segmentation
boundaries match the list. This last part is critical: if jieba splits a
multi-character list word, the validator's out-of-list rate becomes a lie.

Lists live in ../lists relative to this file:
  hsk.tsv       base leveled list (word, level, pinyin, gloss)
  personal.tsv  personal known-words overlay + add-and-gloss escalation sink
  chengyu.tsv   idioms / fixed expressions kept as whole tokens

All three share the same 4-column TSV schema. Lines beginning with '#' and
blank lines are ignored, so the files can carry comments.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

LISTS_DIR = Path(__file__).resolve().parent.parent / "lists"


@dataclass
class Entry:
    word: str
    level: str
    pinyin: str
    gloss: str
    source: str  # "hsk" | "personal" | "chengyu"


@dataclass
class Vocab:
    """Merged vocabulary model.

    entries:    word -> Entry (personal overlay wins on conflict)
    known:      set of all words considered in-list / known
    chengyu:    set of idiom words (the tier-(b) whitelist)
    known_chars: set of individually-known characters. A character counts as
                 known if it is a single-char list entry OR appears in any
                 known word. Rationale: the official HSK list is word-based and
                 omits many standalone characters that compose its own entries
                 (e.g. it has 说话 but not 说, 爬山 but not 山). A learner who
                 knows those words has met those characters, so recombinations
                 built only from them are tier-(c) "stretch", not failures.
                 This is what keeps the out-of-list rate honest.
    """

    entries: Dict[str, Entry] = field(default_factory=dict)
    known: Set[str] = field(default_factory=set)
    chengyu: Set[str] = field(default_factory=set)
    known_chars: Set[str] = field(default_factory=set)
    # Multi-word constructions the scribe is asked to actually use, from
    # expressions.tsv: name -> (compiled regex or None, Entry). A None regex
    # means the expression is a literal set phrase and matches as plain text.
    expressions: Dict[str, tuple] = field(default_factory=dict)

    def is_known(self, word: str) -> bool:
        return word in self.known

    def is_chengyu(self, word: str) -> bool:
        return word in self.chengyu

    def find_expressions(self, text: str) -> List[str]:
        """Distinct expressions from expressions.tsv occurring in `text`.

        Counted per distinct expression, not per hit, so a chapter that repeats
        太…了 five times still shows one construction — the gate is about
        variety, not frequency."""
        found = []
        for name, (rx, _entry) in self.expressions.items():
            if rx.search(text) if rx else (name in text):
                found.append(name)
        return found

    def all_chars_known(self, word: str) -> bool:
        """True if every Han character in the word is individually known."""
        chars = [c for c in word if _is_han(c)]
        return bool(chars) and all(c in self.known_chars for c in chars)

    def char_known(self, ch: str) -> bool:
        return ch in self.known_chars

    def decomposes_known(self, word: str) -> bool:
        """True if the word splits entirely into known list entries (2+ pieces).

        jieba merges frequent collocations (很快, 就要, 只能) into single tokens
        that are not list entries themselves. If every piece of such a token is
        a word the learner was taught, reading it is recognition, not a guess —
        so the validator should not spend the stretch budget on it.
        """
        n = len(word)
        if n < 2 or n > 8:
            return False
        reach = [True] + [False] * n  # reach[i]: word[:i] splits into entries
        for i in range(1, n + 1):
            for j in range(i):
                if reach[j] and (i - j) < n and word[j:i] in self.known:
                    reach[i] = True
                    break
        return reach[n]

    def get(self, word: str) -> Optional[Entry]:
        return self.entries.get(word)


def _is_han(ch: str) -> bool:
    return "一" <= ch <= "鿿"


# Number grammar, not vocabulary: ordinals (第五, 第十一名) and number+measure
# combos (十二个, 一次, 两年, 一个月). jieba merges these into single tokens; a
# learner who knows the numerals and the measure word reads them at sight.
_NUMERALS = set("〇零一二两三四五六七八九十百千万亿几半")
_MEASURES = set("个只条块座名年月天次步位件章场遍岁层家口点分")


def is_number_pattern(token: str) -> bool:
    """True for 第+numeral(+measure) ordinals and numeral(+个)+measure combos."""
    body = token[1:] if token.startswith("第") else token
    if len(body) >= 2 and body[-1] in _MEASURES:
        body = body[:-1]
        if body and body[-1] == "个":  # 一个月-style: numeral + 个 + unit
            body = body[:-1]
    return bool(body) and all(c in _NUMERALS for c in body)


def _read_tsv(path: Path, source: str) -> Iterable[Entry]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        header_seen = False
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not header_seen and line.split("\t")[0].strip() == "word":
                header_seen = True
                continue
            parts = line.split("\t")
            # tolerate short rows
            word = parts[0].strip() if len(parts) > 0 else ""
            if not word:
                continue
            level = parts[1].strip() if len(parts) > 1 else ""
            pinyin = parts[2].strip() if len(parts) > 2 else ""
            gloss = parts[3].strip() if len(parts) > 3 else ""
            yield Entry(word=word, level=level, pinyin=pinyin, gloss=gloss, source=source)


def _read_expressions(path: Path):
    """Yield (Entry, compiled_regex_or_None) from expressions.tsv.

    Columns: expression, level, pinyin, gloss, regex. A blank regex marks a
    literal set phrase."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        word = parts[0].strip()
        if not word or word == "expression":
            continue
        level = parts[1].strip() if len(parts) > 1 else ""
        pinyin = parts[2].strip() if len(parts) > 2 else ""
        gloss = parts[3].strip() if len(parts) > 3 else ""
        pattern = parts[4].strip() if len(parts) > 4 else ""
        rx = re.compile(pattern) if pattern else None
        yield Entry(word=word, level=level, pinyin=pinyin, gloss=gloss,
                    source="expression"), rx


def level_ceiling(text: Optional[str]) -> Optional[int]:
    """Highest HSK band implied by a level string, or None if unbounded.
    "HSK3" -> 3, "HSK1-3" -> 3, "HSK 1-4" -> 4, "" / None -> None."""
    if not text:
        return None
    nums = [int(n) for n in re.findall(r"\d+", text)]
    return max(nums) if nums else None


def load_vocab(lists_dir: Path = LISTS_DIR, configure_jieba: bool = True,
               max_level: Optional[str] = None, book_dir: Optional[Path] = None) -> Vocab:
    """Load and merge the lists. Optionally load them into jieba's dictionary.

    `book_dir` loads that book's own `vocab.tsv` — the names, places and props
    only this story needs (source `book`). It is deliberately *not* part of the
    stable `lists/`: it lives and dies with the book, so retiring a book takes
    its vocabulary with it and never pollutes another book's brief. Only
    `lists/personal.tsv` is the reader's own standing overlay, and it belongs to
    the user, not to any book.

    `max_level` (e.g. "HSK3" or "HSK1-3") caps the HSK base list to that band
    and below — so higher-band words fall out of the known set and are caught
    as stretch/flagged, which is what makes a level target real. Off by default
    (None = load every band), so books without the cap are unaffected. The
    supplement (function words everyone at level knows), chengyu, and personal
    overlays are never capped."""
    v = Vocab()
    cap = level_ceiling(max_level)

    # Order matters: hsk first, then supplement/chengyu, then personal last so
    # personal overrides glosses/levels on conflict.
    for fname, source in (
        ("hsk.tsv", "hsk"),
        ("supplement.tsv", "supplement"),
        ("chengyu.tsv", "chengyu"),
        ("personal.tsv", "personal"),
    ):
        for e in _read_tsv(lists_dir / fname, source):
            if source == "hsk" and cap is not None:
                lvl = level_ceiling(e.level)
                if lvl is not None and lvl > cap:
                    continue
            v.entries[e.word] = e
            v.known.add(e.word)
            if source == "chengyu":
                v.chengyu.add(e.word)

    # The book's own names/places/props, if we were told which book. Loaded
    # after the shared lists so a book may override a gloss for its own use.
    if book_dir is not None:
        for e in _read_tsv(Path(book_dir) / "vocab.tsv", "book"):
            v.entries[e.word] = e
            v.known.add(e.word)

    # Expressions: multi-word constructions, level-capped like the HSK bands.
    # Literal set phrases also join `known` (so they don't read as out-of-list);
    # split patterns like 一…就… never do — they are matched by regex, not as
    # tokens, and their parts are ordinary words anyway.
    for e, rx in _read_expressions(lists_dir / "expressions.tsv"):
        if cap is not None:
            lvl = level_ceiling(e.level)
            if lvl is not None and lvl > cap:
                continue
        v.expressions[e.word] = (rx, e)
        if rx is None:
            v.entries.setdefault(e.word, e)
            v.known.add(e.word)

    # Known-character set drives the compositional stretch tier. Every Han
    # character appearing in any known word counts (see Vocab.known_chars note).
    for word in v.known:
        for ch in word:
            if _is_han(ch):
                v.known_chars.add(ch)

    if configure_jieba:
        configure_segmenter(v)

    return v


def configure_segmenter(v: Vocab) -> None:
    """Load every multi-character list word into jieba so it segments as a unit.

    Uses a high frequency so list words win over jieba's defaults. Without this,
    e.g. 北京 / 没关系 could be split and counted as out-of-list fragments.
    """
    import jieba

    jieba.setLogLevel(60)  # silence the build-dictionary chatter
    for word in v.known:
        if len(word) >= 2:
            jieba.add_word(word, freq=100000)


def segment(text: str) -> List[str]:
    """Segment text into tokens with jieba (call load_vocab first to configure)."""
    import jieba

    return list(jieba.cut(text, HMM=False))


def pinyin_for(word: str, v: Optional[Vocab] = None) -> str:
    """Return space-joined pinyin for a word.

    Prefers the list's stored pinyin (already proofed); falls back to pypinyin
    for anything not in the lists (e.g. tier-(c) stretch words, proper nouns).
    """
    if v is not None:
        e = v.get(word)
        if e and e.pinyin:
            return e.pinyin
    from pypinyin import lazy_pinyin, Style

    return " ".join(lazy_pinyin(word, style=Style.TONE))


if __name__ == "__main__":
    # Smoke test / quick stats.
    v = load_vocab()
    print(f"entries:      {len(v.entries)}")
    print(f"known words:  {len(v.known)}")
    print(f"chengyu:      {len(v.chengyu)}")
    print(f"known chars:  {len(v.known_chars)}")
    sample = "孙悟空是一个很厉害的妖怪。"
    print(f"segment({sample!r}) -> {segment(sample)}")
    for w in ("北京", "妖怪", "厉害"):
        e = v.get(w)
        tag = f"{e.level}/{e.source}" if e else "OUT-OF-LIST"
        print(f"  {w}: {tag}  pinyin={pinyin_for(w, v)}")
