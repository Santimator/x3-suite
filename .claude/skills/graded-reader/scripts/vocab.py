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

    def is_known(self, word: str) -> bool:
        return word in self.known

    def is_chengyu(self, word: str) -> bool:
        return word in self.chengyu

    def all_chars_known(self, word: str) -> bool:
        """True if every Han character in the word is individually known."""
        chars = [c for c in word if _is_han(c)]
        return bool(chars) and all(c in self.known_chars for c in chars)

    def char_known(self, ch: str) -> bool:
        return ch in self.known_chars

    def get(self, word: str) -> Optional[Entry]:
        return self.entries.get(word)


def _is_han(ch: str) -> bool:
    return "一" <= ch <= "鿿"


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


def load_vocab(lists_dir: Path = LISTS_DIR, configure_jieba: bool = True) -> Vocab:
    """Load and merge the lists. Optionally load them into jieba's dictionary."""
    v = Vocab()

    # Order matters: hsk first, then supplement/chengyu, then personal last so
    # personal overrides glosses/levels on conflict.
    for fname, source in (
        ("hsk.tsv", "hsk"),
        ("supplement.tsv", "supplement"),
        ("chengyu.tsv", "chengyu"),
        ("personal.tsv", "personal"),
    ):
        for e in _read_tsv(lists_dir / fname, source):
            v.entries[e.word] = e
            v.known.add(e.word)
            if source == "chengyu":
                v.chengyu.add(e.word)

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
