"""Shared reads from frozen source material; no search and no evaluator imports."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path


class SourceError(ValueError):
    pass


def gps_degrees(parts) -> float:
    """Pillow versions expose either IFDRational or legacy numerator/denominator pairs."""

    def rational(value):
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return float(value[0]) / float(value[1])
        return float(value)

    if len(parts) != 3:
        raise ValueError("gps_dms_requires_three_components")
    result = rational(parts[0]) + rational(parts[1]) / 60 + rational(parts[2]) / 3600
    if not math.isfinite(result):
        raise ValueError("gps_coordinate_not_finite")
    return result


def gps_point(gps: dict) -> dict:
    lat, lon = gps_degrees(gps[2]), gps_degrees(gps[4])

    def direction(value):
        return (value.decode("ascii") if isinstance(value, bytes) else str(value)).strip().upper()

    lat_ref, lon_ref = direction(gps.get(1)), direction(gps.get(3))
    if lat_ref not in ("N", "S") or lon_ref not in ("E", "W"):
        raise ValueError("gps_direction_missing_or_invalid")
    lat *= -1 if lat_ref == "S" else 1
    lon *= -1 if lon_ref == "W" else 1
    if abs(lat) > 90 or abs(lon) > 180:
        raise ValueError("gps_coordinate_out_of_range")
    return {"latitude": lat, "longitude": lon}


def exif_ifd(exif, tag: int) -> dict:
    value = exif.get(tag)
    # Pillow 7 exposes an already-decoded GPS dict directly; get_ifd then returns None.
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    nested = exif.get_ifd(tag)
    if not isinstance(nested, dict):
        raise TypeError("exif_ifd_present_but_unreadable")
    return nested


class SourceStore:
    def __init__(
        self,
        sources: dict[str, dict],
        *,
        path_prefixes: list[tuple[str, str]] | None = None,
        captions: dict[str, str] | None = None,
        provenance: dict | None = None,
    ):
        self.sources, self.path_prefixes, self.captions = sources, path_prefixes or [], captions or {}
        self.provenance = provenance or {}
        self._video_metadata_cache = {}

    @classmethod
    def from_jsonl(cls, path: Path, **kwargs):
        sources = {}
        with Path(path).open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                source_id = row["source_id"]
                if source_id in sources:
                    raise SourceError("duplicate_source_id")
                sources[source_id] = row
        return cls(
            sources,
            provenance={
                "sources_path": str(path),
                "sources_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            },
            **kwargs,
        )

    def row(self, source_id: str) -> dict:
        try:
            return self.sources[source_id]
        except KeyError:
            raise SourceError("source_not_in_frozen_manifest") from None

    def metadata(self, source_id: str) -> dict:
        row = self.row(source_id)
        metadata = row.get("metadata") or {}
        result = {
            "source_id": source_id,
            "modality": row["modality"],
            "source_timestamp": row.get("timestamp"),
            "timestamp_basis": row.get("timestamp_basis"),
            "timestamp_role": row.get("timestamp_role"),
            "timezone": row.get("timezone"),
            "source_location": metadata.get("location_name"),
            "location_basis": metadata.get("location_basis"),
            "location_precision": metadata.get("location_precision"),
            "gps": metadata.get("gps"),
            "gps_basis": metadata.get("gps_basis"),
            "capture_time": row.get("capture_time", metadata.get("capture_time")),
            "parent_id": row.get("parent_id"),
            "source_timestamp_sec": row.get("source_timestamp_sec"),
            "source_sha256": row.get("source_sha256"),
            "field_provenance": row.get("field_provenance"),
            "video_frame_provenance": row.get("video_frame_provenance"),
            "video_probe": metadata.get("video_probe"),
        }
        if row["modality"] == "video":
            result.update(self.video_metadata(source_id))
        elif row.get("parent_id") in self.sources and self.sources[row["parent_id"]]["modality"] == "video":
            result["parent_video_metadata"] = self.video_metadata(row["parent_id"])
        if row["modality"] == "image" and result["gps"] is None:
            try:
                from PIL import Image
                from PIL import __version__ as pillow_version

                result["metadata_reader"] = "Pillow/" + pillow_version
                path = self._path(row)
                if hashlib.sha256(path.read_bytes()).hexdigest() != row.get("source_sha256"):
                    raise SourceError("image_sha256_mismatch")
                with Image.open(path) as image:
                    exif = image.getexif()
                    gps = exif_ifd(exif, 34853)
                    camera = exif_ifd(exif, 34665)
                    if not result["capture_time"] and camera.get(36867):
                        offset = camera.get(36881)
                        result["capture_time"] = {
                            "raw": str(camera[36867]),
                            "source": "source_image_exif",
                            "offset": str(offset) if offset else None,
                            "status": "offset_only" if offset else "timezone_unknown",
                        }
                    if 2 in gps and 4 in gps:
                        result.update(gps=gps_point(gps), gps_basis="source_image_exif")
                    result["gps_read_status"] = "present" if result["gps"] else "absent_in_source"
            except SourceError:
                raise
            except (ImportError, OSError, ValueError, ZeroDivisionError, TypeError) as error:
                result["gps_read_status"] = "unavailable_" + type(error).__name__
        return result

    def video_metadata(self, source_id):
        row = self.row(source_id)
        if row["modality"] != "video":
            raise SourceError("source_is_not_video")
        if source_id in self._video_metadata_cache:
            return dict(self._video_metadata_cache[source_id])
        path = self._path(row)
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["source_sha256"]:
            raise SourceError("video_sha256_mismatch")
        try:
            command = [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format_tags:stream_tags",
                "-of",
                "json",
                str(path),
            ]
            process = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
            raw = json.loads(process.stdout)
            tags = []
            tags.extend(("format." + k, v) for k, v in raw.get("format", {}).get("tags", {}).items())
            for i, stream in enumerate(raw.get("streams", [])):
                tags.extend((f"stream[{i}]." + k, v) for k, v in stream.get("tags", {}).items())
            result = {
                "video_metadata_read_status": "read",
                "video_metadata_basis": "local_source_ffprobe",
                "container_times": [
                    {
                        "field": key,
                        "raw": value,
                        "role": "container_creation_timestamp_not_verified_event_time",
                    }
                    for key, value in tags
                    if key.lower().endswith(("creation_time", "creationdate", "date"))
                ],
                "container_locations": [
                    {"field": key, "raw": value} for key, value in tags if "location" in key.lower()
                ],
            }
            coordinates = []
            for location in result["container_locations"]:
                match = re.fullmatch(
                    r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?)?/?", str(location["raw"])
                )
                if match:
                    lat, lon = map(float, match.groups())
                    if abs(lat) <= 90 and abs(lon) <= 180:
                        coordinates.append({"latitude": lat, "longitude": lon, "field": location["field"]})
            if coordinates:
                unique = {(x["latitude"], x["longitude"]) for x in coordinates}
                result["container_gps_candidates"] = coordinates
                result["container_gps_status"] = "consistent" if len(unique) == 1 else "conflicting_tags"
                if len(unique) == 1:
                    lat, lon = next(iter(unique))
                    result.update(
                        gps={"latitude": lat, "longitude": lon}, gps_basis="source_video_container_metadata"
                    )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            result = {"video_metadata_read_status": "unavailable_" + type(error).__name__}
        self._video_metadata_cache[source_id] = result
        return dict(result)

    def text(self, source_id: str) -> str:
        row = self.row(source_id)
        if row["modality"] not in ("email", "text"):
            raise SourceError("source_is_not_text")
        # ATM's published email 'detail' is already the released source record;
        # do not claim it is a raw RFC822 message or invent absent headers.
        text = row.get("detail", row.get("source_text"))
        if not isinstance(text, str):
            raise SourceError("frozen_source_text_missing")
        return text

    def preview(self, source_id: str, query: str, max_chars: int = 1200) -> dict:
        row = self.row(source_id)
        text = self.captions.get(source_id) or row.get("short_summary") or row.get("detail") or ""
        # An excerpt of an already returned source, never a corpus-wide retrieval replacement.
        offset = 0
        if len(text) > max_chars:
            matches = [text.lower().find(word.lower()) for word in query.split() if len(word) > 2]
            matches = [position for position in matches if position >= 0]
            if matches:
                offset = max(0, min(matches) - max_chars // 4)
        return {
            "text": text[offset : offset + max_chars],
            "offset": offset,
            "total_chars": len(text),
            "truncated": len(text) > max_chars,
            "basis": "shared_caption"
            if source_id in self.captions
            else row.get("field_provenance", "released_source_record"),
        }

    def image(self, source_id: str) -> dict:
        row = self.row(source_id)
        if row["modality"] != "image":
            raise SourceError("source_is_not_image_use_registered_keyframe")
        path = self._path(row)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if not row.get("source_sha256") or row["source_sha256"] != digest:
            raise SourceError("image_sha256_mismatch")
        if data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise SourceError("unsupported_image_bytes")
        return {
            "data_uri": "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii"),
            "sha256": digest,
            "bytes": len(data),
            "mime_type": mime,
            "original": row.get("parent_id", source_id) == source_id,
            "pixel_basis": "registered_video_frame"
            if row.get("parent_id", source_id) != source_id
            else "source_image_bytes",
            "parent_id": row.get("parent_id", source_id),
            "source_timestamp_sec": row.get("source_timestamp_sec"),
        }

    def video_frames(self, source_id: str, offset: int, limit: int) -> dict:
        row = self.row(source_id)
        parent_id = source_id if row["modality"] == "video" else row.get("parent_id")
        if not parent_id or self.row(parent_id)["modality"] != "video":
            raise SourceError("source_is_not_video_or_registered_frame")
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100:
            raise SourceError("invalid_video_frame_page")
        frames = [
            r for r in self.sources.values() if r["modality"] == "image" and r.get("parent_id") == parent_id
        ]
        if not frames:
            raise SourceError("registered_video_frames_missing")
        if any(not isinstance(r.get("source_timestamp_sec"), (int, float)) for r in frames):
            raise SourceError("registered_video_frame_timestamp_missing")
        frames.sort(key=lambda r: (r["source_timestamp_sec"], r["source_id"]))
        end = min(len(frames), offset + limit)
        return {
            "parent_id": parent_id,
            "frames": frames[offset:end],
            "offset": offset,
            "next_offset": end if end < len(frames) else None,
            "total_frames": len(frames),
            "coverage": "selected_frames_only_not_every_decoded_frame",
        }

    def _path(self, row: dict) -> Path:
        path = Path(row["asset_path"])
        for old, new in self.path_prefixes:
            try:
                path = Path(new) / path.relative_to(old)
                break
            except ValueError:
                continue
        return path
