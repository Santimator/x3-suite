#!/usr/bin/env python3
"""pdf2epub pipeline self-test: run after changing any pdf2epub script.

Runs the full deterministic chain (triage -> extract_text -> restore ->
prepare -> build -> verify) on the goya-sueno fixture into a temp
directory, using the committed policy.json/draft.json as the only agent
decisions -- exactly what an interactive run produces, replayed. Also
exercises the OCR roundtrip from extract_ocr.py's own check, skipped with
a notice if the tesseract binary isn't installed.

No network, no LLM. Exit 0 = all good: python scripts/selftest.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import xml.dom.minidom
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import extract_ocr  # noqa: E402
import extract_text  # noqa: E402
import prepare as prepare_mod  # noqa: E402
import render_pages  # noqa: E402
import restore as restore_mod  # noqa: E402
import triage as triage_mod  # noqa: E402
import verify as verify_mod  # noqa: E402

sys.path.insert(0, str(SCRIPTS.parents[1] / "epub-builder" / "scripts"))
import build_epub  # noqa: E402

REPO = SCRIPTS.parents[3]
FIXTURE = REPO / "workspace" / "goya-sueno"

_all_ok = True


def check(label: str, ok: bool, detail="") -> bool:
    global _all_ok
    _all_ok = _all_ok and ok
    print(f"  {'ok' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    return ok


def run_chain(work: Path) -> None:
    source = work / "source.pdf"
    shutil.copy(FIXTURE / "source.pdf", source)
    shutil.copy(FIXTURE / "policy.json", work / "policy.json")
    shutil.copy(FIXTURE / "draft.json", work / "draft.json")

    print("1. triage")
    t_report = triage_mod.triage(source, samples=3)
    check("route is TEXT", t_report["route"] == "TEXT", t_report["route"])
    check("doubled_chars flagged", "doubled_chars" in t_report["flags"], t_report["flags"])

    print("2. extract_text")
    extract_dir = work / "extract"
    e_report = extract_text.run(source, extract_dir, None, "auto")
    check("18 pages processed", e_report["pages_processed"] == 18, e_report["pages_processed"])
    check("all pages deduped", len(e_report["dedupe_pages"]) == 18, e_report["dedupe_pages"])
    p2 = json.loads((extract_dir / "pages.jsonl").read_text(encoding="utf-8").splitlines()[1])
    check("page 2 first line dedupe-clean", p2["lines"][0]["text"].startswith("mismo la FNAC"),
          p2["lines"][0]["text"])

    print("3. restore")
    restore_dir = work / "restore"
    policy = json.loads((work / "policy.json").read_text(encoding="utf-8"))
    r_report, markdown = restore_mod.restore(extract_dir, policy)
    restore_dir.mkdir(parents=True, exist_ok=True)
    (restore_dir / "restored.md").write_text(markdown, encoding="utf-8")
    (restore_dir / "restore-report.json").write_text(json.dumps(r_report, indent=2))
    check("fidelity gate passes", r_report["gate_pass"], r_report)
    check("no low-9 comma remains", "‚" not in markdown)
    check("no line ends mid-word", not any(l.rstrip().endswith("-") for l in markdown.splitlines()))
    check("opens with the expected sentence",
          markdown.startswith("Prefiero que me quite el sueño Goya a que lo haga cualquier hijo de puta."),
          markdown[:80])

    print("4. prepare")
    p_report = prepare_mod.prepare(work, restore_dir)
    total_assigned = sum(c["paragraphs"] for c in p_report["chapters"]) + p_report["front_matter_dropped_paragraphs"]
    check("every paragraph assigned exactly once",
          total_assigned == p_report["total_paragraphs_in_restored"],
          f"{total_assigned} != {p_report['total_paragraphs_in_restored']}")
    check("ch01.md exists", (work / "chapters" / "ch01.md").exists())
    check("book.json exists", (work / "book.json").exists())

    print("5. build")
    epub_path = work / "build" / "goya-sueno.epub"
    chapters, meta = build_epub.assemble(work, None)
    build_epub.write_epub(epub_path, meta["title"], meta.get("author", ""),
                           meta.get("language", "es"), chapters, extended_css=True)
    with zipfile.ZipFile(epub_path) as z:
        names = z.namelist()
        check("mimetype stored first", bool(names) and names[0] == "mimetype")
        malformed = []
        for n in names:
            if n.endswith((".xhtml", ".opf")):
                try:
                    xml.dom.minidom.parseString(z.read(n))
                except Exception as e:
                    malformed.append((n, str(e)))
        check("all xhtml/opf well-formed", not malformed, malformed)

    print("6. verify")
    v_report = verify_mod.verify(epub_path, restore_dir)
    check("integrity passes", v_report["integrity_pass"], v_report["integrity_errors"])
    check("coverage passes", v_report["coverage_pass"], v_report)


def run_ocr_roundtrip(work: Path) -> None:
    print("7. OCR roundtrip (extract_ocr.py)")
    pytesseract, err = extract_ocr.get_pytesseract()
    if pytesseract is None:
        print(f"  skip  tesseract unavailable ({err})")
        return

    import pypdfium2 as pdfium

    source = FIXTURE / "source.pdf"
    pdf = pdfium.PdfDocument(source)
    img1 = render_pages.render_page(pdf, 1, 200).convert("RGB")
    img2 = render_pages.render_page(pdf, 2, 200).convert("RGB")
    synth = work / "synthetic_scan.pdf"
    img1.save(synth, format="PDF", save_all=True, append_images=[img2])

    t_report = triage_mod.triage(synth, samples=1)
    check("synthetic scanned PDF routes OCR", t_report["route"] == "OCR", t_report["route"])

    ocr_dir = work / "ocr_extract"
    extract_ocr.run(synth, ocr_dir, None, "spa", 300, 6)
    p1 = json.loads((ocr_dir / "pages.jsonl").read_text(encoding="utf-8").splitlines()[0])
    p1_text = " ".join(ln["text"] for ln in p1["lines"])
    check("OCR'd page 1 contains the opening phrase", "Prefiero que me quite" in p1_text, p1_text[:200])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pdf2epub-selftest-") as tmp:
        work = Path(tmp)
        run_chain(work)
        run_ocr_roundtrip(work)

    print("PASS" if _all_ok else "FAIL")
    return 0 if _all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
