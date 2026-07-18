#!/usr/bin/env python3
"""Restore the regular Grimm tk2d sprite collection to a WebGL data bundle.

The broken build deleted the ``Grimm Cln`` GameObject, Transform, and
tk2dSpriteCollectionData objects from sharedassets392.assets.  References to
the collection were either cleared or left pointing at a now-unrelated path
ID.  This script copies the three original objects from a known-good build and
repairs only references which the known-good build points at that collection.

UnityPy 1.25.0 is the tested writer.
"""

from __future__ import annotations

import argparse
import bisect
import copy
import gc
import hashlib
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

import UnityPy
from UnityPy.files.ObjectReader import ObjectReader


COLLECTION_NAME = "Grimm Cln"
ANIMATION_LIBRARY_NAME = "Grimm Anim"
FIREBAT_NAME = "Grimm Firebat"
COLLECTION_SCRIPT_PATH_ID = 1283
ANIMATION_SCRIPT_PATH_ID = 1653
SPRITE_SCRIPT_PATH_ID = 2092
TARGET_LEVELS = ("level392", "level393", "level443", "level444")
NEW_GAME_OBJECT_PATH_ID = 148
NEW_TRANSFORM_PATH_ID = 149
NEW_COLLECTION_PATH_ID = 150


def pptr(data: bytes | bytearray, offset: int) -> tuple[int, int]:
    return struct.unpack_from("<iq", data, offset)


def set_pptr(data: bytearray, offset: int, file_id: int, path_id: int) -> None:
    struct.pack_into("<iq", data, offset, file_id, path_id)


def script_path_id(data: bytes) -> int:
    if len(data) < 28:
        return -1
    return pptr(data, 16)[1]


def game_object_path_id(data: bytes) -> int:
    if len(data) < 12:
        return -1
    return pptr(data, 0)[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def raw_data(obj) -> bytes:
    """Return staged bytes for new/edited objects, otherwise source bytes."""
    return obj.data if obj.data is not None else obj.get_raw_data()


def read_game_object_name(obj) -> str:
    if obj.data is None:
        return obj.read().m_Name
    data = raw_data(obj)
    component_count = struct.unpack_from("<I", data, 0)[0]
    cursor = 4 + component_count * 12 + 4
    length = struct.unpack_from("<I", data, cursor)[0]
    cursor += 4
    return data[cursor : cursor + length].decode("utf-8")


def only_bundle(environment):
    files = list(environment.files.values())
    if len(files) != 1 or not hasattr(files[0], "files"):
        raise RuntimeError("Expected one Unity bundle file")
    return files[0]


def game_object_names(serialized_file) -> dict[int, str]:
    names: dict[int, str] = {}
    for obj in serialized_file.objects.values():
        if obj.type.name == "GameObject":
            names[obj.path_id] = obj.read().m_Name
    return names


def find_game_object(serialized_file, name: str):
    matches = [
        obj
        for obj in serialized_file.objects.values()
        if obj.type.name == "GameObject" and read_game_object_name(obj) == name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one GameObject named {name!r}, found {len(matches)}")
    return matches[0]


def find_component(serialized_file, game_object_id: int, script_id: int):
    matches = []
    for obj in serialized_file.objects.values():
        if obj.type.name != "MonoBehaviour":
            continue
        raw = raw_data(obj)
        if game_object_path_id(raw) == game_object_id and script_path_id(raw) == script_id:
            matches.append(obj)
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one component for GameObject {game_object_id} and script {script_id}, "
            f"found {len(matches)}"
        )
    return matches[0]


def all_occurrences(data: bytes, needle: bytes) -> list[int]:
    offsets: list[int] = []
    cursor = 0
    while True:
        offset = data.find(needle, cursor)
        if offset < 0:
            return offsets
        offsets.append(offset)
        cursor = offset + 1


@dataclass
class SceneReference:
    object_path_id: int
    collection_file_id: int
    game_object_name: str


@dataclass
class BaseManifest:
    game_object_raw: bytes
    transform_raw: bytes
    collection_raw: bytes
    collection_serialized_type: object
    collection_script_type: object
    animation_collection_offsets: list[int]
    scene_references: dict[str, list[SceneReference]]


def capture_base_manifest(base_data: Path) -> BaseManifest:
    environment = UnityPy.load(str(base_data))
    bundle = only_bundle(environment)
    shared = bundle.files["sharedassets392.assets"]

    collection_go = find_game_object(shared, COLLECTION_NAME)
    components = [entry.component.path_id for entry in collection_go.read().m_Component]
    if len(components) != 2:
        raise RuntimeError(f"{COLLECTION_NAME!r} should have exactly two components")
    transform = shared.objects[components[0]]
    collection = shared.objects[components[1]]
    if transform.type.name != "Transform" or script_path_id(collection.get_raw_data()) != COLLECTION_SCRIPT_PATH_ID:
        raise RuntimeError("The known-good Grimm collection component graph is not the expected shape")

    animation_go = find_game_object(shared, ANIMATION_LIBRARY_NAME)
    animation = find_component(shared, animation_go.path_id, ANIMATION_SCRIPT_PATH_ID)
    collection_needle = struct.pack("<iq", 0, collection.path_id)
    animation_offsets = all_occurrences(animation.get_raw_data(), collection_needle)
    if len(animation_offsets) != 292:
        raise RuntimeError(f"Expected 292 Grimm animation frame references, found {len(animation_offsets)}")

    scene_references: dict[str, list[SceneReference]] = {}
    for level in TARGET_LEVELS:
        scene = bundle.files[level]
        names = game_object_names(scene)
        references: list[SceneReference] = []
        for obj in scene.objects.values():
            if obj.type.name != "MonoBehaviour":
                continue
            raw = obj.get_raw_data()
            if len(raw) < 44 or script_path_id(raw) != SPRITE_SCRIPT_PATH_ID:
                continue
            file_id, path_id = pptr(raw, 32)
            if path_id == collection.path_id:
                references.append(
                    SceneReference(obj.path_id, file_id, names[game_object_path_id(raw)])
                )
        scene_references[level] = references

    expected_counts = {"level392": 41, "level393": 22, "level443": 41, "level444": 22}
    actual_counts = {level: len(refs) for level, refs in scene_references.items()}
    if actual_counts != expected_counts:
        raise RuntimeError(f"Unexpected known-good scene reference counts: {actual_counts}")

    serialized_type = copy.deepcopy(collection.serialized_type)
    script_type = copy.deepcopy(shared.script_types[collection.serialized_type.script_type_index])
    manifest = BaseManifest(
        collection_go.get_raw_data(),
        transform.get_raw_data(),
        collection.get_raw_data(),
        serialized_type,
        script_type,
        animation_offsets,
        scene_references,
    )
    del (
        environment,
        bundle,
        shared,
        collection_go,
        transform,
        collection,
        animation_go,
        animation,
        scene,
        obj,
        names,
        references,
    )
    gc.collect()
    return manifest


def add_object(serialized_file, path_id: int, raw: bytes, template, *, type_id=None, serialized_type=None):
    if path_id in serialized_file.objects:
        raise RuntimeError(f"Cannot add path ID {path_id}; it already exists")
    reader = ObjectReader(
        assets_file=serialized_file,
        reader=serialized_file.reader,
        path_id=path_id,
        type_id=template.type_id if type_id is None else type_id,
        serialized_type=template.serialized_type if serialized_type is None else serialized_type,
        class_id=template.class_id,
        type=template.type,
        byte_start=0,
        byte_size=len(raw),
        is_destroyed=None,
        is_stripped=None,
        data=raw,
    )
    serialized_file.objects[path_id] = reader
    serialized_file.mark_changed()


def repair_inner_bundle(
    current_data: Path, base_data: Path, output_data: Path
) -> tuple[dict[str, object], list[int]]:
    manifest = capture_base_manifest(base_data)

    environment = UnityPy.load(str(current_data))
    bundle = only_bundle(environment)
    shared = bundle.files["sharedassets392.assets"]
    if len(shared.objects) != 147:
        raise RuntimeError(f"Expected 147 current sharedassets392 objects, found {len(shared.objects)}")
    if any(
        obj.type.name == "MonoBehaviour" and script_path_id(obj.get_raw_data()) == COLLECTION_SCRIPT_PATH_ID
        for obj in shared.objects.values()
    ):
        raise RuntimeError("Current bundle already contains a Grimm collection component")

    game_object_raw = bytearray(manifest.game_object_raw)
    if pptr(game_object_raw, 4)[1] != 74 or pptr(game_object_raw, 16)[1] != 122:
        raise RuntimeError("Unexpected known-good Grimm collection GameObject references")
    set_pptr(game_object_raw, 4, 0, NEW_TRANSFORM_PATH_ID)
    set_pptr(game_object_raw, 16, 0, NEW_COLLECTION_PATH_ID)

    transform_raw = bytearray(manifest.transform_raw)
    if pptr(transform_raw, 0)[1] != 55:
        raise RuntimeError("Unexpected known-good Grimm collection Transform reference")
    set_pptr(transform_raw, 0, 0, NEW_GAME_OBJECT_PATH_ID)

    collection_raw = bytearray(manifest.collection_raw)
    if pptr(collection_raw, 0)[1] != 55 or script_path_id(collection_raw) != COLLECTION_SCRIPT_PATH_ID:
        raise RuntimeError("Unexpected known-good Grimm collection component references")
    set_pptr(collection_raw, 0, 0, NEW_GAME_OBJECT_PATH_ID)

    existing_script_ids = [entry.local_identifier_in_file for entry in shared.script_types]
    if COLLECTION_SCRIPT_PATH_ID in existing_script_ids:
        raise RuntimeError("Collection script type unexpectedly already exists")
    new_script_type_index = len(shared.script_types)
    shared.script_types.append(manifest.collection_script_type)
    collection_type = manifest.collection_serialized_type
    collection_type.script_type_index = new_script_type_index
    new_type_id = len(shared.types)
    shared.types.append(collection_type)
    shared.mark_changed()

    game_object_template = next(obj for obj in shared.objects.values() if obj.type.name == "GameObject")
    transform_template = next(obj for obj in shared.objects.values() if obj.type.name == "Transform")
    mono_template = next(obj for obj in shared.objects.values() if obj.type.name == "MonoBehaviour")
    add_object(shared, NEW_GAME_OBJECT_PATH_ID, bytes(game_object_raw), game_object_template)
    add_object(shared, NEW_TRANSFORM_PATH_ID, bytes(transform_raw), transform_template)
    add_object(
        shared,
        NEW_COLLECTION_PATH_ID,
        bytes(collection_raw),
        mono_template,
        type_id=new_type_id,
        serialized_type=collection_type,
    )

    animation_go = find_game_object(shared, ANIMATION_LIBRARY_NAME)
    animation = find_component(shared, animation_go.path_id, ANIMATION_SCRIPT_PATH_ID)
    animation_raw = bytearray(animation.get_raw_data())
    repaired_animation_refs = 0
    for offset in manifest.animation_collection_offsets:
        current_ref = pptr(animation_raw, offset)
        if current_ref not in ((0, 0), (0, 122)):
            raise RuntimeError(f"Unexpected Grimm animation reference {current_ref} at byte {offset}")
        set_pptr(animation_raw, offset, 0, NEW_COLLECTION_PATH_ID)
        repaired_animation_refs += 1
    animation.set_raw_data(bytes(animation_raw))

    firebat_go = find_game_object(shared, FIREBAT_NAME)
    firebat_sprite = find_component(shared, firebat_go.path_id, SPRITE_SCRIPT_PATH_ID)
    firebat_raw = bytearray(firebat_sprite.get_raw_data())
    if pptr(firebat_raw, 32) != (0, 0):
        raise RuntimeError(f"Unexpected current Grimm Firebat collection reference {pptr(firebat_raw, 32)}")
    set_pptr(firebat_raw, 32, 0, NEW_COLLECTION_PATH_ID)
    firebat_sprite.set_raw_data(bytes(firebat_raw))

    repaired_scene_refs: dict[str, int] = {}
    for level, references in manifest.scene_references.items():
        scene = bundle.files[level]
        external_names = {index: item.name for index, item in enumerate(scene.externals, 1)}
        for reference in references:
            obj = scene.objects[reference.object_path_id]
            raw = bytearray(obj.get_raw_data())
            if script_path_id(raw) != SPRITE_SCRIPT_PATH_ID or pptr(raw, 32) != (0, 0):
                raise RuntimeError(
                    f"Unexpected current {level} sprite reference at object {reference.object_path_id}: "
                    f"{pptr(raw, 32)}"
                )
            if external_names.get(reference.collection_file_id) != "sharedassets392.assets":
                raise RuntimeError(
                    f"{level} file ID {reference.collection_file_id} does not resolve to sharedassets392.assets"
                )
            set_pptr(raw, 32, reference.collection_file_id, NEW_COLLECTION_PATH_ID)
            obj.set_raw_data(bytes(raw))
        repaired_scene_refs[level] = len(references)

    output_data.parent.mkdir(parents=True, exist_ok=True)
    output_data.write_bytes(bundle.save(packer="original"))
    return (
        {
            "animation_references": repaired_animation_refs,
            "shared_prefab_references": 1,
            "scene_references": repaired_scene_refs,
            "output_bytes": output_data.stat().st_size,
            "output_sha256": sha256_file(output_data),
        },
        manifest.animation_collection_offsets,
    )


def verify_inner_bundle(path: Path, animation_offsets: list[int]) -> dict[str, object]:
    environment = UnityPy.load(str(path))
    bundle = only_bundle(environment)
    shared = bundle.files["sharedassets392.assets"]
    if len(shared.objects) != 150:
        raise RuntimeError(f"Patched sharedassets392 should contain 150 objects, found {len(shared.objects)}")
    if find_game_object(shared, COLLECTION_NAME).path_id != NEW_GAME_OBJECT_PATH_ID:
        raise RuntimeError("Patched Grimm collection GameObject has the wrong path ID")
    collection = find_component(shared, NEW_GAME_OBJECT_PATH_ID, COLLECTION_SCRIPT_PATH_ID)
    if collection.path_id != NEW_COLLECTION_PATH_ID:
        raise RuntimeError("Patched Grimm collection component has the wrong path ID")

    animation_go = find_game_object(shared, ANIMATION_LIBRARY_NAME)
    animation = find_component(shared, animation_go.path_id, ANIMATION_SCRIPT_PATH_ID)
    animation_raw = animation.get_raw_data()
    repaired = [
        offset
        for offset in animation_offsets
        if pptr(animation_raw, offset) == (0, NEW_COLLECTION_PATH_ID)
    ]
    if len(repaired) != 292:
        raise RuntimeError(f"Patched animation library has {len(repaired)} collection references, expected 292")

    scene_counts: dict[str, int] = {}
    for level in TARGET_LEVELS:
        scene = bundle.files[level]
        count = 0
        for obj in scene.objects.values():
            if obj.type.name != "MonoBehaviour":
                continue
            raw = obj.get_raw_data()
            if len(raw) >= 44 and script_path_id(raw) == SPRITE_SCRIPT_PATH_ID and pptr(raw, 32)[1] == NEW_COLLECTION_PATH_ID:
                count += 1
        scene_counts[level] = count
    expected = {"level392": 41, "level393": 22, "level443": 41, "level444": 22}
    if scene_counts != expected:
        raise RuntimeError(f"Patched scene collection counts are wrong: {scene_counts}")

    return {
        "objects": len(shared.objects),
        "animation_references": len(repaired),
        "scene_references": scene_counts,
        "sha256": sha256_file(path),
    }


@dataclass
class WebDataEntry:
    offset: int
    length: int
    path: str


class PartReader:
    def __init__(self, parts: list[Path]):
        self.parts = parts
        self.sizes = [part.stat().st_size for part in parts]
        self.starts = []
        cursor = 0
        for size in self.sizes:
            self.starts.append(cursor)
            cursor += size
        self.length = cursor

    def copy_range(self, start: int, length: int, writer) -> None:
        cursor = start
        remaining = length
        while remaining:
            index = bisect.bisect_right(self.starts, cursor) - 1
            local_offset = cursor - self.starts[index]
            take = min(4 * 1024 * 1024, remaining, self.sizes[index] - local_offset)
            with self.parts[index].open("rb") as stream:
                stream.seek(local_offset)
                block = stream.read(take)
            if len(block) != take:
                raise RuntimeError("Unexpected end of build part")
            writer.write(block)
            cursor += take
            remaining -= take

    def read(self, start: int, length: int) -> bytes:
        chunks: list[bytes] = []

        class Collector:
            def write(self, data: bytes) -> None:
                chunks.append(data)

        self.copy_range(start, length, Collector())
        return b"".join(chunks)


class PartWriter:
    def __init__(self, directory: Path, basename: str, chunk_size: int):
        self.directory = directory
        self.basename = basename
        self.chunk_size = chunk_size
        self.part_number = 0
        self.current_size = 0
        self.stream = None
        self.paths: list[Path] = []
        directory.mkdir(parents=True, exist_ok=True)

    def _next(self) -> None:
        if self.stream:
            self.stream.close()
        self.part_number += 1
        path = self.directory / f"{self.basename}.part{self.part_number}"
        self.paths.append(path)
        self.stream = path.open("wb")
        self.current_size = 0

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self.stream is None or self.current_size == self.chunk_size:
                self._next()
            take = min(len(view), self.chunk_size - self.current_size)
            self.stream.write(view[:take])
            self.current_size += take
            view = view[take:]

    def close(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None


def data_parts(build_dir: Path) -> list[Path]:
    parts: list[Path] = []
    number = 1
    while (part := build_dir / f"hktruffled.data.part{number}").exists():
        parts.append(part)
        number += 1
    if not parts:
        raise RuntimeError(f"No hktruffled.data parts found in {build_dir}")
    return parts


def parse_web_data_header(reader: PartReader) -> tuple[int, list[WebDataEntry]]:
    prefix = reader.read(0, 20)
    if prefix[:16] != b"UnityWebData1.0\0":
        raise RuntimeError("Build parts do not start with a UnityWebData1.0 header")
    header_length = struct.unpack_from("<I", prefix, 16)[0]
    header = reader.read(0, header_length)
    entries: list[WebDataEntry] = []
    cursor = 20
    while cursor < header_length:
        offset, length, path_length = struct.unpack_from("<III", header, cursor)
        cursor += 12
        path = header[cursor : cursor + path_length].decode("utf-8")
        cursor += path_length
        entries.append(WebDataEntry(offset, length, path))
    if cursor != header_length:
        raise RuntimeError("UnityWebData header length is inconsistent")
    return header_length, entries


def build_web_data_header(header_length: int, entries: list[WebDataEntry]) -> bytes:
    output = bytearray(b"UnityWebData1.0\0" + struct.pack("<I", header_length))
    for entry in entries:
        encoded = entry.path.encode("utf-8")
        output.extend(struct.pack("<III", entry.offset, entry.length, len(encoded)))
        output.extend(encoded)
    if len(output) != header_length:
        raise RuntimeError("Rebuilt UnityWebData header changed size")
    return bytes(output)


def verify_data_parts(build_dir: Path, patched_data: Path) -> dict[str, object]:
    parts = data_parts(build_dir)
    reader = PartReader(parts)
    header_length, entries = parse_web_data_header(reader)
    matches = [entry for entry in entries if entry.path == "data.unity3d"]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one data.unity3d entry, found {len(matches)}")
    target = matches[0]
    if target.length != patched_data.stat().st_size:
        raise RuntimeError(
            f"Installed data.unity3d is {target.length} bytes; "
            f"patched bundle is {patched_data.stat().st_size} bytes"
        )

    previous_end = header_length
    for entry in sorted(entries, key=lambda item: item.offset):
        if entry.offset < previous_end or entry.offset + entry.length > reader.length:
            raise RuntimeError(f"UnityWebData entry is overlapping or out of bounds: {entry.path}")
        previous_end = entry.offset + entry.length

    extracted_hash = hashlib.sha256()

    class Hasher:
        def write(self, data: bytes) -> None:
            extracted_hash.update(data)

    reader.copy_range(target.offset, target.length, Hasher())
    actual_hash = extracted_hash.hexdigest()
    expected_hash = sha256_file(patched_data)
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"Installed data.unity3d hash {actual_hash} does not match patched bundle {expected_hash}"
        )
    return {
        "parts": len(parts),
        "total_bytes": reader.length,
        "header_bytes": header_length,
        "data_offset": target.offset,
        "data_bytes": target.length,
        "data_sha256": actual_hash,
    }


def rebuild_data_parts(build_dir: Path, patched_data: Path, output_dir: Path) -> dict[str, object]:
    parts = data_parts(build_dir)
    reader = PartReader(parts)
    header_length, entries = parse_web_data_header(reader)
    matches = [entry for entry in entries if entry.path == "data.unity3d"]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one data.unity3d entry, found {len(matches)}")
    target = matches[0]
    if target.offset < header_length:
        raise RuntimeError("data.unity3d overlaps the UnityWebData header")

    old_end = target.offset + target.length
    delta = patched_data.stat().st_size - target.length
    new_entries = []
    for entry in entries:
        if entry.path == target.path:
            new_entries.append(WebDataEntry(entry.offset, patched_data.stat().st_size, entry.path))
        elif entry.offset >= old_end:
            new_entries.append(WebDataEntry(entry.offset + delta, entry.length, entry.path))
        else:
            new_entries.append(entry)
    header = build_web_data_header(header_length, new_entries)

    if output_dir.exists():
        shutil.rmtree(output_dir)
    writer = PartWriter(output_dir, "hktruffled.data", parts[0].stat().st_size)
    try:
        writer.write(header)
        reader.copy_range(header_length, target.offset - header_length, writer)
        with patched_data.open("rb") as stream:
            while block := stream.read(4 * 1024 * 1024):
                writer.write(block)
        reader.copy_range(old_end, reader.length - old_end, writer)
    finally:
        writer.close()

    staged_reader = PartReader(writer.paths)
    staged_header_length, staged_entries = parse_web_data_header(staged_reader)
    staged_target = next(entry for entry in staged_entries if entry.path == "data.unity3d")
    if staged_header_length != header_length or staged_target.length != patched_data.stat().st_size:
        raise RuntimeError("Staged UnityWebData header verification failed")
    extracted_hash = hashlib.sha256()

    class Hasher:
        def write(self, data: bytes) -> None:
            extracted_hash.update(data)

    staged_reader.copy_range(staged_target.offset, staged_target.length, Hasher())
    if extracted_hash.hexdigest() != sha256_file(patched_data):
        raise RuntimeError("Staged UnityWebData data.unity3d hash does not match the patched bundle")

    return {
        "parts": len(writer.paths),
        "total_bytes": staged_reader.length,
        "data_delta": delta,
        "data_sha256": extracted_hash.hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    repair = subparsers.add_parser("repair", help="repair and verify an inner data.unity3d bundle")
    repair.add_argument("--current-data", type=Path, required=True)
    repair.add_argument("--base-data", type=Path, required=True)
    repair.add_argument("--output-data", type=Path, required=True)

    verify = subparsers.add_parser("verify", help="verify an already-repaired inner bundle")
    verify.add_argument("--data", type=Path, required=True)
    verify.add_argument("--base-data", type=Path, required=True)

    repack = subparsers.add_parser("repack", help="put a repaired bundle into staged WebGL data parts")
    repack.add_argument("--build-dir", type=Path, required=True)
    repack.add_argument("--patched-data", type=Path, required=True)
    repack.add_argument("--output-dir", type=Path, required=True)

    verify_parts = subparsers.add_parser(
        "verify-parts", help="verify installed WebGL data parts contain the repaired bundle"
    )
    verify_parts.add_argument("--build-dir", type=Path, required=True)
    verify_parts.add_argument("--patched-data", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "repair":
        result, animation_offsets = repair_inner_bundle(
            args.current_data, args.base_data, args.output_data
        )
        verification = verify_inner_bundle(args.output_data, animation_offsets)
        print({"repair": result, "verification": verification})
    elif args.command == "verify":
        manifest = capture_base_manifest(args.base_data)
        print(verify_inner_bundle(args.data, manifest.animation_collection_offsets))
    elif args.command == "repack":
        print(rebuild_data_parts(args.build_dir, args.patched_data, args.output_dir))
    else:
        print(verify_data_parts(args.build_dir, args.patched_data))


if __name__ == "__main__":
    main()
