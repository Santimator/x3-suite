#!/usr/bin/env python3
"""File an EPUB under catalog metadata instead of its download filename.

This is the catalog's operation, not Telegram's. The bot calls it as a
subprocess; a terminal and any future importer can call the same interface.
EPUB bytes are never rewritten. Series aliases and canonical filenames are
catalog-side presentation only.

Examples:
  ingest_book.py --json inspect download.epub --library workspace/library
  ingest_book.py --json ingest download.epub --library workspace/library
  ingest_book.py --json ingest download.epub --library workspace/library --alias LOTR
  ingest_book.py --json normalize --library workspace/library --dry-run
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
    path = library_dir / library.ALIAS_FILE
    if not path.exists():
        return {"version": 1, "aliases": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("aliases", {}), dict):
        raise IngestError(f"{path.name} is not a valid alias document")
    return {"version": 1, "aliases": dict(data.get("aliases", {}))}


def _write_alias_document(library_dir: Path, data: dict) -> None:
    library_dir.mkdir(parents=True, exist_ok=True)
    path = library_dir / library.ALIAS_FILE
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _updated_alias_document(library_dir: Path, series: str, alias: str) -> dict:
    series = library._clean(series)
    if not series:
        raise IngestError("the full series name cannot be empty")
    try:
        alias = library.validate_alias(alias)
    except ValueError as exc:
        raise IngestError(str(exc)) from exc
    data = _read_alias_document(library_dir)
    aliases = data["aliases"]
    existing_key = next((name for name in aliases if str(name).casefold() == series.casefold()), None)
    collision = next((name for name, used in aliases.items()
                      if str(used).casefold() == alias.casefold()
                      and str(name).casefold() != series.casefold()), None)
    if collision:
        raise IngestError(f"{alias!r} already means {collision!r}")
    if existing_key and existing_key != series:
        aliases.pop(existing_key)
    aliases[series] = alias
    return data


def _rename_plan(books: Iterable[library.Book], alias_override: Optional[Tuple[str, str]] = None
                 ) -> Tuple[List[Tuple[Path, Path]], List[str]]:
    plan: List[Tuple[Path, Path]] = []
    missing = set()
    override_name, override_alias = alias_override or ("", "")
    for book in books:
        alias = book.series_alias
        if book.series and book.series.casefold() == override_name.casefold():
            alias = override_alias
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


def _print_human(report: dict) -> None:
    print(report.get("status", "unknown"))
    if report.get("error"):
        print("  " + report["error"])
    if report.get("series"):
        print(f"  series: {report['series']} [{report.get('series_alias') or '?'}]")
    for rename in report.get("renames", []):
        print(f"  {rename['from']} -> {rename['to']}")
    if report.get("missing_aliases"):
        print("  aliases still needed: " + ", ".join(report["missing_aliases"]))


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

    args = ap.parse_args(argv)
    try:
        if args.command == "inspect":
            report = inspect(args.source, args.library)
        elif args.command == "ingest":
            report = ingest(args.source, args.library, args.alias)
        elif args.command == "set-alias":
            report = set_alias(args.library, args.series, args.alias, dry_run=args.dry_run)
        else:
            report = normalize(args.library, dry_run=args.dry_run)
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
