"""Lossless GCam legacy MicroVideo converter, unchanged from the tested v2.1 codec."""

from __future__ import annotations

import collections
import copy
import hashlib
import io
import re
import struct
import xml.etree.ElementTree as ET

from .core import MAX_BYTES, sha1, sha256

XMP = b"http://ns.adobe.com/xap/1.0/\x00"
EXT = b"http://ns.adobe.com/xmp/extension/\x00"
CAM = "http://ns.google.com/photos/1.0/camera/"
CONT = "http://ns.google.com/photos/1.0/container/"
ITEM = "http://ns.google.com/photos/1.0/container/item/"
HDR = "http://ns.adobe.com/hdr-gain-map/1.0/"
NOTE = "http://ns.adobe.com/xmp/note/"
TAGS = (
    "MicroVideo",
    "MicroVideoVersion",
    "MicroVideoOffset",
    "MicroVideoPresentationTimestampUs",
)


class Unsupported(ValueError):
    pass


def ensure(ok, msg):
    if not ok:
        raise Unsupported(msg)


def xml(data):
    ensure(
        b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper(),
        "XML DTD/entity not accepted",
    )
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise Unsupported(f"Invalid XMP XML: {e}") from e


def parse_jpeg(data: bytes, start: int = 0):
    ensure(data[start : start + 2] == b"\xff\xd8", f"No JPEG SOI at {start}")
    p = start + 2
    segments, scans = [], []
    dimensions = None
    while p < len(data):
        marker_start = p
        ensure(data[p] == 0xFF, f"Invalid JPEG marker at {p}")
        while p < len(data) and data[p] == 0xFF:
            p += 1
        ensure(p < len(data), "Truncated JPEG marker")
        marker = data[p]
        p += 1
        if marker == 0xD9:
            ensure(scans and dimensions, "JPEG lacks image scan or dimensions")
            return {
                "start": start,
                "end": p,
                "segments": segments,
                "scans": scans,
                "dimensions": dimensions,
            }
        ensure(
            marker not in (0, 0xD8, 1) and not 0xD0 <= marker <= 0xD7,
            "Unsupported standalone JPEG marker",
        )
        ensure(p + 2 <= len(data), "Missing JPEG segment size")
        size = int.from_bytes(data[p : p + 2], "big")
        ensure(size >= 2 and p + size <= len(data), "Truncated JPEG segment")
        a, b = p + 2, p + size
        segments.append((marker, marker_start, a, b))
        if marker in (0xC0, 0xC1, 0xC2):
            ensure(size >= 8, "Short JPEG SOF")
            h, w = struct.unpack(">HH", data[a + 1 : a + 5])
            dimensions = [w, h]
        p = b
        if marker == 0xDA:
            scan_start = p
            while True:
                k = data.find(b"\xff", p)
                ensure(k >= 0, "JPEG scan lacks terminating marker")
                p = k + 1
                while p < len(data) and data[p] == 0xFF:
                    p += 1
                ensure(p < len(data), "Truncated entropy-coded scan")
                if data[p] == 0 or 0xD0 <= data[p] <= 0xD7:
                    p += 1
                    continue
                p = k
                break
            scans.append((scan_start, p))
    raise Unsupported("Missing JPEG EOI")


def xmp_segments(data, j):
    return [
        (a + len(XMP), b)
        for marker, _, a, b in j["segments"]
        if marker == 0xE1 and data[a:b].startswith(XMP)
    ]


def semantic_tree(root):
    return (
        root.tag,
        tuple(sorted(root.attrib.items())),
        (root.text or "").strip(),
        tuple(semantic_tree(c) for c in root),
    )


def scan_metadata(data, j):
    exif = []
    raw_xmp = []
    extended = collections.defaultdict(list)
    icc = []
    for marker, _, a, b in j["segments"]:
        payload = data[a:b]
        if marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            exif.append(payload)
        if marker == 0xE1 and payload.startswith(XMP):
            raw_xmp.append(payload[len(XMP) :])
        if marker == 0xE1 and payload.startswith(EXT):
            e = payload[len(EXT) :]
            ensure(len(e) >= 40, "Short extended XMP")
            guid = e[:32]
            total, offset = struct.unpack(">II", e[32:40])
            ensure(total <= 16 * 1024 * 1024, "Extended XMP too large")
            extended[guid].append((offset, total, e[40:]))
        if marker == 0xE2 and payload.startswith(b"ICC_PROFILE\x00"):
            icc.append(payload)
    assembled = {}
    for guid, pieces in extended.items():
        pieces.sort()
        end, chunks, expected_total = 0, [], pieces[0][1]
        for offset, total, content in pieces:
            ensure(
                offset == end and total == expected_total, "Noncontiguous extended XMP"
            )
            end += len(content)
            chunks.append(content)
        ensure(end == expected_total, "Truncated extended XMP")
        combined = b"".join(chunks)
        ensure(
            hashlib.md5(combined).hexdigest().upper().encode() == guid,
            "Extended XMP MD5 mismatch",
        )
        assembled[guid.decode("ascii")] = combined
    for packet in raw_xmp:
        for node in xml(packet).iter():
            guid = node.get("{" + NOTE + "}HasExtendedXMP")
            if guid:
                ensure(guid in assembled, "Missing referenced extended XMP")
    return {"exif": exif, "icc": icc, "raw_xmp": raw_xmp, "extended": assembled}


def mp4_boxes(data):
    p, boxes = 0, []
    while p < len(data):
        ensure(p + 8 <= len(data), "Truncated MP4 box")
        size, typ = struct.unpack(">I4s", data[p : p + 8])
        header = 8
        if size == 1:
            ensure(p + 16 <= len(data), "Truncated MP4 large box")
            size = struct.unpack(">Q", data[p + 8 : p + 16])[0]
            header = 16
        elif size == 0:
            size = len(data) - p
        ensure(size >= header and p + size <= len(data), "Invalid MP4 box length")
        boxes.append(
            {"type": typ.decode("ascii", "replace"), "offset": p, "bytes": size}
        )
        p += size
    ensure(boxes and boxes[0]["type"] == "ftyp", "MP4 does not start with ftyp")
    ensure({"moov", "mdat"} <= {b["type"] for b in boxes}, "MP4 lacks moov/mdat")
    return boxes


def convert(data: bytes):
    ensure(0 < len(data) <= MAX_BYTES, "File too large or empty")
    primary = parse_jpeg(data)
    packets = xmp_segments(data, primary)
    ensure(len(packets) == 1, "Expected exactly one standard XMP packet")
    a, b = packets[0]
    packet, root = data[a:b], xml(data[a:b])
    namespaces = dict(
        value for _, value in ET.iterparse(io.BytesIO(packet), events=("start-ns",))
    )
    ensure(
        namespaces.get("GCamera") == CAM,
        "Only the verified GCamera namespace prefix is supported",
    )
    values = {}
    for node in root.iter():
        ensure(
            "MotionPhoto" not in node.tag and "MicroVideo" not in node.tag,
            "Element-style motion tags not supported",
        )
        for key, value in node.attrib.items():
            if key.startswith("{" + CAM + "}"):
                local = key.split("}", 1)[1]
                ensure(
                    not local.startswith("MotionPhoto"),
                    "Modern MotionPhoto profile: skipped",
                )
                if local.startswith("MicroVideo"):
                    ensure(
                        local in TAGS and local not in values,
                        "Unknown or duplicate MicroVideo tag",
                    )
                    values[local] = value
            ensure(
                "ContentIdentifier" not in key and "MediaGroupUUID" not in key,
                "Live Photo pairing identifier: skipped",
            )
    ensure(values.get("MicroVideo") == "1", "Not an active legacy GCam MicroVideo")
    ensure(values.get("MicroVideoVersion") == "1", "Unsupported MicroVideoVersion")
    ensure(
        re.fullmatch(r"[1-9][0-9]*", values.get("MicroVideoOffset", "")) is not None,
        "Invalid MicroVideoOffset",
    )
    video_bytes = int(values["MicroVideoOffset"])
    video_start = len(data) - video_bytes
    ensure(
        primary["end"] <= video_start < len(data),
        "Offset overlaps primary image or is outside file",
    )
    meta = scan_metadata(data, primary)
    ensure(len(meta["exif"]) == 1, "Expected one EXIF block")
    for ext in meta["extended"].values():
        tree = xml(ext)
        for node in tree.iter():
            ensure(
                not any(
                    t in node.tag
                    for t in (
                        "MotionPhoto",
                        "MicroVideo",
                        "ContentIdentifier",
                        "MediaGroupUUID",
                    )
                ),
                "Motion/pairing metadata in extended XMP: skipped",
            )
            ensure(
                not any(
                    any(
                        t in k
                        for t in (
                            "MotionPhoto",
                            "MicroVideo",
                            "ContentIdentifier",
                            "MediaGroupUUID",
                        )
                    )
                    for k in node.attrib
                ),
                "Motion/pairing attributes in extended XMP: skipped",
            )
    items = root.findall(".//{" + CONT + "}Item")
    semantics = [i.get("{" + ITEM + "}Semantic") for i in items]
    gain = None
    if primary["end"] < video_start:
        gain = parse_jpeg(data, primary["end"])
        ensure(gain["end"] == video_start, "Unrecognized bytes/images before video")
        gain_packets = xmp_segments(data, gain)
        ensure(len(gain_packets) == 1, "Gain map lacks XMP")
        gain_tree = xml(data[slice(*gain_packets[0])])
        ensure(
            any(n.get("{" + HDR + "}Version") == "1.0" for n in gain_tree.iter()),
            "Second JPEG not an HDR gain map",
        )
        ensure(
            semantics == ["Primary", "GainMap"], "Unexpected image container directory"
        )
        length = items[1].get("{" + ITEM + "}Length")
        ensure(
            length and int(length) == gain["end"] - gain["start"],
            "Gain map length mismatch",
        )
    else:
        ensure(
            not items or semantics == ["Primary"], "Directory describes missing image"
        )
    boxes = mp4_boxes(data[video_start:])
    removed = []
    expression = (
        rb"GCamera:(?:"
        + b"|".join(t.encode() for t in TAGS)
        + rb')\s*=\s*(?:"[^"]*"|\x27[^\x27]*\x27)'
    )

    def blank(match):
        removed.append(match.group().decode())
        return b" " * len(match.group())

    new_packet = re.sub(expression, blank, packet)
    ensure(len(removed) == len(values), "Unexpected tag representation")
    expected = copy.deepcopy(root)
    for node in expected.iter():
        for tag in TAGS:
            node.attrib.pop("{" + CAM + "}" + tag, None)
    ensure(
        semantic_tree(xml(new_packet)) == semantic_tree(expected),
        "Non-motion XML would change",
    )
    ensure(
        b"MicroVideo" not in new_packet and b"MotionPhoto" not in new_packet,
        "Residual motion marker",
    )
    output = data[:a] + new_packet + data[b:video_start]
    ensure(len(output) == video_start, "Image offsets changed")
    again = parse_jpeg(output)
    ensure(again == primary, "JPEG structure changed")
    for x, y in primary["scans"]:
        ensure(output[x:y] == data[x:y], "Compressed image scan changed")
    new_meta = scan_metadata(output, again)
    for key in ("exif", "icc", "extended"):
        ensure(meta[key] == new_meta[key], f"Protected metadata changed: {key}")
    if gain:
        ensure(
            data[gain["start"] : gain["end"]] == output[gain["start"] : gain["end"]],
            "Gain map changed",
        )
    summary = {
        "profile": "GCam legacy MicroVideo 1 + optional HDR gain map",
        "original_bytes": len(data),
        "still_bytes": len(output),
        "video_bytes_removed": video_bytes,
        "reduction_percent": round(100 * video_bytes / len(data), 4),
        "original_sha256": sha256(data),
        "still_sha256": sha256(output),
        "original_sha1": sha1(data),
        "still_sha1": sha1(output),
        "primary_dimensions": primary["dimensions"],
        "primary_jpeg_bytes": primary["end"],
        "gainmap_dimensions": gain["dimensions"] if gain else None,
        "gainmap_bytes": gain["end"] - gain["start"] if gain else 0,
        "gainmap_sha256": sha256(data[gain["start"] : gain["end"]]) if gain else None,
        "exif_payload_sha256": [sha256(x) for x in meta["exif"]],
        "icc_payload_sha256": [sha256(x) for x in meta["icc"]],
        "extended_xmp_sha256": {k: sha256(v) for k, v in meta["extended"].items()},
        "image_scan_sha256": [sha256(data[x:y]) for x, y in primary["scans"]],
        "tags_removed": values,
        "mp4_top_level_boxes": boxes,
        "exif_bit_identical": True,
        "icc_bit_identical": True,
        "gainmap_bit_identical": bool(gain),
        "extended_xmp_bit_identical": True,
        "image_scans_bit_identical": True,
        "standard_motion_tags_after": [],
        "bytes_after_image_components": 0,
    }
    return output, summary
