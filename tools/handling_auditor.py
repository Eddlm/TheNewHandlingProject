from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from itertools import zip_longest
from typing import Any, Callable, Dict, Iterable, List, Sequence


DEFAULT_SCAN_ROOT = Path(r"Y:\FiveM\server-data\resources")
RESOURCE_ROOT = Path(__file__).resolve().parents[1]
ACTIVE_DIR = RESOURCE_ROOT / "Active"
INACTIVE_DIR = RESOURCE_ROOT / "Inactive"
CACHE_DIR = RESOURCE_ROOT / "tools" / ".cache"
CACHE_FILE = CACHE_DIR / "handling_auditor_cache.json"
BACKUP_DIR = RESOURCE_ROOT / "tools" / "backups"
STATUS_ORDER = [
    "Active override",
    "Inactive only",
    "Both active + inactive",
    "Missing",
]
TREE_COLUMNS = (
    "model",
    "handling",
    "status",
    "resource",
    "source",
    "matches",
    "warnings",
)
ITEM_BLOCK_RE = re.compile(r"<Item\b.*?>.*?</Item>", re.IGNORECASE | re.DOTALL)
MODEL_RE = re.compile(r"<modelName>\s*([^<]+?)\s*</modelName>", re.IGNORECASE)
HANDLING_ID_RE = re.compile(r"<handlingId>\s*([^<]+?)\s*</handlingId>", re.IGNORECASE)
HANDLING_NAME_RE = re.compile(r"<handlingName>\s*([^<]+?)\s*</handlingName>", re.IGNORECASE)
MANIFEST_META_RE = re.compile(r"['\"]([^'\"]*\.meta(?:\*[^'\"]*)?)['\"]", re.IGNORECASE)


@dataclass(frozen=True)
class VehicleRecord:
    model_name: str
    model_key: str
    handling_id: str
    handling_key: str
    resource_name: str
    resource_root: Path
    source_path: Path
    manifest_mode_match: bool


@dataclass(frozen=True)
class MatchInfo:
    path: Path
    bucket: str


@dataclass(frozen=True)
class AuditRow:
    vehicle: VehicleRecord
    status: str
    match_files: Sequence[MatchInfo]
    warnings: Sequence[str]


@dataclass(frozen=True)
class ScanProgress:
    stage: str
    current: int = 0
    total: int = 0
    detail: str = ""


@dataclass(frozen=True)
class ResourceManifest:
    resource_root: Path
    manifest_path: Path
    body: str
    likely_vehicle_resource: bool


@dataclass(frozen=True)
class HandlingBlockRecord:
    handling_key: str
    handling_name: str
    text: str
    start: int
    end: int


class FileCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "entries": {}}

    def get(self, file_path: Path, kind: str) -> Any | None:
        key = str(file_path.resolve())
        entry = self.data.get("entries", {}).get(key)
        if not entry or entry.get("kind") != kind:
            return None
        try:
            stat = file_path.stat()
        except OSError:
            return None
        if entry.get("mtime_ns") != stat.st_mtime_ns or entry.get("size") != stat.st_size:
            return None
        return entry.get("data")

    def set(self, file_path: Path, kind: str, payload: Any) -> None:
        try:
            stat = file_path.stat()
        except OSError:
            return
        key = str(file_path.resolve())
        self.data.setdefault("entries", {})[key] = {
            "kind": kind,
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "data": payload,
        }

    def save(self) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


def normalize_key(value: str) -> str:
    return value.strip().lower()


def safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def find_resource_root(file_path: Path, scan_root: Path) -> Path:
    current = file_path.parent
    while current != current.parent:
        if (current / "fxmanifest.lua").exists() or (current / "__resource.lua").exists():
            return current
        if current == scan_root:
            return file_path.parent
        current = current.parent
    return file_path.parent


def list_meta_files(folder: Path) -> Iterable[Path]:
    if not folder.exists():
        return []
    return sorted(path for path in folder.rglob("*.meta") if path.is_file())


def read_text(file_path: Path) -> str:
    try:
        return file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def parse_handling_names(meta_path: Path, cache: FileCache) -> List[str]:
    cached = cache.get(meta_path, "handling_names")
    if cached is not None:
        return list(cached)

    text = read_text(meta_path)
    names = [match.group(1).strip() for match in HANDLING_NAME_RE.finditer(text) if match.group(1).strip()]
    cache.set(meta_path, "handling_names", names)
    return names


def parse_handling_blocks(meta_path: Path, cache: FileCache) -> Dict[str, List[str]]:
    cached = cache.get(meta_path, "handling_blocks")
    if cached is not None:
        return {key: list(value) for key, value in cached.items()}

    text = read_text(meta_path)
    block_map: Dict[str, List[str]] = {}
    for block in ITEM_BLOCK_RE.findall(text):
        match = HANDLING_NAME_RE.search(block)
        if not match:
            continue
        handling_name = match.group(1).strip()
        if not handling_name:
            continue
        block_map.setdefault(normalize_key(handling_name), []).append(block.strip())

    cache.set(meta_path, "handling_blocks", block_map)
    return block_map


def extract_handling_block_records(meta_path: Path) -> List[HandlingBlockRecord]:
    text = read_text(meta_path)
    records: List[HandlingBlockRecord] = []
    for match in ITEM_BLOCK_RE.finditer(text):
        block_text = match.group(0).strip()
        handling_match = HANDLING_NAME_RE.search(block_text)
        if not handling_match:
            continue
        handling_name = handling_match.group(1).strip()
        if not handling_name:
            continue
        records.append(
            HandlingBlockRecord(
                handling_key=normalize_key(handling_name),
                handling_name=handling_name,
                text=block_text,
                start=match.start(),
                end=match.end(),
            )
        )
    return records


def build_comparison_text(matches: Sequence[MatchInfo], handling_key: str, cache: FileCache) -> str:
    if not matches:
        return "No matching handling entry."

    sections: List[str] = []
    for match in matches:
        block_map = parse_handling_blocks(match.path, cache)
        blocks = block_map.get(handling_key, [])
        header = f"[{match.bucket}] {safe_relative(match.path, RESOURCE_ROOT)}"
        if not blocks:
            sections.append(f"{header}\n\nHandling name found in index, but the full block could not be extracted.")
            continue
        for idx, block in enumerate(blocks, start=1):
            section_header = header if len(blocks) == 1 else f"{header} (entry {idx})"
            sections.append(f"{section_header}\n\n{block}")
    return "\n\n" + ("\n\n" + ("-" * 80) + "\n\n").join(sections)


def build_handling_index(
    folder: Path, bucket: str, cache: FileCache, progress_callback: Callable[[ScanProgress], None] | None = None
) -> Dict[str, List[MatchInfo]]:
    index: Dict[str, List[MatchInfo]] = {}
    meta_files = list(list_meta_files(folder))
    total = len(meta_files)
    if progress_callback is not None:
        progress_callback(ScanProgress(stage=f"Indexing {bucket} overrides", current=0, total=total))

    for index_num, meta_path in enumerate(meta_files, start=1):
        for handling_name in parse_handling_names(meta_path, cache):
            key = normalize_key(handling_name)
            index.setdefault(key, []).append(MatchInfo(path=meta_path, bucket=bucket))
        if progress_callback is not None:
            progress_callback(
                ScanProgress(
                    stage=f"Indexing {bucket} overrides",
                    current=index_num,
                    total=total,
                    detail=safe_relative(meta_path, RESOURCE_ROOT),
                )
            )
    return index


def parse_vehicles_meta(
    meta_path: Path, scan_root: Path, manifest_mode_match: bool, cache: FileCache
) -> List[VehicleRecord]:
    cache_key = f"vehicles:{int(manifest_mode_match)}"
    cached = cache.get(meta_path, cache_key)
    if cached is not None:
        return [
            VehicleRecord(
                model_name=item["model_name"],
                model_key=item["model_key"],
                handling_id=item["handling_id"],
                handling_key=item["handling_key"],
                resource_name=item["resource_name"],
                resource_root=Path(item["resource_root"]),
                source_path=Path(item["source_path"]),
                manifest_mode_match=item["manifest_mode_match"],
            )
            for item in cached
        ]

    text = read_text(meta_path)
    resource_root = find_resource_root(meta_path, scan_root)
    resource_name = resource_root.name
    rows: List[VehicleRecord] = []

    for block in ITEM_BLOCK_RE.findall(text):
        model_match = MODEL_RE.search(block)
        handling_match = HANDLING_ID_RE.search(block)
        if not model_match or not handling_match:
            continue

        model_name = model_match.group(1).strip()
        handling_id = handling_match.group(1).strip()
        if not model_name or not handling_id:
            continue

        rows.append(
            VehicleRecord(
                model_name=model_name,
                model_key=normalize_key(model_name),
                handling_id=handling_id,
                handling_key=normalize_key(handling_id),
                resource_name=resource_name,
                resource_root=resource_root,
                source_path=meta_path,
                manifest_mode_match=manifest_mode_match,
            )
        )

    cache.set(
        meta_path,
        cache_key,
        [
            {
                "model_name": row.model_name,
                "model_key": row.model_key,
                "handling_id": row.handling_id,
                "handling_key": row.handling_key,
                "resource_name": row.resource_name,
                "resource_root": str(row.resource_root),
                "source_path": str(row.source_path),
                "manifest_mode_match": row.manifest_mode_match,
            }
            for row in rows
        ],
    )
    return rows


def discover_manifests(manifest_paths: Sequence[Path]) -> Dict[Path, ResourceManifest]:
    manifests: Dict[Path, ResourceManifest] = {}
    for manifest_path in manifest_paths:
        resource_root = manifest_path.parent
        body = read_text(manifest_path).replace("\\", "/").lower()
        likely = any(token in body for token in ("vehicle_metadata_file", "vehicles.meta", "carvariations.meta", "carcols.meta"))
        manifests[resource_root] = ResourceManifest(
            resource_root=resource_root,
            manifest_path=manifest_path,
            body=body,
            likely_vehicle_resource=likely,
        )
    return manifests


def discover_relevant_files(scan_root: Path) -> tuple[List[Path], List[Path]]:
    rg_path = shutil.which("rg")
    vehicles: List[Path] = []
    manifests: List[Path] = []

    if rg_path:
        try:
            result = subprocess.run(
                [rg_path, "--files", "-g", "vehicles.meta", "-g", "fxmanifest.lua", "-g", "__resource.lua", "."],
                cwd=scan_root,
                capture_output=True,
                text=True,
                check=True,
            )
            for line in result.stdout.splitlines():
                rel = line.strip()
                if not rel:
                    continue
                path = (scan_root / rel).resolve()
                name = path.name.lower()
                if name == "vehicles.meta":
                    vehicles.append(path)
                elif name in {"fxmanifest.lua", "__resource.lua"}:
                    manifests.append(path)
            return sorted(vehicles), sorted(manifests)
        except (OSError, subprocess.SubprocessError):
            pass

    for current_root, _dirs, files in os.walk(scan_root):
        root_path = Path(current_root)
        for file_name in files:
            lowered = file_name.lower()
            if lowered == "vehicles.meta":
                vehicles.append((root_path / file_name).resolve())
            elif lowered in {"fxmanifest.lua", "__resource.lua"}:
                manifests.append((root_path / file_name).resolve())
    return sorted(vehicles), sorted(manifests)


def path_matches_manifest(meta_path: Path, resource_root: Path, manifest_body: str) -> bool:
    if not manifest_body:
        return False

    rel = safe_relative(meta_path, resource_root).replace("\\", "/").lower()
    basename = meta_path.name.lower()
    parent_rel = meta_path.parent.relative_to(resource_root).as_posix().lower()

    if rel in manifest_body or basename in manifest_body:
        return True
    if "vehicle_metadata_file" in manifest_body and basename == "vehicles.meta":
        return True
    for pattern in MANIFEST_META_RE.findall(manifest_body):
        pattern = pattern.replace("\\", "/").lower()
        if rel == pattern or rel.endswith(pattern):
            return True
        if "*" in pattern and fnmatch.fnmatch(rel, pattern):
            return True
    if parent_rel and "vehicles.meta" in manifest_body and parent_rel in manifest_body:
        return True
    return False


def scan_vehicle_records_with_progress(
    scan_root: Path, cache: FileCache, progress_callback: Callable[[ScanProgress], None] | None = None
) -> List[VehicleRecord]:
    if progress_callback is not None:
        progress_callback(ScanProgress(stage="Discovering manifests and vehicles.meta files"))
    vehicles_paths, manifest_paths = discover_relevant_files(scan_root)
    manifests = discover_manifests(manifest_paths)
    total = len(vehicles_paths)
    records: List[VehicleRecord] = []

    if progress_callback is not None:
        progress_callback(ScanProgress(stage="Scanning vehicles.meta files", current=0, total=total))

    for index, vehicles_path in enumerate(vehicles_paths, start=1):
        resource_root = find_resource_root(vehicles_path, scan_root)
        manifest = manifests.get(resource_root)
        manifest_body = manifest.body if manifest else ""
        manifest_match = path_matches_manifest(vehicles_path, resource_root, manifest_body)
        records.extend(parse_vehicles_meta(vehicles_path, scan_root, manifest_match, cache))
        if progress_callback is not None:
            progress_callback(
                ScanProgress(
                    stage="Scanning vehicles.meta files",
                    current=index,
                    total=total,
                    detail=safe_relative(vehicles_path, scan_root),
                )
            )
    return records


def collect_duplicate_maps(records: Sequence[VehicleRecord]) -> tuple[Dict[str, int], Dict[str, int]]:
    model_counts: Dict[str, int] = {}
    handling_counts: Dict[str, int] = {}
    for record in records:
        model_counts[record.model_key] = model_counts.get(record.model_key, 0) + 1
        handling_counts[record.handling_key] = handling_counts.get(record.handling_key, 0) + 1
    return model_counts, handling_counts


def build_audit_rows(records: Sequence[VehicleRecord], cache: FileCache) -> List[AuditRow]:
    active_index = build_handling_index(ACTIVE_DIR, "Active", cache)
    inactive_index = build_handling_index(INACTIVE_DIR, "Inactive", cache)
    return _build_rows(records, active_index, inactive_index)


def _build_rows(
    records: Sequence[VehicleRecord],
    active_index: Dict[str, List[MatchInfo]],
    inactive_index: Dict[str, List[MatchInfo]],
    progress_callback: Callable[[ScanProgress], None] | None = None,
) -> List[AuditRow]:
    all_records = list(records)
    model_counts, handling_counts = collect_duplicate_maps(all_records)
    rows: List[AuditRow] = []
    total = len(all_records)

    if progress_callback is not None:
        progress_callback(ScanProgress(stage="Comparing handling IDs", current=0, total=total))

    for index, record in enumerate(all_records, start=1):
        active_matches = active_index.get(record.handling_key, [])
        inactive_matches = inactive_index.get(record.handling_key, [])
        warnings: List[str] = []

        if active_matches and inactive_matches:
            status = "Both active + inactive"
            warnings.append("Handling exists in both Active and Inactive.")
        elif active_matches:
            status = "Active override"
        elif inactive_matches:
            status = "Inactive only"
            warnings.append("Handling exists only in Inactive.")
        else:
            status = "Missing"
            warnings.append("Handling is not present in edd-handling.")

        if model_counts.get(record.model_key, 0) > 1:
            warnings.append("Vehicle model is defined in multiple vehicles.meta entries.")
        if handling_counts.get(record.handling_key, 0) > 1:
            warnings.append("Handling ID is referenced by multiple vehicle entries.")
        if len(active_matches) > 1:
            warnings.append("Multiple Active files define this handling.")
        if len(inactive_matches) > 1:
            warnings.append("Multiple Inactive files define this handling.")
        if not record.manifest_mode_match:
            warnings.append("vehicles.meta does not appear manifest-referenced.")

        rows.append(
            AuditRow(
                vehicle=record,
                status=status,
                match_files=[*active_matches, *inactive_matches],
                warnings=warnings,
            )
        )
        if progress_callback is not None and (index == total or index % 5 == 0):
            progress_callback(
                ScanProgress(
                    stage="Comparing handling IDs",
                    current=index,
                    total=total,
                    detail=f"{record.model_name} -> {record.handling_id}",
                )
            )

    return rows


def build_audit_rows_with_progress(
    records: Sequence[VehicleRecord], cache: FileCache, progress_callback: Callable[[ScanProgress], None] | None = None
) -> List[AuditRow]:
    active_index = build_handling_index(ACTIVE_DIR, "Active", cache, progress_callback)
    inactive_index = build_handling_index(INACTIVE_DIR, "Inactive", cache, progress_callback)
    rows = _build_rows(records, active_index, inactive_index, progress_callback)
    if progress_callback is not None:
        progress_callback(ScanProgress(stage="Saving scan cache"))
    cache.save()
    if progress_callback is not None:
        progress_callback(ScanProgress(stage="Finished", current=len(rows), total=len(rows)))
    return rows


def summary_payload(rows: Sequence[AuditRow], mode: str) -> dict:
    counts = {status: 0 for status in STATUS_ORDER}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return {"mode": mode, "rows": len(rows), "counts": counts}


def backup_file(file_path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_name = f"{timestamp}__{file_path.name}"
    backup_path = BACKUP_DIR / backup_name
    shutil.copy2(file_path, backup_path)
    return backup_path


def write_text_file(file_path: Path, content: str) -> None:
    file_path.write_text(content, encoding="utf-8", newline="\n")


def replace_single_handling_block(target_path: Path, original_block: HandlingBlockRecord, replacement_text: str) -> None:
    source_text = read_text(target_path)
    current_records = extract_handling_block_records(target_path)
    current_match = next(
        (
            record
            for record in current_records
            if record.handling_key == original_block.handling_key and record.text == original_block.text
        ),
        None,
    )
    if current_match is None:
        raise RuntimeError(f"Could not find the expected handling block in {target_path.name}.")
    updated = source_text[: current_match.start] + replacement_text + source_text[current_match.end :]
    write_text_file(target_path, updated)


def remove_single_handling_block(target_path: Path, original_block: HandlingBlockRecord) -> None:
    source_text = read_text(target_path)
    current_records = extract_handling_block_records(target_path)
    current_match = next(
        (
            record
            for record in current_records
            if record.handling_key == original_block.handling_key and record.text == original_block.text
        ),
        None,
    )
    if current_match is None:
        raise RuntimeError(f"Could not find the expected handling block in {target_path.name}.")

    start = current_match.start
    end = current_match.end
    while end < len(source_text) and source_text[end] in "\r\n\t ":
        end += 1
    updated = source_text[:start] + source_text[end:]
    write_text_file(target_path, updated)


class AuditorApp:
    def __init__(self, root: tk.Tk, scan_root: Path) -> None:
        self.root = root
        self.scan_root = scan_root
        self.root.title("edd-handling Coverage Auditor")
        self.root.geometry("1640x900")

        self.search_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="All vehicles.meta")
        self.status_vars = {status: tk.BooleanVar(value=True) for status in STATUS_ORDER}
        self.summary_var = tk.StringVar(value="Scanning...")
        self.progress_var = tk.StringVar(value="Preparing scan...")

        self.all_rows: List[AuditRow] = []
        self.filtered_rows: List[AuditRow] = []
        self.scan_queue: queue.Queue = queue.Queue()
        self.is_scanning = False
        self.cache_for_details = FileCache(CACHE_FILE)
        self._syncing_compare_scroll = False
        self.selected_row: AuditRow | None = None
        self.action_var = tk.StringVar(value="Select a row with one Active and one Inactive match to enable safe actions.")

        self._build_ui()
        self.root.after(50, self.refresh_scan)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        controls = ttk.Frame(self.root, padding=10)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(7, weight=1)

        ttk.Label(controls, text="Mode").grid(row=0, column=0, padx=(0, 6), sticky="w")
        mode_box = ttk.Combobox(
            controls,
            textvariable=self.mode_var,
            values=["All vehicles.meta", "Manifest-aware"],
            state="readonly",
            width=18,
        )
        mode_box.grid(row=0, column=1, padx=(0, 12), sticky="w")
        mode_box.bind("<<ComboboxSelected>>", lambda _event: self.apply_filters())

        ttk.Label(controls, text="Search").grid(row=0, column=2, padx=(0, 6), sticky="w")
        search_entry = ttk.Entry(controls, textvariable=self.search_var, width=32)
        search_entry.grid(row=0, column=3, padx=(0, 12), sticky="w")
        search_entry.bind("<KeyRelease>", lambda _event: self.apply_filters())

        self.refresh_button = ttk.Button(controls, text="Refresh Scan", command=self.refresh_scan)
        self.refresh_button.grid(row=0, column=4, padx=(0, 12))

        status_frame = ttk.Frame(controls)
        status_frame.grid(row=0, column=5, padx=(0, 12), sticky="w")
        for idx, status in enumerate(STATUS_ORDER):
            ttk.Checkbutton(status_frame, text=status, variable=self.status_vars[status], command=self.apply_filters).grid(
                row=0, column=idx, padx=(0, 6), sticky="w"
            )

        ttk.Label(controls, textvariable=self.summary_var, anchor="e").grid(row=0, column=7, sticky="e")

        progress_frame = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        progress_frame.grid(row=1, column=0, sticky="ew")
        progress_frame.columnconfigure(1, weight=1)
        ttk.Label(progress_frame, textvariable=self.progress_var).grid(row=0, column=0, sticky="w")
        self.progressbar = ttk.Progressbar(progress_frame, mode="indeterminate")
        self.progressbar.grid(row=0, column=1, padx=(12, 0), sticky="ew")

        content = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        content.grid(row=2, column=0, sticky="nsew")

        table_frame = ttk.Frame(content, padding=(10, 0, 10, 10))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        content.add(table_frame, weight=3)

        self.tree = ttk.Treeview(table_frame, columns=TREE_COLUMNS, show="headings", height=22)
        widths = {
            "model": 160,
            "handling": 160,
            "status": 170,
            "resource": 170,
            "source": 340,
            "matches": 340,
            "warnings": 340,
        }
        labels = {
            "model": "Model",
            "handling": "Handling ID",
            "status": "Status",
            "resource": "Resource",
            "source": "vehicles.meta Path",
            "matches": "edd-handling Match Files",
            "warnings": "Warnings",
        }
        for column in TREE_COLUMNS:
            self.tree.heading(column, text=labels[column], command=lambda col=column: self.sort_by(col, False))
            self.tree.column(column, width=widths[column], stretch=True)

        tree_scroll_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        tree_scroll_x = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll_y.grid(row=0, column=1, sticky="ns")
        tree_scroll_x.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        detail_frame = ttk.LabelFrame(content, text="Selection Details", padding=10)
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(1, weight=1)
        content.add(detail_frame, weight=1)

        self.detail_text = tk.Text(detail_frame, wrap="word", height=12)
        self.detail_text.grid(row=0, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.detail_text.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.detail_text.configure(yscrollcommand=detail_scroll.set, state="disabled")

        compare_frame = ttk.Frame(detail_frame, padding=(0, 10, 0, 0))
        compare_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")
        compare_frame.columnconfigure(0, weight=1)
        compare_frame.columnconfigure(1, weight=1)
        compare_frame.rowconfigure(1, weight=1)

        self.active_compare_label = tk.StringVar(value="Active Comparison")
        self.inactive_compare_label = tk.StringVar(value="Inactive Comparison")
        ttk.Label(compare_frame, textvariable=self.active_compare_label).grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Label(compare_frame, textvariable=self.inactive_compare_label).grid(row=0, column=1, sticky="w", padx=(8, 0))

        active_panel = ttk.Frame(compare_frame)
        active_panel.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        active_panel.columnconfigure(0, weight=1)
        active_panel.rowconfigure(0, weight=1)

        inactive_panel = ttk.Frame(compare_frame)
        inactive_panel.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        inactive_panel.columnconfigure(0, weight=1)
        inactive_panel.rowconfigure(0, weight=1)

        self.active_compare_text = tk.Text(active_panel, wrap="none", height=16)
        self.active_compare_text.grid(row=0, column=0, sticky="nsew")
        self.active_compare_scroll_y = ttk.Scrollbar(
            active_panel,
            orient="vertical",
            command=lambda *args: self.compare_scroll_command("active", *args),
        )
        self.active_compare_scroll_y.grid(row=0, column=1, sticky="ns")
        active_compare_scroll_x = ttk.Scrollbar(active_panel, orient="horizontal", command=self.active_compare_text.xview)
        active_compare_scroll_x.grid(row=1, column=0, sticky="ew")
        self.active_compare_text.configure(
            yscrollcommand=lambda first, last: self.on_compare_yscroll("active", first, last),
            xscrollcommand=active_compare_scroll_x.set,
            state="disabled",
        )
        self.active_compare_text.tag_configure("diff", background="#fff2a8")

        self.inactive_compare_text = tk.Text(inactive_panel, wrap="none", height=16)
        self.inactive_compare_text.grid(row=0, column=0, sticky="nsew")
        self.inactive_compare_scroll_y = ttk.Scrollbar(
            inactive_panel,
            orient="vertical",
            command=lambda *args: self.compare_scroll_command("inactive", *args),
        )
        self.inactive_compare_scroll_y.grid(row=0, column=1, sticky="ns")
        inactive_compare_scroll_x = ttk.Scrollbar(inactive_panel, orient="horizontal", command=self.inactive_compare_text.xview)
        inactive_compare_scroll_x.grid(row=1, column=0, sticky="ew")
        self.inactive_compare_text.configure(
            yscrollcommand=lambda first, last: self.on_compare_yscroll("inactive", first, last),
            xscrollcommand=inactive_compare_scroll_x.set,
            state="disabled",
        )
        self.inactive_compare_text.tag_configure("diff", background="#fff2a8")

        action_frame = ttk.LabelFrame(content, text="Safe Actions", padding=10)
        action_frame.columnconfigure(2, weight=1)
        content.add(action_frame, weight=0)

        self.promote_button = ttk.Button(
            action_frame,
            text="Update Active With Inactive And Remove Inactive",
            command=self.promote_inactive_to_active,
            state="disabled",
        )
        self.promote_button.grid(row=0, column=0, padx=(0, 8), sticky="w")

        self.discard_button = ttk.Button(
            action_frame,
            text="Discard Inactive Section",
            command=self.discard_inactive_section,
            state="disabled",
        )
        self.discard_button.grid(row=0, column=1, padx=(0, 8), sticky="w")

        ttk.Label(action_frame, textvariable=self.action_var, wraplength=780, justify="left").grid(
            row=0, column=2, sticky="ew"
        )

    def refresh_scan(self) -> None:
        if self.is_scanning:
            return
        self.is_scanning = True
        self.refresh_button.configure(state="disabled")
        self.summary_var.set("Scanning resources...")
        self.progress_var.set("Loading cached scan data...")
        self.progressbar.start(12)
        self.all_rows = []
        self.filtered_rows = []
        self.populate_tree()
        self.set_detail("Scan in progress...\n\nThe auditor is loading cached results where possible and rescanning any changed files.")
        self.set_comparison_text(self.active_compare_text, "Scan in progress...")
        self.set_comparison_text(self.inactive_compare_text, "Scan in progress...")
        self.active_compare_label.set("Active Comparison")
        self.inactive_compare_label.set("Inactive Comparison")
        self.selected_row = None
        self.update_action_state()
        threading.Thread(target=self._scan_worker, daemon=True).start()
        self.root.after(100, self.poll_scan_queue)

    def _scan_worker(self) -> None:
        cache = FileCache(CACHE_FILE)

        def emit_progress(progress: ScanProgress) -> None:
            self.scan_queue.put(("progress", progress))

        try:
            records = scan_vehicle_records_with_progress(self.scan_root, cache, emit_progress)
            rows = build_audit_rows_with_progress(records, cache, emit_progress)
            self.scan_queue.put(("done", rows))
        except Exception as exc:  # pragma: no cover
            self.scan_queue.put(("error", str(exc)))

    def poll_scan_queue(self) -> None:
        while True:
            try:
                message_type, payload = self.scan_queue.get_nowait()
            except queue.Empty:
                break
            if message_type == "progress":
                self.update_progress(payload)
            elif message_type == "done":
                self.all_rows = payload
                self.is_scanning = False
                self.progressbar.stop()
                self.refresh_button.configure(state="normal")
                self.progress_var.set("Scan complete.")
                self.apply_filters()
            elif message_type == "error":
                self.is_scanning = False
                self.progressbar.stop()
                self.refresh_button.configure(state="normal")
                self.summary_var.set("Scan failed")
                self.progress_var.set(str(payload))
                self.set_detail(f"Scan failed:\n\n{payload}")
        if self.is_scanning:
            self.root.after(100, self.poll_scan_queue)

    def update_progress(self, progress: ScanProgress) -> None:
        if progress.total:
            self.progress_var.set(f"{progress.stage} ({progress.current}/{progress.total}) {progress.detail}".strip())
        else:
            self.progress_var.set(f"{progress.stage} {progress.detail}".strip())

    def active_mode_rows(self) -> List[AuditRow]:
        if self.mode_var.get() == "Manifest-aware":
            return [row for row in self.all_rows if row.vehicle.manifest_mode_match]
        return list(self.all_rows)

    def apply_filters(self) -> None:
        rows = self.active_mode_rows()
        query = self.search_var.get().strip().lower()
        enabled_statuses = {status for status, var in self.status_vars.items() if var.get()}
        filtered: List[AuditRow] = []
        for row in rows:
            if row.status not in enabled_statuses:
                continue
            haystack = " ".join(
                [
                    row.vehicle.model_name,
                    row.vehicle.handling_id,
                    row.vehicle.resource_name,
                    safe_relative(row.vehicle.source_path, self.scan_root),
                    " ".join(w.lower() for w in row.warnings),
                ]
            ).lower()
            if query and query not in haystack:
                continue
            filtered.append(row)
        self.filtered_rows = filtered
        self.populate_tree()
        summary = summary_payload(filtered, self.mode_var.get())
        counts = ", ".join(f"{key}: {value}" for key, value in summary["counts"].items())
        self.summary_var.set(f"Rows: {summary['rows']} | {counts}")

    def populate_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, row in enumerate(self.filtered_rows):
            matches = ", ".join(f"{m.bucket}:{safe_relative(m.path, RESOURCE_ROOT)}" for m in row.match_files)
            warnings = " | ".join(row.warnings)
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    row.vehicle.model_name,
                    row.vehicle.handling_id,
                    row.status,
                    row.vehicle.resource_name,
                    safe_relative(row.vehicle.source_path, self.scan_root),
                    matches,
                    warnings,
                ),
            )

    def on_select(self, _event: object) -> None:
        selection = self.tree.selection()
        if not selection:
            self.selected_row = None
            self.update_action_state()
            return
        row = self.filtered_rows[int(selection[0])]
        self.selected_row = row
        self.cache_for_details = FileCache(CACHE_FILE)
        lines = [
            f"Model: {row.vehicle.model_name}",
            f"Handling ID: {row.vehicle.handling_id}",
            f"Status: {row.status}",
            f"Resource: {row.vehicle.resource_name}",
            f"Resource root: {row.vehicle.resource_root}",
            f"vehicles.meta: {row.vehicle.source_path}",
            f"Manifest-aware included: {'yes' if row.vehicle.manifest_mode_match else 'no'}",
            "",
            "Matching edd-handling files:",
        ]
        if row.match_files:
            lines.extend(f"- {m.bucket}: {safe_relative(m.path, RESOURCE_ROOT)}" for m in row.match_files)
        else:
            lines.append("- None")
        lines.append("")
        lines.append("Warnings:")
        if row.warnings:
            lines.extend(f"- {warning}" for warning in row.warnings)
        else:
            lines.append("- None")
        self.set_detail("\n".join(lines))

        active_matches = [match for match in row.match_files if match.bucket == "Active"]
        inactive_matches = [match for match in row.match_files if match.bucket == "Inactive"]
        self.active_compare_label.set(f"Active Comparison ({len(active_matches)} file(s))")
        self.inactive_compare_label.set(f"Inactive Comparison ({len(inactive_matches)} file(s))")
        active_text = build_comparison_text(active_matches, row.vehicle.handling_key, self.cache_for_details)
        inactive_text = build_comparison_text(inactive_matches, row.vehicle.handling_key, self.cache_for_details)
        self.set_comparison_texts(active_text, inactive_text)
        self.update_action_state()

    def set_detail(self, text: str) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")

    @staticmethod
    def set_comparison_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def get_single_action_blocks(self) -> tuple[MatchInfo, HandlingBlockRecord, MatchInfo, HandlingBlockRecord] | None:
        if self.selected_row is None:
            return None

        active_matches = [match for match in self.selected_row.match_files if match.bucket == "Active"]
        inactive_matches = [match for match in self.selected_row.match_files if match.bucket == "Inactive"]
        if len(active_matches) != 1 or len(inactive_matches) != 1:
            return None

        active_blocks = [
            record
            for record in extract_handling_block_records(active_matches[0].path)
            if record.handling_key == self.selected_row.vehicle.handling_key
        ]
        inactive_blocks = [
            record
            for record in extract_handling_block_records(inactive_matches[0].path)
            if record.handling_key == self.selected_row.vehicle.handling_key
        ]
        if len(active_blocks) != 1 or len(inactive_blocks) != 1:
            return None

        return active_matches[0], active_blocks[0], inactive_matches[0], inactive_blocks[0]

    def update_action_state(self) -> None:
        action_data = self.get_single_action_blocks()
        enabled = action_data is not None
        self.promote_button.configure(state="normal" if enabled else "disabled")
        self.discard_button.configure(state="normal" if enabled else "disabled")

        if self.selected_row is None:
            self.action_var.set("Select a row with one Active and one Inactive match to enable safe actions.")
            return

        if action_data is None:
            self.action_var.set(
                "Actions are disabled because this row is ambiguous. The tool only edits when it finds exactly one matching block in Active and exactly one in Inactive."
            )
            return

        active_match, _active_block, inactive_match, _inactive_block = action_data
        self.action_var.set(
            "Ready for a safe edit. Backups will be created before changing "
            f"{safe_relative(active_match.path, RESOURCE_ROOT)} and/or {safe_relative(inactive_match.path, RESOURCE_ROOT)}."
        )

    def promote_inactive_to_active(self) -> None:
        action_data = self.get_single_action_blocks()
        if action_data is None:
            messagebox.showwarning(
                "Action Not Available",
                "This row is ambiguous. The tool only edits when there is exactly one matching block in Active and one in Inactive.",
            )
            return

        active_match, active_block, inactive_match, inactive_block = action_data
        confirm = messagebox.askyesno(
            "Confirm Active Update",
            "This will replace the Active handling block with the Inactive block and then remove the Inactive block.\n\n"
            f"Active file: {safe_relative(active_match.path, RESOURCE_ROOT)}\n"
            f"Inactive file: {safe_relative(inactive_match.path, RESOURCE_ROOT)}\n\n"
            "Timestamped backups will be created first.",
        )
        if not confirm:
            return

        try:
            active_backup = backup_file(active_match.path)
            inactive_backup = backup_file(inactive_match.path)
            replace_single_handling_block(active_match.path, active_block, inactive_block.text)
            remove_single_handling_block(inactive_match.path, inactive_block)
            self.cache_for_details = FileCache(CACHE_FILE)
            messagebox.showinfo(
                "Update Complete",
                "The Active block was updated and the Inactive block was removed.\n\n"
                f"Backups:\n- {active_backup}\n- {inactive_backup}",
            )
            self.refresh_scan()
        except Exception as exc:
            messagebox.showerror("Update Failed", f"No further action was taken automatically.\n\n{exc}")

    def discard_inactive_section(self) -> None:
        action_data = self.get_single_action_blocks()
        if action_data is None:
            messagebox.showwarning(
                "Action Not Available",
                "This row is ambiguous. The tool only edits when there is exactly one matching block in Active and one in Inactive.",
            )
            return

        _active_match, _active_block, inactive_match, inactive_block = action_data
        confirm = messagebox.askyesno(
            "Confirm Inactive Removal",
            "This will remove the Inactive handling block and keep the Active block unchanged.\n\n"
            f"Inactive file: {safe_relative(inactive_match.path, RESOURCE_ROOT)}\n\n"
            "A timestamped backup will be created first.",
        )
        if not confirm:
            return

        try:
            inactive_backup = backup_file(inactive_match.path)
            remove_single_handling_block(inactive_match.path, inactive_block)
            self.cache_for_details = FileCache(CACHE_FILE)
            messagebox.showinfo(
                "Inactive Section Removed",
                "The Inactive block was removed and the Active file was left unchanged.\n\n"
                f"Backup:\n- {inactive_backup}",
            )
            self.refresh_scan()
        except Exception as exc:
            messagebox.showerror("Removal Failed", f"No further action was taken automatically.\n\n{exc}")

    def set_comparison_texts(self, active_text: str, inactive_text: str) -> None:
        self.set_comparison_text(self.active_compare_text, active_text)
        self.set_comparison_text(self.inactive_compare_text, inactive_text)
        self.highlight_text_differences(active_text, inactive_text)

    def highlight_text_differences(self, active_text: str, inactive_text: str) -> None:
        self.active_compare_text.configure(state="normal")
        self.inactive_compare_text.configure(state="normal")
        self.active_compare_text.tag_remove("diff", "1.0", tk.END)
        self.inactive_compare_text.tag_remove("diff", "1.0", tk.END)

        active_lines = active_text.splitlines()
        inactive_lines = inactive_text.splitlines()

        for line_number, (active_line, inactive_line) in enumerate(
            zip_longest(active_lines, inactive_lines, fillvalue=""),
            start=1,
        ):
            if active_line == inactive_line:
                continue

            matcher = difflib.SequenceMatcher(a=active_line, b=inactive_line)
            for tag, a0, a1, b0, b1 in matcher.get_opcodes():
                if tag == "equal":
                    continue
                self.add_diff_tag(self.active_compare_text, line_number, a0, a1, len(active_line))
                self.add_diff_tag(self.inactive_compare_text, line_number, b0, b1, len(inactive_line))

        self.active_compare_text.configure(state="disabled")
        self.inactive_compare_text.configure(state="disabled")

    @staticmethod
    def add_diff_tag(widget: tk.Text, line_number: int, start_col: int, end_col: int, line_length: int) -> None:
        if line_length == 0:
            return
        if start_col == end_col:
            if start_col >= line_length:
                start_col = max(0, line_length - 1)
                end_col = line_length
            else:
                end_col = min(line_length, start_col + 1)
        start = f"{line_number}.{start_col}"
        end = f"{line_number}.{min(end_col, line_length)}"
        widget.tag_add("diff", start, end)

    def compare_scroll_command(self, source: str, *args: str) -> None:
        source_widget, other_widget = self.get_compare_widgets(source)
        self._syncing_compare_scroll = True
        try:
            source_widget.yview(*args)
            other_widget.yview(*args)
        finally:
            self._syncing_compare_scroll = False

    def on_compare_yscroll(self, source: str, first: str, last: str) -> None:
        if source == "active":
            self.active_compare_scroll_y.set(first, last)
        else:
            self.inactive_compare_scroll_y.set(first, last)

        if self._syncing_compare_scroll:
            return

        source_widget, other_widget = self.get_compare_widgets(source)
        other_scrollbar = self.inactive_compare_scroll_y if source == "active" else self.active_compare_scroll_y

        self._syncing_compare_scroll = True
        try:
            other_widget.yview_moveto(first)
            other_first, other_last = other_widget.yview()
            other_scrollbar.set(other_first, other_last)
        finally:
            self._syncing_compare_scroll = False

    def get_compare_widgets(self, source: str) -> tuple[tk.Text, tk.Text]:
        if source == "active":
            return self.active_compare_text, self.inactive_compare_text
        return self.inactive_compare_text, self.active_compare_text

    def sort_by(self, column: str, descending: bool) -> None:
        indexed = list(enumerate(self.filtered_rows))
        indexed.sort(key=lambda pair: self.sort_key(pair[1], column), reverse=descending)
        self.filtered_rows = [row for _, row in indexed]
        self.populate_tree()
        self.tree.heading(column, command=lambda: self.sort_by(column, not descending))

    @staticmethod
    def sort_key(row: AuditRow, column: str) -> str:
        if column == "model":
            return row.vehicle.model_name.lower()
        if column == "handling":
            return row.vehicle.handling_id.lower()
        if column == "status":
            return f"{STATUS_ORDER.index(row.status):02d}-{row.status.lower()}"
        if column == "resource":
            return row.vehicle.resource_name.lower()
        if column == "source":
            return str(row.vehicle.source_path).lower()
        if column == "matches":
            return ",".join(str(match.path).lower() for match in row.match_files)
        if column == "warnings":
            return " ".join(warning.lower() for warning in row.warnings)
        return ""


def run_gui(scan_root: Path) -> None:
    root = tk.Tk()
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    AuditorApp(root, scan_root)
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone edd-handling coverage auditor.")
    parser.add_argument("--scan-root", type=Path, default=DEFAULT_SCAN_ROOT)
    parser.add_argument("--summary-only", action="store_true", help="Print a JSON summary instead of starting the GUI.")
    parser.add_argument("--mode", choices=["all", "manifest"], default="all", help="Used with --summary-only.")
    args = parser.parse_args()

    scan_root = args.scan_root.resolve()

    if args.summary_only:
        cache = FileCache(CACHE_FILE)
        rows = build_audit_rows(scan_vehicle_records_with_progress(scan_root, cache), cache)
        cache.save()
        if args.mode == "manifest":
            rows = [row for row in rows if row.vehicle.manifest_mode_match]
            mode_label = "Manifest-aware"
        else:
            mode_label = "All vehicles.meta"
        print(json.dumps(summary_payload(rows, mode_label), indent=2))
        return

    run_gui(scan_root)


if __name__ == "__main__":
    main()
