#!/usr/bin/env python3
"""Read and explicitly edit the small metadata surface the catalog uses.

The EPUB remains a publication, not a bag of files to rebuild.  An edit
replaces only the package document inside the archive, preserves every other
entry, validates the result, and then gives the catalog file its canonical
name.  Device slimming is intentionally nowhere in this module.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Tuple

import library

EDITABLE_FIELDS = ("title", "author", "series", "series_index", "language")
OPTIONAL_FIELDS = {"author", "series", "series_index"}
LANGUAGE_RE = re.compile(
    r"^(?:[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*|"
    r"[ixIX](?:-[A-Za-z0-9]{1,8})+)$")
POSITION_RE = re.compile(r"^\d+(?:\.\d+)*$")


class MetadataError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                value.update(chunk)
    except OSError as exc:
        raise MetadataError(f"cannot read source file: {exc}") from exc
    return value.hexdigest()


def _inside(root: Path, path: Path) -> bool:
    root, path = root.resolve(), path.resolve()
    return path == root or root in path.parents


def _local_name(element: ET.Element) -> str:
    return str(element.tag).rsplit("}", 1)[-1]


def _opf_tag(metadata: ET.Element, name: str) -> str:
    tag = str(metadata.tag)
    namespace = tag[1:].split("}", 1)[0] if tag.startswith("{") else ""
    return f"{{{namespace}}}{name}" if namespace else name


def _children(parent: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(parent) if _local_name(child) == name]


def _load_package(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as archive:
            container_bytes = archive.read("META-INF/container.xml")
            container = ET.fromstring(container_bytes)
            rootfile = container.find(
                f"{library.CONTAINER_NS}rootfiles/{library.CONTAINER_NS}rootfile")
            opf_path = rootfile.get("full-path", "") if rootfile is not None else ""
            if not opf_path:
                raise MetadataError("META-INF/container.xml has no OPF rootfile")
            opf_bytes = archive.read(opf_path)
            parser = ET.XMLParser(target=ET.TreeBuilder(
                insert_comments=True, insert_pis=True))
            package = ET.fromstring(opf_bytes, parser=parser)
            metadata = next(
                (child for child in list(package) if _local_name(child) == "metadata"), None)
            if metadata is None:
                raise MetadataError("the OPF package has no metadata element")
            names = set(archive.namelist())
    except FileNotFoundError as exc:
        raise MetadataError("source file does not exist") from exc
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise MetadataError(f"not a readable EPUB: {exc}") from exc
    return {
        "opf_path": opf_path,
        "opf_bytes": opf_bytes,
        "package": package,
        "metadata_element": metadata,
        "version": library._clean(package.get("version", "")),
        "signed": "META-INF/signatures.xml" in names,
    }


def _metadata_values(path: Path) -> Dict[str, str]:
    values = library.read_opf_metadata(path)
    return {field: library._clean(values.get(field, "")) for field in EDITABLE_FIELDS}


def inspect_metadata(source: Path, library_dir: Path) -> dict:
    source, library_dir = Path(source).resolve(), Path(library_dir).resolve()
    if not _inside(library_dir, source):
        return {"status": "invalid", "source": str(source),
                "error": "the book is outside the catalog directory"}
    try:
        package = _load_package(source)
    except MetadataError as exc:
        return {"status": "invalid", "source": str(source), "error": str(exc)}

    values = _metadata_values(source)
    embedded_title = values["title"] or library.title_from_filename(source)
    base_title = library.clean_embedded_title(
        embedded_title, values["author"], values["series"], values["series_index"])
    aliases = library.aliases_for(source, library_dir)
    alias = library.alias_for(values["series"], aliases) if values["series"] else ""
    title = library.catalog_title(base_title, alias, values["series_index"])
    warnings = []
    if not values["title"]:
        warnings.append("dc:title is missing")
    if not values["language"]:
        warnings.append("dc:language is missing")
    if package["signed"]:
        warnings.append("the EPUB is signed; editing would invalidate its signature")
    return {
        "status": "needs_alias" if values["series"] and not alias else "ready",
        "source": str(source),
        "package_version": package["version"] or "unknown",
        "editable": not package["signed"],
        "metadata": values,
        "base_title": base_title,
        "title": title,
        "series_alias": alias,
        "suggested_alias": (library.suggest_alias(values["series"], aliases.values())
                            if values["series"] and not alias else ""),
        "filename": library.canonical_filename(title, values["author"]),
        "warnings": warnings,
    }


def _validate_updates(current: Dict[str, str], updates: dict) -> Dict[str, str]:
    if not isinstance(updates, dict):
        raise MetadataError("metadata updates must be a JSON object")
    unknown = sorted(set(updates) - set(EDITABLE_FIELDS))
    if unknown:
        raise MetadataError("unsupported metadata field(s): " + ", ".join(unknown))
    proposed = dict(current)
    for field, value in updates.items():
        if not isinstance(value, str):
            raise MetadataError(f"{field} must be text")
        proposed[field] = library._clean(value)

    if not proposed["title"]:
        raise MetadataError("title is required; fill it before writing metadata")
    if not proposed["language"]:
        raise MetadataError("language is required; fill it before writing metadata")
    if proposed["language"] and not LANGUAGE_RE.fullmatch(proposed["language"]):
        raise MetadataError(
            "language must be a well-formed tag such as en, es, zh-Hans, or en-US")
    if (not proposed["series"] and "series_index" in updates
            and library._clean(updates["series_index"])):
        raise MetadataError("set a series before setting its position")
    if not proposed["series"]:
        proposed["series_index"] = ""
    elif proposed["series_index"] and not POSITION_RE.fullmatch(proposed["series_index"]):
        raise MetadataError(
            "series position must be digits separated by dots, such as 1, 1.5, or 2.2.1")
    return proposed


def _remove_with_refiners(metadata: ET.Element,
                          elements: Iterable[ET.Element]) -> None:
    doomed = list(elements)
    ids = {element.get("id", "") for element in doomed if element.get("id")}
    for child in list(metadata):
        if child in doomed or child.get("refines", "").lstrip("#") in ids:
            metadata.remove(child)


def _set_dc(metadata: ET.Element, name: str, value: str) -> None:
    matches = [child for child in list(metadata)
               if str(child.tag) == f"{library.DC_NS}{name}"]
    if value:
        if matches:
            old_value = library._clean(matches[0].text or "")
            target = matches[0].get("id", "")
            matches[0].text = value
            if target and old_value != value:
                stale = {"file-as", "alternate-script"}
                for child in list(metadata):
                    if (child.get("refines", "").lstrip("#") == target
                            and library._clean(child.get("property", "")) in stale):
                        metadata.remove(child)
        else:
            element = ET.Element(f"{library.DC_NS}{name}")
            element.text = value
            # Core Dublin Core fields are easiest for old readers to discover
            # before the generic meta fields.
            index = next((n for n, child in enumerate(list(metadata))
                          if _local_name(child) == "meta"), len(metadata))
            metadata.insert(index, element)
    elif matches:
        _remove_with_refiners(metadata, matches)


def _series_elements(metadata: ET.Element) -> list[ET.Element]:
    metas = _children(metadata, "meta")
    collections = [element for element in metas
                   if library._clean(element.get("property", ""))
                   == "belongs-to-collection" and library._clean(element.text or "")]
    by_target: Dict[str, Dict[str, str]] = {}
    for element in metas:
        prop = library._clean(element.get("property", ""))
        target = library._clean(element.get("refines", "")).lstrip("#")
        if target and prop in ("collection-type", "group-position"):
            by_target.setdefault(target, {})[prop] = library._clean(element.text or "")
    explicit = [element for element in collections
                if by_target.get(element.get("id", ""), {}).get(
                    "collection-type", "").casefold() == "series"]
    if explicit:
        return explicit
    return collections if len(collections) == 1 else []


def _replace_series(metadata: ET.Element, series: str, position: str,
                    epub3: bool) -> None:
    _remove_with_refiners(metadata, _series_elements(metadata))
    for element in list(metadata):
        if _local_name(element) != "meta":
            continue
        name = library._clean(element.get("name", "")).casefold()
        if name in ("calibre:series", "calibre:series_index"):
            metadata.remove(element)
    if not series:
        return

    meta_tag = _opf_tag(metadata, "meta")
    if epub3:
        used_ids = {element.get("id") for element in metadata.iter() if element.get("id")}
        series_id = "x3-series"
        suffix = 2
        while series_id in used_ids:
            series_id = f"x3-series-{suffix}"
            suffix += 1
        collection = ET.SubElement(
            metadata, meta_tag, {"property": "belongs-to-collection", "id": series_id})
        collection.text = series
        collection_type = ET.SubElement(
            metadata, meta_tag,
            {"refines": f"#{series_id}", "property": "collection-type"})
        collection_type.text = "series"
        if position:
            group_position = ET.SubElement(
                metadata, meta_tag,
                {"refines": f"#{series_id}", "property": "group-position"})
            group_position.text = position

    # Calibre's EPUB 2 form is retained as a compatibility mirror. EPUB 3
    # explicitly permits legacy OPF2 meta elements in package metadata.
    ET.SubElement(metadata, meta_tag,
                  {"name": "calibre:series", "content": series})
    if position:
        ET.SubElement(metadata, meta_tag,
                      {"name": "calibre:series_index", "content": position})


def _touch_modified(metadata: ET.Element) -> None:
    matches = [element for element in _children(metadata, "meta")
               if library._clean(element.get("property", "")) == "dcterms:modified"]
    value = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if matches:
        matches[0].text = value
    else:
        element = ET.SubElement(metadata, _opf_tag(metadata, "meta"),
                                {"property": "dcterms:modified"})
        element.text = value


def _serialize_package(package: ET.Element, original: bytes) -> bytes:
    try:
        for _, (prefix, uri) in ET.iterparse(io.BytesIO(original), events=("start-ns",)):
            if prefix != "xml":
                try:
                    ET.register_namespace(prefix or "", uri)
                except ValueError:
                    pass
    except ET.ParseError:
        pass
    declaration = original.lstrip().startswith(b"<?xml")
    return ET.tostring(package, encoding="utf-8", xml_declaration=declaration)


def _rewrite_epub(source: Path, package: dict, opf_bytes: bytes, target: Path) -> None:
    try:
        with zipfile.ZipFile(source) as incoming, zipfile.ZipFile(target, "x") as outgoing:
            outgoing.comment = incoming.comment
            for info in incoming.infolist():
                payload = opf_bytes if info.filename == package["opf_path"] else incoming.read(info)
                outgoing.writestr(info, payload)
        os.chmod(target, stat.S_IMODE(source.stat().st_mode))
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise MetadataError(f"could not rewrite the EPUB safely: {exc}") from exc


def _validate_rewrite(path: Path, expected: Dict[str, str]) -> None:
    _load_package(path)
    actual = _metadata_values(path)
    for field in EDITABLE_FIELDS:
        if actual[field] != expected[field]:
            raise MetadataError(
                f"rewritten EPUB did not retain {field}: {actual[field]!r} != {expected[field]!r}")


def edit_metadata(source: Path, library_dir: Path, updates: dict, *,
                  alias: str = "", dry_run: bool = False,
                  expected_sha256: str = "") -> dict:
    """Preview or atomically apply an explicit metadata edit and canonical rename."""
    source, library_dir = Path(source).resolve(), Path(library_dir).resolve()
    if not _inside(library_dir, source):
        return {"status": "invalid", "source": str(source),
                "error": "the book is outside the catalog directory"}
    try:
        source_sha256 = _digest(source)
        if expected_sha256 and source_sha256 != expected_sha256:
            raise MetadataError(
                "the EPUB changed after the preview; inspect it again before writing")
        package = _load_package(source)
        if package["signed"]:
            raise MetadataError(
                "the EPUB is signed; changing its package metadata would invalidate the signature")
        current = _metadata_values(source)
        proposed = _validate_updates(current, updates)
    except MetadataError as exc:
        return {"status": "invalid", "source": str(source), "error": str(exc)}

    changed_fields = [field for field in EDITABLE_FIELDS
                      if proposed[field] != current[field]]
    if ({"series", "series_index"} & set(changed_fields)
            and len(_series_elements(package["metadata_element"])) > 1):
        return {
            "status": "invalid", "source": str(source),
            "error": ("the EPUB declares more than one series; this focused editor "
                      "will not guess which collection to replace"),
        }
    try:
        alias_doc = library.read_alias_document(library_dir)
        aliases = dict(alias_doc["aliases"])
        series_alias = library.alias_for(proposed["series"], aliases) if proposed["series"] else ""
        alias_changed = False
        if proposed["series"] and alias:
            alias = library.validate_alias(alias)
            if series_alias and series_alias.casefold() != alias.casefold():
                raise MetadataError(
                    f"{proposed['series']!r} already uses short name {series_alias!r}; "
                    "change the whole series alias explicitly")
            if not series_alias:
                alias_doc = library.with_alias(alias_doc, proposed["series"], alias)
                series_alias = alias
                alias_changed = True
        if proposed["series"] and not series_alias:
            suggestion = library.suggest_alias(proposed["series"], aliases.values())
            return {
                "status": "needs_alias", "source": str(source),
                "metadata_before": current, "metadata_after": proposed,
                "series": proposed["series"], "suggested_alias": suggestion,
                "changed_fields": changed_fields,
            }
    except (ValueError, MetadataError) as exc:
        return {"status": "conflict", "source": str(source), "error": str(exc)}

    embedded_title = proposed["title"] or library.title_from_filename(source)
    base_title = library.clean_embedded_title(
        embedded_title, proposed["author"], proposed["series"], proposed["series_index"])
    display_title = library.catalog_title(base_title, series_alias, proposed["series_index"])
    destination = source.with_name(library.canonical_filename(display_title, proposed["author"]))
    if destination != source and destination.exists():
        return {
            "status": "conflict", "source": str(source),
            "error": f"refusing to overwrite existing {destination.name!r}",
            "destination": str(destination),
        }

    report = {
        "status": "dry_run" if dry_run else "updated",
        "source": str(source), "destination": str(destination),
        "metadata_before": current, "metadata_after": proposed,
        "changed_fields": changed_fields,
        "series_alias": series_alias,
        "alias_changed": alias_changed,
        "title": display_title, "base_title": base_title,
        "filename": destination.name,
        "sha256": source_sha256,
        "bytes_rewritten": bool(changed_fields),
        "renames": ([{"from": str(source), "to": str(destination)}]
                    if destination != source else []),
    }
    if dry_run:
        return report
    if not changed_fields and destination == source and not alias_changed:
        return {**report, "status": "no_change", "bytes_rewritten": False}

    tmp = source.with_name(f".{source.name}.metadata-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    backup = source.with_name(f".{source.name}.metadata-backup-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    moved_original = False
    placed_destination = False
    try:
        if changed_fields:
            metadata = package["metadata_element"]
            _set_dc(metadata, "title", proposed["title"])
            _set_dc(metadata, "creator", proposed["author"])
            _set_dc(metadata, "language", proposed["language"])
            if {"series", "series_index"} & set(changed_fields):
                _replace_series(metadata, proposed["series"], proposed["series_index"],
                                package["version"].startswith("3"))
            if package["version"].startswith("3"):
                _touch_modified(metadata)
            opf_bytes = _serialize_package(package["package"], package["opf_bytes"])
            _rewrite_epub(source, package, opf_bytes, tmp)
            _validate_rewrite(tmp, proposed)

        os.replace(source, backup)
        moved_original = True
        os.replace(tmp if changed_fields else backup, destination)
        placed_destination = True
        if not changed_fields:
            moved_original = False
        if alias_changed:
            library.write_alias_document(library_dir, alias_doc)
        if moved_original:
            try:
                backup.unlink()
            except OSError:
                # The committed book is already complete. A hidden recovery
                # copy is safer than rolling back a valid edit merely because
                # cleanup failed.
                report["warning"] = f"old recovery copy remains at {backup}"
            moved_original = False
        library._META_CACHE.clear()
    except Exception as exc:
        rollback_errors = []
        if placed_destination and destination.exists():
            try:
                if changed_fields:
                    os.replace(destination, tmp)
                elif destination != source:
                    os.replace(destination, source)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if moved_original and backup.exists() and not source.exists():
            try:
                os.replace(backup, source)
                moved_original = False
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        # Delete only a known modified temporary after the original is back.
        # If rollback itself failed, both hidden paths are deliberately kept
        # for manual recovery; losing the sole original is never cleanup.
        if not rollback_errors and source.exists():
            tmp.unlink(missing_ok=True)
        detail = str(exc)
        if rollback_errors:
            detail += ("; rollback needs manual recovery (kept hidden files): "
                       + "; ".join(rollback_errors))
        return {"status": "error", "source": str(source), "error": detail}
    tmp.unlink(missing_ok=True)
    return report


def rename_series(library_dir: Path, current_name: str, new_name: str, *,
                  merge: bool = False, dry_run: bool = False,
                  expected_sha256s: dict | None = None) -> dict:
    """Rename one embedded series across all of its volumes as one operation.

    Every rewritten EPUB is prepared and validated before an original moves.
    The originals then sit at private backup paths until every new book and the
    alias sidecar are in place.  This is deliberately a catalog operation: a
    terminal, Telegram, or another steward gets the same preview/apply seam.
    """
    library_dir = Path(library_dir).resolve()
    current_name = library._clean(current_name)
    requested_name = library._clean(new_name)
    if not current_name or not requested_name:
        return {"status": "invalid",
                "error": "both the current and new series names are required"}

    books = library.scan([library_dir], [])
    source_books = [book for book in books
                    if book.series.casefold() == current_name.casefold()]
    if not source_books:
        return {"status": "invalid", "series": current_name,
                "error": f"no catalog books belong to {current_name!r}"}
    canonical_current = source_books[0].series

    try:
        alias_before = library.read_alias_document(library_dir)
    except ValueError as exc:
        return {"status": "conflict", "series": canonical_current,
                "error": str(exc)}
    aliases = alias_before["aliases"]
    source_alias = library.alias_for(canonical_current, aliases)
    if not source_alias:
        return {"status": "conflict", "series": canonical_current,
                "error": "set this series' short name before renaming it"}

    same_identity = requested_name.casefold() == canonical_current.casefold()
    target_books = [book for book in books
                    if book.series.casefold() == requested_name.casefold()
                    and book.series.casefold() != canonical_current.casefold()]
    target_key = next((name for name in aliases
                       if name.casefold() == requested_name.casefold()
                       and name.casefold() != canonical_current.casefold()), "")
    target_exists = bool(target_books or target_key)
    canonical_target = (target_books[0].series if target_books else target_key) \
        if target_exists else requested_name
    target_alias = (library.alias_for(canonical_target, aliases)
                    if target_exists else source_alias)
    if target_exists and not merge:
        return {
            "status": "needs_merge", "series": canonical_current,
            "target_series": canonical_target, "series_alias": target_alias,
            "count": len(source_books), "target_count": len(target_books),
            "error": f"{canonical_target!r} already exists",
        }
    if target_exists and not target_alias:
        return {"status": "conflict", "series": canonical_current,
                "target_series": canonical_target,
                "error": "the destination series has no short name"}

    alias_after = {"version": 1, "aliases": dict(aliases)}
    old_key = next((name for name in alias_after["aliases"]
                    if name.casefold() == canonical_current.casefold()), "")
    if old_key:
        alias_after["aliases"].pop(old_key)
    if not target_exists:
        try:
            alias_after = library.with_alias(
                alias_after, canonical_target, source_alias)
        except ValueError as exc:
            return {"status": "conflict", "series": canonical_current,
                    "error": str(exc)}

    items = []
    for book in source_books:
        source = Path(book.path).resolve()
        if not _inside(library_dir, source):
            return {"status": "invalid", "series": canonical_current,
                    "error": f"{source.name!r} is outside the catalog"}
        try:
            package = _load_package(source)
            if package["signed"]:
                raise MetadataError(
                    f"{source.name!r} is signed; editing would invalidate its signature")
            current = _metadata_values(source)
            if current["series"].casefold() != canonical_current.casefold():
                raise MetadataError(
                    f"{source.name!r} changed series while the catalog was scanned")
            if len(_series_elements(package["metadata_element"])) > 1:
                raise MetadataError(
                    f"{source.name!r} declares more than one series; refusing to guess")
            digest = _digest(source)
        except MetadataError as exc:
            return {"status": "invalid", "series": canonical_current,
                    "error": str(exc)}
        proposed = dict(current)
        proposed["series"] = canonical_target
        display_title = library.catalog_title(
            book.base_title, target_alias, proposed["series_index"])
        destination = source.with_name(
            library.canonical_filename(display_title, proposed["author"]))
        items.append({
            "source": source, "destination": destination, "package": package,
            "before": current, "after": proposed, "sha256": digest,
            "base_title": book.base_title, "title": display_title,
        })

    sources = {item["source"] for item in items}
    destinations = {}
    for item in items:
        destination = item["destination"]
        previous = destinations.get(destination)
        if previous and previous != item["source"]:
            return {"status": "conflict", "series": canonical_current,
                    "error": f"two volumes would both become {destination.name!r}"}
        destinations[destination] = item["source"]
        if destination.exists() and destination not in sources:
            return {"status": "conflict", "series": canonical_current,
                    "error": f"refusing to overwrite existing {destination.name!r}"}

    actual_hashes = {str(item["source"]): item["sha256"] for item in items}
    if expected_sha256s is not None:
        expected = {str(Path(path).resolve()): str(digest)
                    for path, digest in expected_sha256s.items()}
        if expected != actual_hashes:
            return {"status": "conflict", "series": canonical_current,
                    "error": "the series changed after the preview; open it again"}

    report = {
        "status": "dry_run" if dry_run else "renamed",
        "series_before": canonical_current, "series": canonical_target,
        "series_alias": target_alias, "merge": bool(target_exists),
        "count": len(items), "target_count": len(target_books),
        "expected_sha256s": actual_hashes,
        "alias_changed": alias_after != alias_before,
        "renames": [
            {"from": str(item["source"]), "to": str(item["destination"])}
            for item in items if item["source"] != item["destination"]
        ],
        "books": [
            {"from": str(item["source"]), "to": str(item["destination"]),
             "title": item["base_title"],
             "series_index": item["after"]["series_index"],
             "sha256": item["sha256"]}
            for item in items
        ],
    }
    if same_identity and canonical_target == canonical_current:
        return {**report, "status": "no_change"}
    if dry_run:
        return report

    for item in items:
        nonce = uuid.uuid4().hex[:8]
        item["tmp"] = item["source"].with_name(
            f".{item['source'].name}.series-{os.getpid()}-{nonce}")
        item["backup"] = item["source"].with_name(
            f".{item['source'].name}.series-backup-{os.getpid()}-{nonce}")

    prepared = []
    try:
        for item in items:
            metadata = item["package"]["metadata_element"]
            _replace_series(metadata, canonical_target,
                            item["after"]["series_index"],
                            item["package"]["version"].startswith("3"))
            if item["package"]["version"].startswith("3"):
                _touch_modified(metadata)
            opf = _serialize_package(
                item["package"]["package"], item["package"]["opf_bytes"])
            _rewrite_epub(item["source"], item["package"], opf, item["tmp"])
            _validate_rewrite(item["tmp"], item["after"])
            prepared.append(item)
    except Exception as exc:
        for item in items:
            item["tmp"].unlink(missing_ok=True)
        return {"status": "error", "series": canonical_current,
                "error": f"could not prepare the complete series: {exc}"}

    backed_up, placed = [], []
    try:
        for item in items:
            os.replace(item["source"], item["backup"])
            backed_up.append(item)
        for item in items:
            os.replace(item["tmp"], item["destination"])
            placed.append(item)
        if alias_after != alias_before:
            library.write_alias_document(library_dir, alias_after)
    except Exception as exc:
        rollback_errors = []
        try:
            if alias_after != alias_before:
                library.write_alias_document(library_dir, alias_before)
        except Exception as rollback_exc:
            rollback_errors.append(f"alias sidecar: {rollback_exc}")
        for item in reversed(placed):
            try:
                if item["destination"].exists():
                    os.replace(item["destination"], item["tmp"])
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        for item in reversed(backed_up):
            try:
                if item["backup"].exists() and not item["source"].exists():
                    os.replace(item["backup"], item["source"])
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if not rollback_errors:
            for item in items:
                item["tmp"].unlink(missing_ok=True)
        detail = str(exc)
        if rollback_errors:
            detail += "; recovery copies kept: " + "; ".join(rollback_errors)
        return {"status": "error", "series": canonical_current, "error": detail}

    warnings = []
    for item in items:
        try:
            item["backup"].unlink()
        except OSError as exc:
            warnings.append(f"recovery copy remains at {item['backup']}: {exc}")
        item["tmp"].unlink(missing_ok=True)
    library._META_CACHE.clear()
    if warnings:
        report["warnings"] = warnings
    return report
