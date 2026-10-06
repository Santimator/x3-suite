"""Shared pieces of the pdf2epub chunk harness: the job file, chunk paths, the
chunk markdown's two intermediate constructs, and the text normalization the
fidelity gate and verify.py both use.

Workspace (one folder per conversion, `workspace/<slug>/`):

  source.pdf                the input, never modified
  job.json                  plan.py: device, figure mode, metadata, the chunks
  build/triage.json         plan.py (via triage.py)
  extract/                  plan.py (via extract_text.py): pages.jsonl, text/pNNN.txt
  pages/pNNN.png            plan.py (via render_pages.py): what the model reads
  figures/candidates.json   plan.py / figures.py add: regions that can become images
  figures/previews/ID.png   a look at each candidate
  chunks/cNN/input.json     plan.py: the chunk's pages, text, figure candidates
  chunks/cNN/out.md         the model: the chunk, in chunk markdown
  chunks/cNN/check.json     check_chunk.py: the gate's verdict on out.md
  chunks/cNN/review.json    the reviewer (required where there is no text layer)
  figures/prepared.json     figures.py prepare: device-sized images/ + snippets
  chapters/, book.json      assemble.py: the common book format
  build/<slug>.epub         epub-builder

Chunk markdown is the builder's FORMAT.md plus two constructs that assemble.py
resolves, so the builder never sees them:

  [[fig:ID | caption]]   own paragraph; a figure candidate placed here, with
                         its printed caption (none: [[fig:ID]]).
                         A third field overrides the job's figure mode for
                         this one: `| single`, `| double`, or `| inline`
                         (page width, never turned or split — a photo that
                         only illustrates)
  [[continues]]          last line; the chunk's last paragraph runs on into the
                         next chunk's first one (joined, de-hyphenated)
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parents[2]
BUILDER_SCRIPTS = REPO_ROOT / "epub-builder" / "scripts"

FIGURE_MODES = ("single", "double")

PLACEHOLDER_RE = re.compile(
    r"^\[\[fig:([A-Za-z0-9_-]+)\s*(?:\|\s*([^|\]]*?))?\s*(?:\|\s*(single|double|inline))?\s*\]\]$")
CONTINUES = "[[continues]]"


class HarnessError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Job + chunks
# --------------------------------------------------------------------------- #
def load_job(workdir: Path) -> dict:
    path = workdir / "job.json"
    if not path.exists():
        raise HarnessError(f"no {path} — run plan.py first (it asks for the device "
                           f"and the figure mode)")
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def chunk_dir(workdir: Path, cid: str) -> Path:
    return workdir / "chunks" / cid


def sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_candidates(workdir: Path) -> dict:
    """{id: candidate} from figures/candidates.json (empty if none)."""
    data = load_json(workdir / "figures" / "candidates.json", {"candidates": []})
    return {c["id"]: c for c in data["candidates"]}


def chunk_state(workdir: Path, chunk: dict) -> dict:
    """Where one chunk stands, from the files alone (so any driver can resume):
    written? gated, and on the current out.md? reviewed, if review is needed?"""
    d = chunk_dir(workdir, chunk["id"])
    digest = sha256(d / "out.md")
    check = load_json(d / "check.json")
    review = load_json(d / "review.json")
    gated = bool(check and check.get("sha256") == digest and digest)
    needs_review = not chunk["text_layer"]
    reviewed = bool(review and review.get("sha256") == digest and digest
                    and review.get("verdict") == "ok")
    if digest is None:
        state = "todo"
    elif not gated:
        state = "unchecked"
    elif not check["pass"]:
        state = "failed"
    elif needs_review and not reviewed:
        state = "needs-review"
    else:
        state = "done"
    return {"state": state, "sha256": digest, "needs_review": needs_review}


# --------------------------------------------------------------------------- #
# Placeholders
# --------------------------------------------------------------------------- #
def placeholders(md: str) -> list:
    """[(id, caption, mode or None)] for every [[fig:...]] paragraph, in order."""
    out = []
    for block in re.split(r"\n\s*\n", md):
        m = PLACEHOLDER_RE.match(block.strip())
        if m:
            out.append((m.group(1), (m.group(2) or "").strip(), m.group(3)))
    return out


# --------------------------------------------------------------------------- #
# Text normalization (the fidelity gate's and verify.py's common ground)
# --------------------------------------------------------------------------- #
# A line-break hyphen: "sustancial-\nmente". Joined before comparing, so a
# chunk that de-hyphenates correctly is not punished for it.
LINE_HYPHEN = re.compile(r"(\w)[-­]\s*\n\s*(?=[^\W\d_])")
WORD = re.compile(r"\w+")


def markdown_to_text(md: str) -> str:
    """Chunk/chapter markdown -> its reading text: drop the markup the builder
    renders as structure (heading markers, verse fences, emphasis stars,
    footnote markers, image syntax, harness placeholders)."""
    lines = []
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("```") or s == CONTINUES or PLACEHOLDER_RE.match(s):
            continue
        s = re.sub(r"^#+\s*", "", s)
        s = re.sub(r"^!\[([^\]]*)\]\([^)]*\)$", r"\1", s)
        s = re.sub(r"\[\^[^\]]+\]:?", " ", s)
        s = s.replace("*", "")
        lines.append(s)
    return "\n".join(lines)


def tokens(text: str) -> list:
    text = unicodedata.normalize("NFKC", text)
    text = LINE_HYPHEN.sub(r"\1", text)
    return [w.lower() for w in WORD.findall(text)]


def ngram_set(toks: list, n: int) -> set:
    return {tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)}


def uncovered_runs(toks: list, other: set, n: int) -> list:
    """Maximal runs of tokens that no shared n-gram covers: [(start, end)].
    One mismatched word leaves a run of one; a dropped or invented sentence
    leaves a run as long as the sentence."""
    covered = [False] * len(toks)
    for i in range(len(toks) - n + 1):
        if tuple(toks[i:i + n]) in other:
            for j in range(i, i + n):
                covered[j] = True
    runs, start = [], None
    for i, c in enumerate(covered + [True]):
        if not c and start is None:
            start = i
        elif c and start is not None:
            runs.append((start, i))
            start = None
    return runs
