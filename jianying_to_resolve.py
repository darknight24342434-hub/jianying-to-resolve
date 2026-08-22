#!/usr/bin/env python3
"""Build an offline Jianying/CapCut draft-to-Resolve conversion package.

This tool never opens Resolve and never modifies a Jianying draft. It reads
Timelines/project.json, timeline_layout.json, and Timelines/<id>/template.json
where available, exports audit CSVs/reports, and emits a Resolve Python script
that can be run later in a live Resolve scripting environment.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse


US_PER_SECOND = 1_000_000.0
DEFAULT_FPS = 30.0

MEDIA_EXTENSIONS = {
    ".3gp",
    ".aac",
    ".aif",
    ".aiff",
    ".avi",
    ".flac",
    ".gif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mxf",
    ".png",
    ".tif",
    ".tiff",
    ".wav",
    ".webm",
    ".wmv",
}

TIMELINE_ID_KEYS = (
    "id",
    "timeline_id",
    "timelineId",
    "draft_id",
    "draftId",
    "uuid",
)
NAME_KEYS = (
    "name",
    "timeline_name",
    "timelineName",
    "draft_name",
    "draftName",
    "title",
)
PATH_KEYS = (
    "path",
    "file_path",
    "filepath",
    "local_path",
    "localPath",
    "local_material_path",
    "localMaterialPath",
    "draft_file_path",
    "draftFilePath",
    "source_path",
    "sourcePath",
    "origin_path",
    "originPath",
    "material_path",
    "materialPath",
    "audio_path",
    "audioPath",
    "video_path",
    "videoPath",
)
TEXT_KEYS = (
    "text",
    "plain_text",
    "plainText",
    "content",
    "text_content",
    "textContent",
    "source_text",
    "sourceText",
    "value",
)
FEATURE_TOKENS = (
    "adjust",
    "animation",
    "anim",
    "blend",
    "curve",
    "effect",
    "filter",
    "key_frame",
    "keyframe",
    "mask",
    "motion",
    "speed",
    "sticker",
    "template",
    "transition",
    "transform",
)


@dataclass
class TimelineCandidate:
    draft_root: str
    timeline_id: str
    template_path: str
    timeline_name: str = ""
    project_path: str = ""
    layout_path: str = ""
    active_hint: bool = False


@dataclass
class SelectedTimeline:
    candidate: Optional[TimelineCandidate]
    reason: str
    error: str = ""


def read_json(path: Path) -> Any:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "gb18030"):
        try:
            with path.open("r", encoding=encoding) as handle:
                return json.load(handle)
        except UnicodeDecodeError as exc:
            last_error = exc
        except json.JSONDecodeError as exc:
            last_error = exc
            break
    raise ValueError(f"Could not parse JSON at {path}: {last_error}")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_csv(path: Path, rows: Sequence[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def sanitize_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    clean = clean.strip("._-")
    return clean or "jianying_job"


def normalize_path_string(value: str) -> str:
    value = value.strip().strip('"')
    if value.startswith("file:"):
        parsed = urlparse(value)
        if parsed.scheme == "file":
            return unquote(parsed.path.lstrip("/")) if os.name == "nt" else unquote(parsed.path)
    return value


def short_json(value: Any, limit: int = 500) -> str:
    if value in ("", None, [], {}):
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def to_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def us_to_sec(value: Any) -> str:
    number = to_number(value)
    if number is None:
        return ""
    return f"{number / US_PER_SECOND:.6f}"


def sec_to_srt_time(value: float) -> str:
    if value < 0:
        value = 0.0
    total_ms = int(round(value * 1000.0))
    ms = total_ms % 1000
    total_sec = total_ms // 1000
    sec = total_sec % 60
    total_min = total_sec // 60
    minute = total_min % 60
    hour = total_min // 60
    return f"{hour:02d}:{minute:02d}:{sec:02d},{ms:03d}"


def is_mapping(value: Any) -> bool:
    return isinstance(value, dict)


def ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return [value]


def get_first_string(mapping: Dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def collect_strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, list):
        result: List[str] = []
        for item in value:
            result.extend(collect_strings(item))
        return result
    if isinstance(value, dict):
        result = []
        for key in ("id", "material_id", "materialId", "ref_id", "refId"):
            if key in value:
                result.extend(collect_strings(value[key]))
        return result
    return []


def iter_files_named(root: Path, names: Iterable[str]) -> Iterable[Path]:
    wanted = {name.lower() for name in names}
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {".git", "__pycache__"}]
        for filename in filenames:
            if filename.lower() in wanted:
                yield Path(dirpath) / filename


def timeline_root_from_template(path: Path) -> Optional[Path]:
    try:
        if path.name.lower() != "template.json":
            return None
        if path.parent.parent.name.lower() != "timelines":
            return None
        return path.parent.parent.parent
    except IndexError:
        return None


def collect_timeline_names(project_data: Any) -> Dict[str, str]:
    names: Dict[str, str] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            timeline_id = get_first_string(value, TIMELINE_ID_KEYS)
            timeline_name = get_first_string(value, NAME_KEYS)
            if timeline_id and timeline_name:
                names.setdefault(timeline_id, timeline_name)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(project_data)
    return names


def collect_active_timeline_hints(layout_data: Any) -> List[str]:
    hints: List[str] = []

    def visit(value: Any, path: Tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_l = str(key).lower()
                child_path = path + (str(key),)
                if isinstance(child, (str, int, float)) and any(
                    token in key_l for token in ("active", "current", "select", "timeline")
                ):
                    hints.extend(collect_strings(child))
                visit(child, child_path)
        elif isinstance(value, list):
            for child in value:
                visit(child, path)

    visit(layout_data)
    return list(dict.fromkeys(hints))


def discover_timelines(draft_root: Path) -> Tuple[List[TimelineCandidate], List[str], List[str]]:
    messages: List[str] = []
    invalid_project_files: List[str] = []
    template_paths: List[Path] = []

    direct_timelines = draft_root / "Timelines"
    if direct_timelines.exists():
        template_paths.extend(sorted(direct_timelines.glob("*/template.json")))

    if not template_paths:
        for path in iter_files_named(draft_root, ("template.json",)):
            if timeline_root_from_template(path):
                template_paths.append(path)

    template_paths = sorted(set(template_paths))
    candidates: List[TimelineCandidate] = []
    by_draft_root: Dict[Path, List[Path]] = {}
    for path in template_paths:
        root = timeline_root_from_template(path)
        if root:
            by_draft_root.setdefault(root, []).append(path)

    if not by_draft_root:
        project_files = list(iter_files_named(draft_root, ("project.json",)))
        invalid_project_files = [str(path) for path in project_files if path.parent.name.lower() == "timelines"]
        if invalid_project_files:
            messages.append("Found Timelines/project.json files but no Timelines/<id>/template.json files.")
        else:
            messages.append("No Jianying Timelines/<id>/template.json files found.")
        return candidates, messages, invalid_project_files

    for root, paths in by_draft_root.items():
        project_path = root / "Timelines" / "project.json"
        layout_path = root / "timeline_layout.json"
        timeline_names: Dict[str, str] = {}
        active_hints: List[str] = []

        if project_path.exists():
            try:
                timeline_names = collect_timeline_names(read_json(project_path))
            except Exception as exc:  # noqa: BLE001 - audit report should continue.
                messages.append(f"Could not parse {project_path}: {exc}")
        else:
            messages.append(f"Missing project.json at {project_path}")

        if layout_path.exists():
            try:
                active_hints = collect_active_timeline_hints(read_json(layout_path))
            except Exception as exc:  # noqa: BLE001
                messages.append(f"Could not parse {layout_path}: {exc}")

        for template_path in paths:
            timeline_id = template_path.parent.name
            candidates.append(
                TimelineCandidate(
                    draft_root=str(root),
                    timeline_id=timeline_id,
                    template_path=str(template_path),
                    timeline_name=timeline_names.get(timeline_id, ""),
                    project_path=str(project_path) if project_path.exists() else "",
                    layout_path=str(layout_path) if layout_path.exists() else "",
                    active_hint=timeline_id in active_hints,
                )
            )

    return sorted(candidates, key=lambda item: (item.draft_root, item.timeline_id)), messages, invalid_project_files


def select_timeline(candidates: Sequence[TimelineCandidate], timeline_id: str = "") -> SelectedTimeline:
    if not candidates:
        return SelectedTimeline(candidate=None, reason="none", error="No valid Jianying timeline template found.")
    if timeline_id:
        matches = [candidate for candidate in candidates if candidate.timeline_id == timeline_id]
        if matches:
            return SelectedTimeline(candidate=matches[0], reason="requested timeline id")
        return SelectedTimeline(
            candidate=None,
            reason="requested timeline id not found",
            error=f"Timeline id {timeline_id!r} was not found among discovered templates.",
        )
    active = [candidate for candidate in candidates if candidate.active_hint]
    if len(active) == 1:
        return SelectedTimeline(candidate=active[0], reason="timeline_layout active hint")
    if len(candidates) == 1:
        return SelectedTimeline(candidate=candidates[0], reason="only discovered timeline")
    return SelectedTimeline(candidate=candidates[0], reason="first discovered timeline; pass --timeline-id to override")


def collect_material_maps(template: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    materials = template.get("materials")
    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    if not isinstance(materials, dict):
        return result
    for kind, value in materials.items():
        kind_map: Dict[str, Dict[str, Any]] = {}
        for item in ensure_list(value):
            if not isinstance(item, dict):
                continue
            material_id = get_first_string(item, ("id", "material_id", "materialId", "uid", "uuid"))
            if material_id:
                kind_map[material_id] = item
        result[str(kind)] = kind_map
    return result


def classify_kind(kind: str) -> str:
    lower = kind.lower()
    if "audio" in lower and "effect" not in lower and "fade" not in lower:
        return "audio"
    if any(token in lower for token in ("video", "photo", "image")) and "effect" not in lower:
        return "video"
    if "text" in lower or "subtitle" in lower or "caption" in lower:
        return "text"
    if "speed" in lower:
        return "speed"
    if "transition" in lower:
        return "transition"
    if any(token in lower for token in ("effect", "filter", "mask", "sticker", "animation")):
        return "effect"
    return lower


def material_lookup(material_maps: Dict[str, Dict[str, Dict[str, Any]]], material_id: str) -> List[Tuple[str, Dict[str, Any]]]:
    found: List[Tuple[str, Dict[str, Any]]] = []
    for kind, items in material_maps.items():
        if material_id in items:
            found.append((kind, items[material_id]))
    return found


def looks_like_path(value: str) -> bool:
    text = value.strip()
    if not text or len(text) > 1000:
        return False
    if text.startswith(("http://", "https://")):
        return False
    suffix = Path(text.replace("\\", "/")).suffix.lower()
    return (
        suffix in MEDIA_EXTENSIONS
        or ":/" in text
        or ":\\" in text
        or "\\" in text
        or "/" in text
        or text.startswith("file:")
    )


def collect_path_values(value: Any, parent_key: str = "") -> List[str]:
    paths: List[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            key_l = key_text.lower()
            if isinstance(child, str) and (
                key_text in PATH_KEYS or "path" in key_l or key_l in {"file", "uri", "url"}
            ):
                if looks_like_path(child):
                    paths.append(child)
            paths.extend(collect_path_values(child, key_text))
    elif isinstance(value, list):
        for child in value:
            paths.extend(collect_path_values(child, parent_key))
    elif isinstance(value, str) and parent_key in PATH_KEYS and looks_like_path(value):
        paths.append(value)
    return list(dict.fromkeys(paths))


def extract_material_refs(segment: Dict[str, Any]) -> List[str]:
    refs: List[str] = []
    for key, value in segment.items():
        key_l = str(key).lower()
        if (
            key_l in {"material_id", "materialid", "ref_id", "refid"}
            or ("material" in key_l and ("id" in key_l or "ref" in key_l))
            or key_l in {"extra_material_refs", "extra_materialrefs", "video_id", "audio_id", "text_id"}
        ):
            refs.extend(collect_strings(value))
    return list(dict.fromkeys(refs))


def get_timerange(segment: Dict[str, Any], key: str) -> Tuple[Any, Any]:
    candidates = [key]
    if key == "target_timerange":
        candidates.extend(("targetTimerange", "target_time_range", "targetTimeRange"))
    if key == "source_timerange":
        candidates.extend(("sourceTimerange", "source_time_range", "sourceTimeRange"))
    for candidate in candidates:
        timerange = segment.get(candidate)
        if isinstance(timerange, dict):
            start = (
                timerange.get("start")
                or timerange.get("start_time")
                or timerange.get("startTime")
                or timerange.get("start_us")
                or timerange.get("startUs")
                or 0
            )
            duration = (
                timerange.get("duration")
                or timerange.get("dur")
                or timerange.get("length")
                or timerange.get("duration_us")
                or timerange.get("durationUs")
                or ""
            )
            return start, duration
    return "", ""


def find_feature_paths(value: Any, tokens: Sequence[str] = FEATURE_TOKENS, max_items: int = 50) -> List[str]:
    found: List[str] = []

    def visit(child: Any, path: Tuple[str, ...], depth: int) -> None:
        if len(found) >= max_items or depth > 8:
            return
        if isinstance(child, dict):
            for key, nested in child.items():
                key_l = str(key).lower()
                next_path = path + (str(key),)
                if any(token in key_l for token in tokens) and nested not in (None, "", [], {}):
                    found.append(".".join(next_path))
                    if len(found) >= max_items:
                        return
                visit(nested, next_path, depth + 1)
        elif isinstance(child, list):
            for index, nested in enumerate(child[:20]):
                visit(nested, path + (str(index),), depth + 1)

    visit(value, (), 0)
    return list(dict.fromkeys(found))


def extract_jsonish(value: str) -> Any:
    text = value.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def is_probable_human_text(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if looks_like_path(text):
        return False
    if len(text) > 5000:
        return False
    if re.fullmatch(r"[A-Za-z0-9_-]{16,}", text):
        return False
    return True


def extract_text_content(material: Dict[str, Any]) -> str:
    candidates: List[str] = []

    def visit(value: Any, key: str = "", depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key), depth + 1)
        elif isinstance(value, list):
            for child in value[:50]:
                visit(child, key, depth + 1)
        elif isinstance(value, str):
            parsed = extract_jsonish(value)
            if parsed is not None:
                visit(parsed, key, depth + 1)
            if key in TEXT_KEYS or key.lower() in {item.lower() for item in TEXT_KEYS}:
                if is_probable_human_text(value):
                    candidates.append(value.strip())

    visit(material)
    if not candidates:
        return ""
    candidates = sorted(set(candidates), key=lambda item: (-len(item), item))
    return candidates[0]


def material_name(material: Dict[str, Any], path_values: Sequence[str]) -> str:
    name = get_first_string(material, ("name", "material_name", "materialName", "display_name", "displayName"))
    if name:
        return name
    for value in path_values:
        if value:
            return Path(normalize_path_string(value).replace("\\", "/")).name
    return ""


class MediaResolver:
    def __init__(self, draft_root: Path, material_root: Path, needed_names: Iterable[str]) -> None:
        self.draft_root = draft_root
        self.material_root = material_root
        self.needed_names = {name for name in needed_names if name}
        self.index: Dict[str, List[Path]] = {}
        if self.needed_names:
            self._build_index()

    def _build_index(self) -> None:
        roots = []
        for root in (self.draft_root, self.material_root):
            if root.exists() and root not in roots:
                roots.append(root)
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if name not in {".git", "__pycache__"}]
                for filename in filenames:
                    if filename in self.needed_names:
                        self.index.setdefault(filename, []).append(Path(dirpath) / filename)
        for filename, paths in list(self.index.items()):
            self.index[filename] = sorted(set(paths))

    def resolve(self, declared_paths: Sequence[str]) -> Tuple[str, str, str, str]:
        if not declared_paths:
            return "", "", "no_declared_path", ""
        missing_declared = ""
        for declared_raw in declared_paths:
            declared = normalize_path_string(str(declared_raw))
            if not declared:
                continue
            missing_declared = missing_declared or declared
            path = Path(declared)
            if path.is_absolute() and path.exists():
                return declared, str(path), "absolute", ""
            draft_candidate = self.draft_root / declared
            if draft_candidate.exists():
                return declared, str(draft_candidate), "draft_relative", ""
            material_candidate = self.material_root / declared
            if material_candidate.exists():
                return declared, str(material_candidate), "material_relative", ""
            filename = Path(declared.replace("\\", "/")).name
            exact_matches = self.index.get(filename, [])
            if exact_matches:
                status = "exact_filename_search"
                note = ""
                if len(exact_matches) > 1:
                    status = "exact_filename_search_multiple"
                    note = f"{len(exact_matches)} exact filename matches; first sorted path selected."
                return declared, str(exact_matches[0]), status, note
        return missing_declared, "", "missing", ""


def build_relink_rows(
    material_maps: Dict[str, Dict[str, Dict[str, Any]]],
    draft_root: Path,
    material_root: Path,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    media_materials: List[Tuple[str, str, Dict[str, Any], List[str]]] = []
    needed_names: List[str] = []

    for kind, items in material_maps.items():
        broad_kind = classify_kind(kind)
        if broad_kind not in {"video", "audio"}:
            continue
        for material_id, material in items.items():
            paths = collect_path_values(material)
            media_materials.append((kind, material_id, material, paths))
            for declared in paths:
                filename = Path(normalize_path_string(declared).replace("\\", "/")).name
                if filename:
                    needed_names.append(filename)

    resolver = MediaResolver(draft_root=draft_root, material_root=material_root, needed_names=needed_names)
    rows: List[Dict[str, Any]] = []
    resolved_by_material_id: Dict[str, Dict[str, Any]] = {}

    for kind, material_id, material, paths in media_materials:
        declared_path, resolved_path, status, note = resolver.resolve(paths)
        row = {
            "material_id": material_id,
            "material_kind": kind,
            "media_type": classify_kind(kind),
            "material_name": material_name(material, paths),
            "declared_path": declared_path,
            "resolved_path": resolved_path,
            "status": status,
            "note": note,
            "all_declared_paths": " | ".join(paths),
        }
        rows.append(row)
        resolved_by_material_id[material_id] = row

    return rows, resolved_by_material_id


def infer_track_type(track: Dict[str, Any], segment_material_types: Sequence[str]) -> str:
    for key in ("type", "track_type", "trackType", "media_type", "mediaType"):
        value = track.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    for preferred in ("video", "audio", "text"):
        if preferred in segment_material_types:
            return preferred
    return "unknown"


def conversion_support_for(
    broad_type: str,
    relink: Optional[Dict[str, Any]],
    text: str,
    metadata_features: Sequence[str],
) -> Tuple[str, str]:
    if broad_type in {"video", "audio"}:
        if not relink or not relink.get("resolved_path"):
            return "unsupported", "media missing"
        if metadata_features:
            return "supported_basic_media", "timing/media supported; effects or retime preserved as metadata"
        return "supported", "timing/media supported"
    if broad_type == "text" or text:
        return "metadata_only", "text exported to CSV/SRT and Resolve markers"
    if metadata_features:
        return "metadata_only", "feature metadata exported; visual rebuild is manual"
    return "metadata_only", "unclassified segment exported for audit"


def extract_segments(
    template: Dict[str, Any],
    material_maps: Dict[str, Dict[str, Dict[str, Any]]],
    relink_by_material_id: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    tracks = ensure_list(template.get("tracks"))
    segment_rows: List[Dict[str, Any]] = []
    track_rows: List[Dict[str, Any]] = []
    text_rows: List[Dict[str, Any]] = []

    for track_index, track in enumerate(tracks, start=1):
        if not isinstance(track, dict):
            continue
        segments = ensure_list(track.get("segments") or track.get("clips") or track.get("items"))
        track_segment_types: List[str] = []
        track_id = get_first_string(track, ("id", "track_id", "trackId", "uuid")) or str(track_index)

        pending_rows: List[Dict[str, Any]] = []
        max_end_us = 0.0
        for clip_index, segment in enumerate(segments, start=1):
            if not isinstance(segment, dict):
                continue
            refs = extract_material_refs(segment)
            material_hits: List[Tuple[str, str, Dict[str, Any]]] = []
            for ref in refs:
                for kind, material in material_lookup(material_maps, ref):
                    material_hits.append((ref, kind, material))

            media_hit: Optional[Tuple[str, str, Dict[str, Any]]] = None
            text_hit: Optional[Tuple[str, str, Dict[str, Any]]] = None
            feature_hits: List[Tuple[str, str, Dict[str, Any]]] = []
            for hit in material_hits:
                broad = classify_kind(hit[1])
                if broad in {"video", "audio"} and media_hit is None:
                    media_hit = hit
                elif broad == "text" and text_hit is None:
                    text_hit = hit
                elif broad in {"speed", "transition", "effect"}:
                    feature_hits.append(hit)

            selected_hit = media_hit or text_hit or (material_hits[0] if material_hits else None)
            material_id = selected_hit[0] if selected_hit else ""
            material_kind = selected_hit[1] if selected_hit else ""
            broad_type = classify_kind(material_kind) if material_kind else ""
            if broad_type:
                track_segment_types.append(broad_type)

            relink = relink_by_material_id.get(material_id)
            selected_material = selected_hit[2] if selected_hit else {}
            path_values = collect_path_values(selected_material) if selected_material else []
            text_material = text_hit[2] if text_hit else (selected_material if broad_type == "text" else {})
            text_content = extract_text_content(text_material) if text_material else ""

            target_start_us, target_duration_us = get_timerange(segment, "target_timerange")
            source_start_us, source_duration_us = get_timerange(segment, "source_timerange")
            target_start_num = to_number(target_start_us) or 0.0
            target_duration_num = to_number(target_duration_us) or 0.0
            max_end_us = max(max_end_us, target_start_num + target_duration_num)

            feature_paths = find_feature_paths(segment)
            material_feature_paths: List[str] = []
            for _, _, feature_material in feature_hits:
                material_feature_paths.extend(find_feature_paths(feature_material))

            speed_values = []
            for key in ("speed", "speed_rate", "speedRate", "play_speed", "playSpeed"):
                if key in segment:
                    speed_values.append(f"{key}={segment[key]}")
            for ref, kind, feature_material in feature_hits:
                if classify_kind(kind) == "speed":
                    speed_values.append(f"{ref}:{short_json(feature_material, 180)}")

            transition_refs = [
                f"{ref}:{kind}" for ref, kind, _ in feature_hits if classify_kind(kind) == "transition"
            ]
            effect_refs = [f"{ref}:{kind}" for ref, kind, _ in feature_hits if classify_kind(kind) == "effect"]
            metadata_features = list(dict.fromkeys(feature_paths + material_feature_paths + speed_values + transition_refs + effect_refs))
            support, support_note = conversion_support_for(broad_type, relink, text_content, metadata_features)

            row = {
                "clip_index": clip_index,
                "track_index": track_index,
                "track_id": track_id,
                "track_type": "",
                "segment_id": get_first_string(segment, ("id", "segment_id", "segmentId", "uuid")),
                "material_id": material_id,
                "material_kind": material_kind,
                "media_type": broad_type,
                "material_name": material_name(selected_material, path_values) if selected_material else "",
                "declared_path": relink.get("declared_path", "") if relink else "",
                "resolved_path": relink.get("resolved_path", "") if relink else "",
                "missing": "yes" if broad_type in {"video", "audio"} and not (relink and relink.get("resolved_path")) else "no",
                "target_start_us": target_start_us,
                "target_duration_us": target_duration_us,
                "target_start_sec": us_to_sec(target_start_us),
                "target_duration_sec": us_to_sec(target_duration_us),
                "source_start_us": source_start_us,
                "source_duration_us": source_duration_us,
                "source_start_sec": us_to_sec(source_start_us),
                "source_duration_sec": us_to_sec(source_duration_us),
                "speed": " | ".join(speed_values),
                "effects": " | ".join(effect_refs),
                "transitions": " | ".join(transition_refs),
                "keyframe_or_feature_fields": " | ".join(metadata_features),
                "material_refs": " | ".join(refs),
                "conversion_support": support,
                "notes": support_note,
            }
            pending_rows.append(row)

            if text_content:
                start_sec = (to_number(target_start_us) or 0.0) / US_PER_SECOND
                duration_sec = (to_number(target_duration_us) or 0.0) / US_PER_SECOND
                text_rows.append(
                    {
                        "track_index": track_index,
                        "track_id": track_id,
                        "segment_id": row["segment_id"],
                        "material_id": text_hit[0] if text_hit else material_id,
                        "start_sec": f"{start_sec:.6f}",
                        "duration_sec": f"{duration_sec:.6f}",
                        "end_sec": f"{start_sec + duration_sec:.6f}",
                        "text": text_content,
                        "text_material_kind": text_hit[1] if text_hit else material_kind,
                        "style_or_template_refs": " | ".join(refs),
                    }
                )

        track_type = infer_track_type(track, track_segment_types)
        for row in pending_rows:
            row["track_type"] = track_type
        segment_rows.extend(pending_rows)
        track_rows.append(
            {
                "track_index": track_index,
                "track_id": track_id,
                "track_type": track_type,
                "segment_count": len(pending_rows),
                "muted": track.get("muted", track.get("mute", "")),
                "hidden": track.get("hidden", track.get("visible", "")),
                "locked": track.get("locked", ""),
                "material_types": " | ".join(sorted(set(track_segment_types))),
                "duration_sec_estimate": f"{max_end_us / US_PER_SECOND:.6f}" if max_end_us else "",
                "raw_feature_fields": " | ".join(find_feature_paths(track)),
            }
        )

    return segment_rows, track_rows, text_rows


def write_srt(path: Path, text_rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(text_rows, key=lambda row: float(row.get("start_sec") or 0.0))
    with path.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(ordered, start=1):
            start = float(row.get("start_sec") or 0.0)
            end = float(row.get("end_sec") or start)
            text = str(row.get("text") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            handle.write(f"{index}\n")
            handle.write(f"{sec_to_srt_time(start)} --> {sec_to_srt_time(end)}\n")
            handle.write(f"{text}\n\n")


def package_file_names(job: str) -> Dict[str, str]:
    return {
        "segments_csv": f"jianying_{job}_segments.csv",
        "track_inventory_csv": f"jianying_{job}_track_inventory.csv",
        "relink_map_csv": f"jianying_{job}_relink_map.csv",
        "missing_csv": f"jianying_{job}_missing.csv",
        "text_labels_csv": f"jianying_{job}_text_labels.csv",
        "text_labels_srt": f"jianying_{job}_text_labels.srt",
        "source_template_json": f"source_template_{job}.json",
        "conversion_summary_json": f"conversion_summary_{job}.json",
        "fidelity_report_md": f"fidelity_report_{job}.md",
        "validation_report_md": f"validation_report_{job}.md",
        "resolve_build_py": f"resolve_build_{job}.py",
        "readme_md": f"README_{job}.md",
    }


def write_resolve_build_script(path: Path, job: str, summary_name: str) -> None:
    script = f'''#!/usr/bin/env python3
"""Future live Resolve importer for Jianying conversion package {job}.

Generated by jianying_to_resolve_offline.py. Do not run this until DaVinci
Resolve is open and External scripting is enabled.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path


# Blackmagic's standard scripting-module location, per platform.
# Set RESOLVE_SCRIPT_API to override; "Modules" is appended for you.
_API_ROOT = os.environ.get("RESOLVE_SCRIPT_API")
if not _API_ROOT:
    if sys.platform == "win32":
        _API_ROOT = r"C:\\ProgramData\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting"
    elif sys.platform == "darwin":
        _API_ROOT = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
    else:
        _API_ROOT = "/opt/resolve/Developer/Scripting"
DEFAULT_MODULE_PATH = os.path.join(_API_ROOT, "Modules")


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def connect_resolve():
    if DEFAULT_MODULE_PATH not in sys.path:
        sys.path.insert(0, DEFAULT_MODULE_PATH)
    import DaVinciResolveScript as dvr  # type: ignore

    resolve = dvr.scriptapp("Resolve")
    if not resolve:
        raise RuntimeError("Resolve API not connected. Open Resolve and enable External scripting.")
    return resolve


def frame(value, fps):
    try:
        return int(round(float(value or 0) * fps))
    except ValueError:
        return 0


def main():
    package_dir = Path(__file__).resolve().parent
    summary = json.loads((package_dir / "{summary_name}").read_text(encoding="utf-8"))
    files = summary["files"]
    segments = read_csv(package_dir / files["segments_csv"])
    text_rows = read_csv(package_dir / files["text_labels_csv"])
    fps = float(summary.get("timeline", {{}}).get("fps") or 30.0)

    resolve = connect_resolve()
    project_manager = resolve.GetProjectManager()
    media_storage = resolve.GetMediaStorage()

    project_name = summary.get("resolve_project_name") or "{job}_Resolve"
    project = project_manager.CreateProject(project_name)
    if not project:
        project = project_manager.LoadProject(project_name)
    if not project:
        raise RuntimeError(f"Could not create or load Resolve project {{project_name!r}}")

    project.SetSetting("timelineFrameRate", str(int(fps) if fps.is_integer() else fps))
    project.SetSetting("timelineResolutionWidth", "1920")
    project.SetSetting("timelineResolutionHeight", "1080")

    media_pool = project.GetMediaPool()
    timeline_name = summary.get("resolve_timeline_name") or "{job}_timeline"
    timeline = media_pool.CreateEmptyTimeline(timeline_name)
    if not timeline:
        timeline = project.GetCurrentTimeline()
    if not timeline:
        raise RuntimeError("Could not create Resolve timeline")

    media_paths = sorted({{
        row["resolved_path"]
        for row in segments
        if row.get("resolved_path") and row.get("conversion_support") in {{"supported", "supported_basic_media"}}
    }})
    imported_items = media_storage.AddItemListToMediaPool(media_paths) if media_paths else []
    media_by_path = {{}}
    for item in imported_items or []:
        props = item.GetClipProperty() or {{}}
        full_path = props.get("File Path") or props.get("FilePath") or props.get("Path") or ""
        if full_path:
            media_by_path[str(full_path)] = item
    for path_value, item in zip(media_paths, imported_items or []):
        media_by_path.setdefault(path_value, item)

    append_results = []
    for media_type in ("video", "audio"):
        track_rows = [row for row in segments if row.get("media_type") == media_type and row.get("resolved_path")]
        track_ids = sorted({{row.get("track_index") or "1" for row in track_rows}}, key=lambda value: int(value or 0))
        for track_id in track_ids:
            clip_infos = []
            for row in [item for item in track_rows if (item.get("track_index") or "1") == track_id]:
                media_item = media_by_path.get(row["resolved_path"])
                if not media_item:
                    continue
                start_frame = frame(row.get("source_start_sec"), fps)
                duration_frame = frame(row.get("source_duration_sec") or row.get("target_duration_sec"), fps)
                clip_info = {{
                    "mediaPoolItem": media_item,
                    "startFrame": start_frame,
                    "endFrame": max(start_frame, start_frame + duration_frame - 1),
                    "recordFrame": frame(row.get("target_start_sec"), fps),
                    "trackIndex": int(track_id or 1),
                }}
                clip_infos.append(clip_info)
            result = media_pool.AppendToTimeline(clip_infos) if clip_infos else []
            append_results.append({{"media_type": media_type, "track_index": track_id, "requested": len(clip_infos), "result_count": len(result or [])}})

    marker_rows = []
    for row in segments:
        if row.get("conversion_support") not in {{"supported"}} or row.get("keyframe_or_feature_fields"):
            marker_rows.append(row)
    for row in marker_rows:
        timeline.AddMarker(
            frame(row.get("target_start_sec"), fps),
            "Yellow",
            f"Jianying metadata: {{row.get('segment_id') or row.get('material_id') or 'segment'}}",
            row.get("notes") or row.get("keyframe_or_feature_fields") or "metadata-only feature",
            max(1, frame(row.get("target_duration_sec"), fps)),
            row.get("segment_id") or "",
        )
    for row in text_rows:
        timeline.AddMarker(
            frame(row.get("start_sec"), fps),
            "Blue",
            "Jianying text",
            row.get("text") or "",
            max(1, frame(row.get("duration_sec"), fps)),
            row.get("segment_id") or "",
        )

    verify = {{
        "project_name": project_name,
        "timeline_name": timeline_name,
        "media_paths_requested": len(media_paths),
        "append_results": append_results,
        "video_track_count": timeline.GetTrackCount("video"),
        "audio_track_count": timeline.GetTrackCount("audio"),
        "start_frame": int(timeline.GetStartFrame()),
        "end_frame": int(timeline.GetEndFrame()),
    }}
    verify_path = package_dir / "resolve_build_verify.json"
    verify_path.write_text(json.dumps(verify, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")

    drp_path = str(package_dir / f"{{project_name}}.drp")
    project_manager.SaveProject()
    project_manager.ExportProject(project_name, drp_path, True)
    print(json.dumps({{"verify": verify, "drp_path": drp_path}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
'''
    path.write_text(script, encoding="utf-8")


def write_fidelity_report(
    path: Path,
    summary: Dict[str, Any],
    segment_rows: Sequence[Dict[str, Any]],
    text_rows: Sequence[Dict[str, Any]],
    missing_rows: Sequence[Dict[str, Any]],
) -> None:
    supported = [row for row in segment_rows if row.get("conversion_support") == "supported"]
    partial = [row for row in segment_rows if row.get("conversion_support") == "supported_basic_media"]
    metadata = [row for row in segment_rows if row.get("conversion_support") == "metadata_only"]
    unsupported = [row for row in segment_rows if row.get("conversion_support") == "unsupported"]

    lines = [
        f"# Fidelity Report - {summary['job_name']}",
        "",
        "## Supported",
        f"- Basic video/audio timing and relinked media rows: {len(supported) + len(partial)}",
        f"- Text labels exported as CSV/SRT and future Resolve markers: {len(text_rows)}",
        "- Source template copied into the package for audit.",
        "",
        "## Metadata Only",
        f"- Rows with effects, speed, transitions, keyframes, masks, templates, or other feature fields: {len(partial) + len(metadata)}",
        "- Styled text templates, filters, effects, stickers, transitions, and retime metadata are preserved in CSV/markers but not visually rebuilt.",
        "",
        "## Unsupported",
        f"- Missing media rows: {len(missing_rows)}",
        f"- Unsupported segment rows: {len(unsupported)}",
        "- Jianying-only render behavior, effect stacks, masks, tracking, advanced speed curves, and exact typography are not reproduced by this offline prototype.",
        "",
        "## Counts",
        f"- Total segments: {len(segment_rows)}",
        f"- Supported: {len(supported)}",
        f"- Supported basic media with metadata caveats: {len(partial)}",
        f"- Metadata only: {len(metadata)}",
        f"- Unsupported: {len(unsupported)}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_validation_report(
    path: Path,
    args: argparse.Namespace,
    candidates: Sequence[TimelineCandidate],
    selected: SelectedTimeline,
    messages: Sequence[str],
    invalid_project_files: Sequence[str],
    package_dir: Path,
) -> None:
    command = " ".join(sys.argv)
    lines = [
        f"# Validation Report - {package_dir.name}",
        "",
        f"- Command: `{command}`",
        f"- Draft root: `{args.draft_root}`",
        f"- Material root: `{args.material_root}`",
        f"- Output package: `{package_dir}`",
        f"- Discovered valid timeline templates: {len(candidates)}",
        f"- Discovered project.json files without valid templates: {len(invalid_project_files)}",
        f"- Selection reason: {selected.reason}",
    ]
    if selected.error:
        lines.append(f"- Result: {selected.error}")
    elif selected.candidate:
        lines.extend(
            [
                f"- Selected draft folder: `{selected.candidate.draft_root}`",
                f"- Selected timeline id: `{selected.candidate.timeline_id}`",
                f"- Selected timeline name: `{selected.candidate.timeline_name}`",
            ]
        )
    if messages:
        lines.append("")
        lines.append("## Notes")
        lines.extend(f"- {message}" for message in messages)
    if candidates:
        lines.append("")
        lines.append("## Candidate Timelines")
        for candidate in candidates:
            active = " active-hint" if candidate.active_hint else ""
            lines.append(f"- `{candidate.timeline_id}`{active}: `{candidate.template_path}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_package_readme(path: Path, summary: Dict[str, Any]) -> None:
    files = summary["files"]
    lines = [
        f"# Jianying to Resolve Package - {summary['job_name']}",
        "",
        "This package was generated offline. It did not open DaVinci Resolve and did not modify the source draft.",
        "",
        "## Files",
        f"- `{files['segments_csv']}`: segment inventory with timing, material refs, relink status, and feature hints.",
        f"- `{files['track_inventory_csv']}`: track-level segment counts and raw feature hints.",
        f"- `{files['relink_map_csv']}`: media material path resolution audit.",
        f"- `{files['missing_csv']}`: unresolved media rows.",
        f"- `{files['text_labels_csv']}` and `{files['text_labels_srt']}`: extracted text labels.",
        f"- `{files['source_template_json']}`: copied source template or no-draft placeholder.",
        f"- `{files['conversion_summary_json']}`: machine-readable summary.",
        f"- `{files['fidelity_report_md']}`: supported vs metadata-only vs unsupported features.",
        f"- `{files['validation_report_md']}`: validation command and discovery result.",
        f"- `{files['resolve_build_py']}`: future Resolve import script; run only after opening Resolve.",
        "",
        "## Future Resolve Import",
        "1. Open DaVinci Resolve Studio.",
        "2. Enable External scripting using local network in Resolve preferences.",
        "3. Run the generated `resolve_build_*.py` with a Python environment that can import the Resolve scripting module.",
        "4. Inspect generated markers for text, effects, speed changes, transitions, and unsupported Jianying-only features.",
        "",
        "The Resolve script is a prototype. It imports relinked media and appends basic clips where the Resolve API supports it, then adds markers for metadata-only features.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_empty_outputs(
    package_dir: Path,
    files: Dict[str, str],
    args: argparse.Namespace,
    candidates: Sequence[TimelineCandidate],
    selected: SelectedTimeline,
    messages: Sequence[str],
    invalid_project_files: Sequence[str],
    job: str,
) -> Dict[str, Any]:
    summary = {
        "status": "no_valid_draft",
        "job_name": job,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "draft_root": str(Path(args.draft_root)),
        "material_root": str(Path(args.material_root)),
        "files": files,
        "discovered_timeline_count": len(candidates),
        "invalid_project_files": list(invalid_project_files),
        "messages": list(messages) + ([selected.error] if selected.error else []),
    }
    write_csv(package_dir / files["segments_csv"], [], SEGMENT_FIELDS)
    write_csv(package_dir / files["track_inventory_csv"], [], TRACK_FIELDS)
    write_csv(package_dir / files["relink_map_csv"], [], RELINK_FIELDS)
    write_csv(package_dir / files["missing_csv"], [], MISSING_FIELDS)
    write_csv(package_dir / files["text_labels_csv"], [], TEXT_FIELDS)
    write_srt(package_dir / files["text_labels_srt"], [])
    write_json(package_dir / files["source_template_json"], {"status": "not_found", "message": selected.error})
    write_json(package_dir / files["conversion_summary_json"], summary)
    write_fidelity_report(package_dir / files["fidelity_report_md"], summary, [], [], [])
    write_validation_report(
        package_dir / files["validation_report_md"],
        args,
        candidates,
        selected,
        messages,
        invalid_project_files,
        package_dir,
    )
    write_resolve_build_script(package_dir / files["resolve_build_py"], job, files["conversion_summary_json"])
    write_package_readme(package_dir / files["readme_md"], summary)
    return summary


SEGMENT_FIELDS = [
    "clip_index",
    "track_index",
    "track_id",
    "track_type",
    "segment_id",
    "material_id",
    "material_kind",
    "media_type",
    "material_name",
    "declared_path",
    "resolved_path",
    "missing",
    "target_start_us",
    "target_duration_us",
    "target_start_sec",
    "target_duration_sec",
    "source_start_us",
    "source_duration_us",
    "source_start_sec",
    "source_duration_sec",
    "speed",
    "effects",
    "transitions",
    "keyframe_or_feature_fields",
    "material_refs",
    "conversion_support",
    "notes",
]
TRACK_FIELDS = [
    "track_index",
    "track_id",
    "track_type",
    "segment_count",
    "muted",
    "hidden",
    "locked",
    "material_types",
    "duration_sec_estimate",
    "raw_feature_fields",
]
RELINK_FIELDS = [
    "material_id",
    "material_kind",
    "media_type",
    "material_name",
    "declared_path",
    "resolved_path",
    "status",
    "note",
    "all_declared_paths",
]
MISSING_FIELDS = [
    "material_id",
    "material_kind",
    "media_type",
    "material_name",
    "declared_path",
    "status",
    "note",
]
TEXT_FIELDS = [
    "track_index",
    "track_id",
    "segment_id",
    "material_id",
    "start_sec",
    "duration_sec",
    "end_sec",
    "text",
    "text_material_kind",
    "style_or_template_refs",
]


def run_conversion(args: argparse.Namespace) -> Dict[str, Any]:
    draft_root = Path(args.draft_root).resolve()
    material_root = Path(args.material_root).resolve()
    out_root = Path(args.out).resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    requested_job = args.job_name or draft_root.name or "jianying_job"
    job = sanitize_name(requested_job)
    package_dir = out_root / f"jianying_{job}_to_resolve_{timestamp}"
    package_dir.mkdir(parents=True, exist_ok=False)
    files = package_file_names(job)

    candidates, messages, invalid_project_files = discover_timelines(draft_root)
    selected = select_timeline(candidates, args.timeline_id or "")
    if not selected.candidate:
        return create_empty_outputs(
            package_dir,
            files,
            args,
            candidates,
            selected,
            messages,
            invalid_project_files,
            job,
        )

    candidate = selected.candidate
    template_path = Path(candidate.template_path)
    template = read_json(template_path)
    if not isinstance(template, dict):
        raise ValueError(f"Expected JSON object in {template_path}")

    material_maps = collect_material_maps(template)
    relink_rows, relink_by_material_id = build_relink_rows(
        material_maps=material_maps,
        draft_root=Path(candidate.draft_root),
        material_root=material_root,
    )
    segment_rows, track_rows, text_rows = extract_segments(template, material_maps, relink_by_material_id)
    missing_rows = [
        {
            "material_id": row.get("material_id", ""),
            "material_kind": row.get("material_kind", ""),
            "media_type": row.get("media_type", ""),
            "material_name": row.get("material_name", ""),
            "declared_path": row.get("declared_path", ""),
            "status": row.get("status", ""),
            "note": row.get("note", ""),
        }
        for row in relink_rows
        if row.get("media_type") in {"video", "audio"} and not row.get("resolved_path")
    ]

    timeline_duration = template.get("duration", "")
    summary: Dict[str, Any] = {
        "status": "converted_offline",
        "job_name": job,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "draft_root": candidate.draft_root,
        "material_root": str(material_root),
        "selected_timeline": asdict(candidate),
        "selection_reason": selected.reason,
        "timeline": {
            "id": candidate.timeline_id,
            "name": candidate.timeline_name,
            "duration_us": timeline_duration,
            "duration_sec": us_to_sec(timeline_duration),
            "fps": DEFAULT_FPS,
        },
        "counts": {
            "discovered_timeline_count": len(candidates),
            "tracks": len(track_rows),
            "segments": len(segment_rows),
            "relink_rows": len(relink_rows),
            "missing_media": len(missing_rows),
            "text_labels": len(text_rows),
            "supported": sum(1 for row in segment_rows if row.get("conversion_support") == "supported"),
            "supported_basic_media": sum(
                1 for row in segment_rows if row.get("conversion_support") == "supported_basic_media"
            ),
            "metadata_only": sum(1 for row in segment_rows if row.get("conversion_support") == "metadata_only"),
            "unsupported": sum(1 for row in segment_rows if row.get("conversion_support") == "unsupported"),
        },
        "files": files,
        "resolve_project_name": f"{job}_Resolve_{timestamp}",
        "resolve_timeline_name": f"{job}_{candidate.timeline_id}",
        "messages": messages,
    }

    write_csv(package_dir / files["segments_csv"], segment_rows, SEGMENT_FIELDS)
    write_csv(package_dir / files["track_inventory_csv"], track_rows, TRACK_FIELDS)
    write_csv(package_dir / files["relink_map_csv"], relink_rows, RELINK_FIELDS)
    write_csv(package_dir / files["missing_csv"], missing_rows, MISSING_FIELDS)
    write_csv(package_dir / files["text_labels_csv"], text_rows, TEXT_FIELDS)
    write_srt(package_dir / files["text_labels_srt"], text_rows)
    shutil.copy2(template_path, package_dir / files["source_template_json"])
    write_json(package_dir / files["conversion_summary_json"], summary)
    write_fidelity_report(package_dir / files["fidelity_report_md"], summary, segment_rows, text_rows, missing_rows)
    write_validation_report(
        package_dir / files["validation_report_md"],
        args,
        candidates,
        selected,
        messages,
        invalid_project_files,
        package_dir,
    )
    write_resolve_build_script(package_dir / files["resolve_build_py"], job, files["conversion_summary_json"])
    write_package_readme(package_dir / files["readme_md"], summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline Jianying/CapCut draft to Resolve conversion package builder."
    )
    parser.add_argument("--draft-root", required=True, help="Draft folder or search root containing Timelines/<id>/template.json.")
    parser.add_argument("--material-root", required=True, help="Root used for exact filename media relink search.")
    parser.add_argument("--out", required=True, help="Parent directory where a timestamped conversion package is created.")
    parser.add_argument("--timeline-id", default="", help="Optional Timelines/<id> folder name to select.")
    parser.add_argument("--job-name", default="", help="Optional package/job name; sanitized for filenames.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = run_conversion(args)
    except Exception as exc:  # noqa: BLE001 - CLI should emit concise failure.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
