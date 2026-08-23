#!/usr/bin/env python3
"""File, audit and explicitly edit EPUBs under catalog metadata.

This is the catalog's operation, not Telegram's. The bot calls it as a
subprocess; a terminal and any future importer can call the same interface.
Ordinary ingest, alias changes and catalog tidying never rewrite EPUB bytes.
The ``edit`` command is the deliberate exception: after a human confirms an
edit, it changes only the OPF metadata and preserves the rest of the book.

Examples:
  ingest_book.py --json inspect download.epub --library workspace/library
  ingest_book.py --json ingest download.epub --library workspace/library
  ingest_book.py --json ingest download.epub --library workspace/library --alias LOTR
  ingest_book.py audit --library workspace/library
  ingest_book.py tidy --library workspace/library
  ingest_book.py edit --library workspace/library
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import library  # noqa: E402
import epub_metadata  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFY = REPO_ROOT / "epub-builder" / "scripts" / "verify_epub.py"


class IngestError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _epub_readable(path: Path) -> Tuple[bool, str]:
    """A broad EPUB check suitable for third-party books.

    The suite's shared verifier is intentionally stricter: it verifies the
    exact output contract of *our* builder and can complain about a perfectly
    readable commercial EPUB. Ingest therefore gates only on the EPUB
    container/OPF being readable and carries the strict report as a warning.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            container = archive.read("META-INF/container.xml")
            root = ET.fromstring(container)
            rootfile = root.find(
                f"{library.CONTAINER_NS}rootfiles/{library.CONTAINER_NS}rootfile")
            opf_path = rootfile.get("full-path") if rootfile is not None else ""
            if not opf_path:
                return False, "META-INF/container.xml has no OPF rootfile"
            ET.fromstring(archive.read(opf_path))
    except FileNotFoundError:
        return False, "source file does not exist"
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        return False, f"not a readable EPUB: {exc}"
    return True, ""


def _strict_verify(path: Path) -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            [sys.executable, str(VERIFY), str(path)], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def inspect(source: Path, library_dir: Path) -> dict:
    source = Path(source).resolve()
    library_dir = Path(library_dir).resolve()
    readable, problem = _epub_readable(source)
    if not readable:
        return {"status": "invalid", "source": str(source), "error": problem}

    metadata = library.read_opf_metadata(source)
    author = metadata.get("author", "")
    series = metadata.get("series", "")
    series_index = metadata.get("series_index", "")
    embedded_title = metadata.get("title") or library.title_from_filename(source)
    base_title = library.clean_embedded_title(
        embedded_title, author, series, series_index)
    aliases = library.load_aliases(library_dir)
    alias = library.alias_for(series, aliases) if series else ""
    suggestion = library.suggest_alias(series, aliases.values()) if series and not alias else ""
    title = library.catalog_title(base_title, alias, series_index)
    verified, detail = _strict_verify(source)
    return {
        "status": "needs_alias" if series and not alias else "ready",
        "source": str(source),
        "base_title": base_title,
        "title": title,
        "author": author,
        "language": metadata.get("language", ""),
        "series": series,
        "series_index": series_index,
        "series_alias": alias,
        "suggested_alias": suggestion,
        "filename": library.canonical_filename(title, author),
        "verify_ok": verified,
        "verify_detail": detail,
        "sha256": _digest(source),
    }


def _read_alias_document(library_dir: Path) -> dict:
    try:
        return library.read_alias_document(library_dir)
    except ValueError as exc:
        raise IngestError(str(exc)) from exc


def _write_alias_document(library_dir: Path, data: dict) -> None:
    library.write_alias_document(library_dir, data)


def _updated_alias_document(library_dir: Path, series: str, alias: str) -> dict:
    try:
        return library.with_alias(_read_alias_document(library_dir), series, alias)
    except ValueError as exc:
        raise IngestError(str(exc)) from exc


def _rename_plan(books: Iterable[library.Book], alias_override=None
                 ) -> Tuple[List[Tuple[Path, Path]], List[str]]:
    plan: List[Tuple[Path, Path]] = []
    missing = set()
    if isinstance(alias_override, dict):
        overrides = {str(name).casefold(): str(alias)
                     for name, alias in alias_override.items()}
    elif alias_override:
        overrides = {str(alias_override[0]).casefold(): str(alias_override[1])}
    else:
        overrides = {}
    for book in books:
        alias = book.series_alias
        if book.series and book.series.casefold() in overrides:
            alias = overrides[book.series.casefold()]
        if book.series and not alias:
            missing.add(book.series)
            continue
        title = library.catalog_title(book.base_title, alias, book.series_index)
        source = Path(book.path)
        dest = source.with_name(library.canonical_filename(title, book.author))
        if source != dest:
            plan.append((source, dest))
    return plan, sorted(missing, key=str.casefold)


def _preflight(plan: Iterable[Tuple[Path, Path]]) -> None:
    plan = list(plan)
    sources = {source.resolve() for source, _ in plan}
    destinations: Dict[Path, Path] = {}
    for source, dest in plan:
        resolved_dest = dest.resolve()
        previous = destinations.get(resolved_dest)
        if previous and previous.resolve() != source.resolve():
            raise IngestError(
                f"two books would both become {dest.name!r}: {previous.name!r} and {source.name!r}")
        destinations[resolved_dest] = source
        if dest.exists() and resolved_dest not in sources:
            raise IngestError(f"refusing to overwrite existing {dest.name!r}")


def _apply_renames(plan: Iterable[Tuple[Path, Path]]) -> None:
    plan = list(plan)
    _preflight(plan)
    staged: List[Tuple[Path, Path, Path]] = []
    try:
        for n, (source, dest) in enumerate(plan):
            tmp = source.with_name(f".{source.name}.catalog-rename-{os.getpid()}-{n}")
            if tmp.exists():
                raise IngestError(f"temporary rename path already exists: {tmp.name}")
            os.replace(source, tmp)
            staged.append((source, tmp, dest))
        for source, tmp, dest in staged:
            os.replace(tmp, dest)
    except Exception:
        # Best effort back to the exact starting paths. No completed target is
        # ever overwritten: the preflight guaranteed they were ours.
        for source, tmp, dest in reversed(staged):
            current = tmp if tmp.exists() else dest
            if current.exists() and not source.exists():
                os.replace(current, source)
        raise


def set_alias(library_dir: Path, series: str, alias: str, *, dry_run: bool = False) -> dict:
    library_dir = Path(library_dir).resolve()
    data = _updated_alias_document(library_dir, series, alias)
    canonical_series = next(name for name in data["aliases"]
                            if str(name).casefold() == library._clean(series).casefold())
    canonical_alias = data["aliases"][canonical_series]
    books = library.scan([library_dir], [])
    matching = [book for book in books
                if book.series.casefold() == canonical_series.casefold()]
    plan, _ = _rename_plan(matching, (canonical_series, canonical_alias))
    _preflight(plan)
    if not dry_run:
        _apply_renames(plan)
        try:
            _write_alias_document(library_dir, data)
        except Exception:
            _apply_renames((dest, source) for source, dest in reversed(plan))
            raise
    return {
        "status": "dry_run" if dry_run else "alias_set",
        "series": canonical_series,
        "series_alias": canonical_alias,
        "changed": len(plan),
        "renames": [{"from": str(source), "to": str(dest)} for source, dest in plan],
    }


def _file_without_overwrite(source: Path, dest: Path) -> None:
    """Copy into place atomically, then consume the non-catalog source."""
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.ingest")
    try:
        with source.open("rb") as src, tmp.open("xb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        # Hard-link creation has O_EXCL semantics: it cannot replace an entry
        # that appeared after the preflight.
        os.link(tmp, dest)
        tmp.unlink()
        source.unlink()
    finally:
        tmp.unlink(missing_ok=True)


def _identical_catalog_file(library_dir: Path, source: Path,
                            digest: str) -> Optional[Path]:
    """Find the same bytes under an obsolete flat catalog name, if present."""
    try:
        size = source.stat().st_size
    except OSError:
        return None
    for candidate in sorted(library_dir.glob("*.epub")):
        try:
            if candidate.is_symlink():
                continue
            resolved = candidate.resolve()
            if (resolved == source or not candidate.is_file()
                    or candidate.stat().st_size != size):
                continue
            if _digest(candidate) == digest:
                return resolved
        except OSError:
            continue
    return None


def ingest(source: Path, library_dir: Path, alias: str = "") -> dict:
    source = Path(source).resolve()
    library_dir = Path(library_dir).resolve()
    report = inspect(source, library_dir)
    if report["status"] == "invalid":
        return report

    existing_alias = report.get("series_alias", "")
    if report.get("series") and alias:
        if existing_alias and existing_alias.casefold() != alias.casefold():
            return {**report, "status": "conflict",
                    "error": (f"{report['series']!r} already uses short name "
                              f"{existing_alias!r}; change the series alias explicitly")}
        if not existing_alias:
            try:
                set_alias(library_dir, report["series"], alias)
            except IngestError as exc:
                return {**report, "status": "conflict", "error": str(exc)}
            report = inspect(source, library_dir)

    if report["status"] == "needs_alias":
        return report

    library_dir.mkdir(parents=True, exist_ok=True)
    dest = library_dir / report["filename"]
    report["destination"] = str(dest)
    if source == dest.resolve():
        return {**report, "status": "filed"}
    if dest.exists():
        if _digest(dest) == report["sha256"]:
            return {**report, "status": "already_present",
                    "error": "an identical catalog copy already exists; source was left untouched"}
        return {**report, "status": "conflict",
                "error": f"refusing to overwrite different catalog file {dest.name!r}"}

    # Naming rules can become more precise after a book was filed. A repeat
    # upload of those exact bytes should repair that one catalog path, not make
    # a second copy under the new canonical name. The upload itself follows the
    # established duplicate rule and is left untouched.
    previous = _identical_catalog_file(library_dir, source, report["sha256"])
    if previous is not None:
        try:
            _apply_renames([(previous, dest)])
        except (IngestError, OSError) as exc:
            return {**report, "status": "conflict", "error": str(exc)}
        return {**report, "status": "already_present",
                "renamed_from": str(previous),
                "note": "the identical catalog copy was renamed; source was left untouched"}
    try:
        _file_without_overwrite(source, dest)
    except FileExistsError:
        return {**report, "status": "conflict",
                "error": f"refusing to overwrite catalog file {dest.name!r}"}
    return {**report, "status": "filed"}


def normalize(library_dir: Path, *, dry_run: bool = False) -> dict:
    library_dir = Path(library_dir).resolve()
    books = library.scan([library_dir], [])
    plan, missing = _rename_plan(books)
    try:
        _preflight(plan)
        if not dry_run:
            _apply_renames(plan)
    except IngestError as exc:
        return {"status": "conflict", "error": str(exc), "changed": 0,
                "missing_aliases": missing, "renames": []}
    return {
        "status": "dry_run" if dry_run else "normalized",
        "changed": len(plan),
        "missing_aliases": missing,
        "renames": [{"from": str(source), "to": str(dest)} for source, dest in plan],
    }


def _readable_catalog_books(library_dir: Path) -> Tuple[List[library.Book], list]:
    """Return scanner records for readable EPUBs plus untouched failures."""
    library_dir = Path(library_dir).resolve()
    scanned = {str(Path(book.path).resolve()): book
               for book in library.scan([library_dir], [])}
    readable_books, invalid = [], []
    if not library_dir.is_dir():
        return readable_books, invalid
    for path in sorted(library_dir.rglob("*.epub")):
        if path.is_symlink():
            invalid.append({"path": str(path),
                            "error": "symbolic links are not modified by catalog tidy"})
            continue
        if not path.is_file():
            continue
        resolved = path.resolve()
        ok, problem = _epub_readable(resolved)
        if not ok:
            invalid.append({"path": str(resolved), "error": problem})
            continue
        book = scanned.get(str(resolved))
        if book is not None:
            readable_books.append(book)
    return readable_books, invalid


def audit(library_dir: Path) -> dict:
    """Read-only catalog audit: canonical names, aliases and unsafe cases."""
    library_dir = Path(library_dir).resolve()
    books, invalid = _readable_catalog_books(library_dir)
    metadata_warnings = []
    for book in books:
        raw = library.read_opf_metadata(Path(book.path))
        missing_fields = [field for field in ("title", "language")
                          if not raw.get(field)]
        if missing_fields:
            metadata_warnings.append({
                "path": book.path,
                "warning": "missing required OPF " + ", ".join(missing_fields),
            })
    plan, missing = _rename_plan(books)
    rename_error = ""
    try:
        _preflight(plan)
    except IngestError as exc:
        rename_error = str(exc)

    alias_error = ""
    try:
        alias_doc = _read_alias_document(library_dir)
        used_aliases = list(alias_doc["aliases"].values())
    except IngestError as exc:
        alias_error = str(exc)
        used_aliases = list(library.load_aliases(library_dir).values())

    missing_groups = []
    for series in missing:
        group = [book for book in books if book.series.casefold() == series.casefold()]
        suggestion = library.suggest_alias(series, used_aliases)
        used_aliases.append(suggestion)
        missing_groups.append({
            "series": series,
            "suggested_alias": suggestion,
            "count": len(group),
            "books": [{"path": book.path, "title": book.base_title,
                       "series_index": book.series_index} for book in group],
        })

    by_digest: Dict[str, list] = {}
    for book in books:
        path = Path(book.path)
        try:
            by_digest.setdefault(_digest(path), []).append(str(path))
        except OSError:
            pass
    duplicates = [{"sha256": digest, "paths": paths}
                  for digest, paths in by_digest.items() if len(paths) > 1]

    rename_sources = {str(source.resolve()) for source, _ in plan}
    pending_paths = {book["path"] for group in missing_groups for book in group["books"]}
    correct = sum(1 for book in books
                  if str(Path(book.path).resolve()) not in rename_sources
                  and str(Path(book.path).resolve()) not in pending_paths)
    if alias_error or rename_error:
        status = "conflict"
    elif missing_groups:
        status = "needs_alias"
    elif plan:
        status = "needs_changes"
    elif invalid or duplicates or metadata_warnings:
        status = "issues"
    else:
        status = "clean"
    return {
        "status": status,
        "library": str(library_dir),
        "count": len(books) + len(invalid),
        "readable": len(books),
        "correct": correct,
        "invalid": invalid,
        "metadata_warnings": metadata_warnings,
        "duplicates": duplicates,
        "missing_series": missing_groups,
        "missing_aliases": [group["series"] for group in missing_groups],
        "renames": [{"from": str(source), "to": str(dest)} for source, dest in plan],
        "alias_error": alias_error,
        "rename_error": rename_error,
    }


def reconcile(library_dir: Path, aliases: Optional[Dict[str, str]] = None,
              *, dry_run: bool = False, expected_renames: Optional[list] = None) -> dict:
    """Apply a preflighted alias batch and canonicalize readable books only."""
    library_dir = Path(library_dir).resolve()
    aliases = aliases or {}
    try:
        before = _read_alias_document(library_dir)
        after = before
        for series, alias in aliases.items():
            after = library.with_alias(after, series, alias)
        books, invalid = _readable_catalog_books(library_dir)
        overrides = {series: library.alias_for(series, after["aliases"])
                     for series in aliases}
        plan, missing = _rename_plan(books, overrides)
        _preflight(plan)
        serialized_plan = [{"from": str(source), "to": str(dest)}
                           for source, dest in plan]
        if expected_renames is not None and serialized_plan != expected_renames:
            raise IngestError(
                "the catalog changed after the preview; run the tidy audit again")
        if not dry_run:
            _apply_renames(plan)
            try:
                if after != before:
                    _write_alias_document(library_dir, after)
            except Exception:
                _apply_renames((dest, source) for source, dest in reversed(plan))
                raise
    except (IngestError, OSError, ValueError) as exc:
        return {"status": "conflict", "error": str(exc), "changed": 0,
                "aliases_changed": 0, "missing_aliases": [], "renames": []}
    return {
        "status": "dry_run" if dry_run else "reconciled",
        "changed": len(plan),
        "aliases_changed": sum(
            1 for name, value in after["aliases"].items()
            if library.alias_for(name, before["aliases"]) != value),
        "missing_aliases": missing,
        "invalid": invalid,
        "renames": serialized_plan,
    }


def tidy_interactive(library_dir: Path, *, input_fn=None) -> dict:
    """Human terminal UI over ``audit`` and ``reconcile``.

    Keep this prompt sequence aligned with Bot's queued alias conversation in
    ``tools/tgbot/scripts/bot.py``.  Both are deliberately thin interfaces;
    every decision and mutation remains in the functions above.
    """
    input_fn = input_fn or input
    first = audit(library_dir)
    print(f"Checked {first['count']} EPUB(s): {first['correct']} already canonical, "
          f"{len(first['renames'])} rename(s), "
          f"{len(first['missing_series'])} series alias(es) needed.")
    for item in first["invalid"]:
        print(f"  unreadable, left untouched: {item['path']} ({item['error']})")
    for item in first["metadata_warnings"]:
        print(f"  metadata warning, left untouched: {item['path']} "
              f"({item['warning']})")
    for group in first["duplicates"]:
        print("  duplicate bytes, left untouched: " + ", ".join(group["paths"]))
    if first.get("alias_error") or first.get("rename_error"):
        return {"status": "conflict",
                "error": first.get("alias_error") or first.get("rename_error")}

    answers: Dict[str, str] = {}
    working = _read_alias_document(Path(library_dir).resolve())
    for group in first["missing_series"]:
        while True:
            volumes = ", ".join(
                book["series_index"] or "?" for book in group["books"][:8])
            prompt = (f"Short name for {group['series']} ({group['count']} book(s)"
                      + (f", volumes {volumes}" if volumes else "")
                      + f") [{group['suggested_alias']}] — '-' skips: ")
            answer = input_fn(prompt).strip()
            if answer == "-":
                break
            answer = answer or group["suggested_alias"]
            try:
                working = library.with_alias(working, group["series"], answer)
            except ValueError as exc:
                print(f"  {exc}")
                continue
            answers[group["series"]] = answer
            break

    preview = reconcile(library_dir, answers, dry_run=True)
    if preview.get("status") == "conflict":
        return preview
    print(f"Plan: store {preview['aliases_changed']} alias(es) and rename "
          f"{preview['changed']} file(s).")
    for rename in preview["renames"]:
        print(f"  {Path(rename['from']).name} -> {Path(rename['to']).name}")
    if preview["missing_aliases"]:
        print("Still skipped: " + ", ".join(preview["missing_aliases"]))
    if not preview["aliases_changed"] and not preview["changed"]:
        return {**preview, "status": "no_change"}
    if input_fn("Apply this catalog-only plan? [y/N] ").strip().casefold() not in {
            "y", "yes"}:
        return {**preview, "status": "cancelled"}
    return reconcile(library_dir, answers, expected_renames=preview["renames"])


def _choose_book(library_dir: Path, input_fn) -> Optional[Path]:
    books, _ = _readable_catalog_books(library_dir)
    if not books:
        print("No readable EPUBs in the catalog.")
        return None
    for number, book in enumerate(books, 1):
        print(f"{number:>3}. {book.title} — {book.author or 'unknown'}")
    while True:
        answer = input_fn("Book number (empty cancels): ").strip()
        if not answer:
            return None
        try:
            return Path(books[int(answer) - 1].path)
        except (ValueError, IndexError):
            print("  Choose one of the listed numbers.")


def edit_interactive(source: Optional[Path], library_dir: Path, *,
                     dry_run: bool = False, input_fn=None) -> dict:
    """Terminal metadata wizard over the same typed editor Telegram calls.

    If this conversational surface changes, review Bot.show_book_metadata and
    Bot.on_book_metadata_callback too; neither layer may acquire metadata rules
    of its own.
    """
    input_fn = input_fn or input
    library_dir = Path(library_dir).resolve()
    source = Path(source).resolve() if source else _choose_book(library_dir, input_fn)
    if source is None:
        return {"status": "cancelled"}
    report = epub_metadata.inspect_metadata(source, library_dir)
    if report.get("status") == "invalid":
        return report
    current = report["metadata"]
    labels = {
        "title": "Title", "author": "Author", "series": "Series",
        "series_index": "Series position", "language": "Language",
    }
    updates = {}
    print(f"Editing {source.name} (EPUB {report['package_version']})")
    print("Enter keeps the current value; '-' clears an optional value.")
    for field in epub_metadata.EDITABLE_FIELDS:
        shown = current[field] or "(empty)"
        while True:
            answer = input_fn(f"{labels[field]} [{shown}]: ")
            if answer == "":
                break
            if answer.strip() == "-":
                if field not in epub_metadata.OPTIONAL_FIELDS:
                    print("  That field is required and cannot be cleared.")
                    continue
                updates[field] = ""
            else:
                updates[field] = answer
            break

    alias = ""
    preview = epub_metadata.edit_metadata(
        source, library_dir, updates, dry_run=True)
    if preview.get("status") == "needs_alias":
        while True:
            alias = input_fn(
                f"Short name for {preview['series']} "
                f"[{preview['suggested_alias']}]: ").strip() or preview["suggested_alias"]
            preview = epub_metadata.edit_metadata(
                source, library_dir, updates, alias=alias, dry_run=True)
            if preview.get("status") != "conflict":
                break
            print(f"  {preview.get('error', 'that alias cannot be used')}")
    if preview.get("status") in {"invalid", "conflict", "error"}:
        return preview
    for field in preview.get("changed_fields", []):
        before = preview["metadata_before"][field] or "(empty)"
        after = preview["metadata_after"][field] or "(empty)"
        print(f"  {labels[field]}: {before} -> {after}")
    if preview.get("renames"):
        print(f"  File: {source.name} -> {Path(preview['destination']).name}")
    if dry_run:
        return preview
    if not preview.get("changed_fields") and not preview.get("renames") and not alias:
        return {**preview, "status": "no_change"}
    if input_fn("Write this metadata and canonical filename? [y/N] ").strip().casefold() \
            not in {"y", "yes"}:
        return {**preview, "status": "cancelled"}
    return epub_metadata.edit_metadata(
        source, library_dir, updates, alias=alias,
        expected_sha256=preview.get("sha256", ""))


def _print_human(report: dict) -> None:
    print(report.get("status", "unknown"))
    if report.get("error"):
        print("  " + report["error"])
    if "count" in report:
        print(f"  {report.get('correct', 0)} of {report['count']} already canonical")
    if report.get("metadata"):
        for field in epub_metadata.EDITABLE_FIELDS:
            print(f"  {field}: {report['metadata'].get(field) or '(empty)'}")
    if report.get("metadata_after"):
        for field in epub_metadata.EDITABLE_FIELDS:
            print(f"  {field}: {report['metadata_after'].get(field) or '(empty)'}")
    if report.get("destination"):
        print(f"  file: {report.get('source')} -> {report['destination']}")
    if report.get("series"):
        print(f"  series: {report['series']} [{report.get('series_alias') or '?'}]")
    for rename in report.get("renames", []):
        print(f"  {rename['from']} -> {rename['to']}")
    if report.get("missing_aliases"):
        print("  aliases still needed: " + ", ".join(report["missing_aliases"]))
    for item in report.get("invalid", []):
        print(f"  unreadable, untouched: {item['path']} ({item['error']})")
    for item in report.get("metadata_warnings", []):
        print(f"  metadata warning: {item['path']} ({item['warning']})")
    for group in report.get("duplicates", []):
        print("  duplicate bytes, untouched: " + ", ".join(group["paths"]))
    for group in report.get("missing_series", []):
        print(f"  alias needed: {group['series']} "
              f"[{group['suggested_alias']}] ({group['count']} book(s))")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit a typed JSON report")
    sub = ap.add_subparsers(dest="command", required=True)

    inspect_ap = sub.add_parser("inspect", help="read metadata without moving anything")
    inspect_ap.add_argument("source", type=Path)
    inspect_ap.add_argument("--library", type=Path, required=True)

    ingest_ap = sub.add_parser("ingest", help="file one EPUB under its catalog name")
    ingest_ap.add_argument("source", type=Path)
    ingest_ap.add_argument("--library", type=Path, required=True)
    ingest_ap.add_argument("--alias", default="", help="confirmed alias for a new series")

    alias_ap = sub.add_parser("set-alias", help="store an alias and rename that series")
    alias_ap.add_argument("series")
    alias_ap.add_argument("alias")
    alias_ap.add_argument("--library", type=Path, required=True)
    alias_ap.add_argument("--dry-run", action="store_true")

    normal_ap = sub.add_parser("normalize", help="canonicalize existing catalog filenames")
    normal_ap.add_argument("--library", type=Path, required=True)
    normal_ap.add_argument("--dry-run", action="store_true")

    audit_ap = sub.add_parser("audit", help="verify the catalog without changing it")
    audit_ap.add_argument("--library", type=Path, required=True)

    tidy_ap = sub.add_parser(
        "tidy", help="ask once per missing series alias, then apply one catalog plan")
    tidy_ap.add_argument("--library", type=Path, required=True)

    metadata_ap = sub.add_parser("metadata", help="show the editable metadata for one book")
    metadata_ap.add_argument("source", type=Path)
    metadata_ap.add_argument("--library", type=Path, required=True)

    edit_ap = sub.add_parser(
        "edit", help="edit title, author, series, position and language")
    edit_ap.add_argument("source", type=Path, nargs="?")
    edit_ap.add_argument("--library", type=Path, required=True)
    edit_ap.add_argument(
        "--set-json", default="",
        help="non-interactive field object used by steward interfaces")
    edit_ap.add_argument("--alias", default="", help="confirmed alias for a new series")
    edit_ap.add_argument(
        "--expect-sha256", default="",
        help="refuse if the EPUB changed after a prior preview")
    edit_ap.add_argument("--dry-run", action="store_true")

    args = ap.parse_args(argv)
    try:
        if args.command == "inspect":
            report = inspect(args.source, args.library)
        elif args.command == "ingest":
            report = ingest(args.source, args.library, args.alias)
        elif args.command == "set-alias":
            report = set_alias(args.library, args.series, args.alias, dry_run=args.dry_run)
        elif args.command == "normalize":
            report = normalize(args.library, dry_run=args.dry_run)
        elif args.command == "audit":
            report = audit(args.library)
        elif args.command == "tidy":
            if args.json:
                raise IngestError("tidy is an interactive terminal command; use audit for JSON")
            report = tidy_interactive(args.library)
        elif args.command == "metadata":
            report = epub_metadata.inspect_metadata(args.source, args.library)
        elif args.set_json:
            if args.source is None:
                raise IngestError("non-interactive metadata editing requires a source path")
            try:
                updates = json.loads(args.set_json)
            except json.JSONDecodeError as exc:
                raise IngestError(f"--set-json is not valid JSON: {exc}") from exc
            report = epub_metadata.edit_metadata(
                args.source, args.library, updates, alias=args.alias,
                dry_run=args.dry_run, expected_sha256=args.expect_sha256)
        elif args.json:
            raise IngestError("interactive edit cannot be combined with --json")
        else:
            report = edit_interactive(
                args.source, args.library, dry_run=args.dry_run)
    except (IngestError, OSError) as exc:
        report = {"status": "error", "error": str(exc)}

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_human(report)
    return 1 if report.get("status") in {"invalid", "conflict", "error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
