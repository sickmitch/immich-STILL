"""Metadata for the NEW upload only. Original JPEG and original sidecar never written."""

from __future__ import annotations

import copy
import datetime as dt
import math
import re
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from .core import (
    Stop,
    db_sha,
    equal_date,
    instant,
    require,
)
from .gcam import CAM, CONT, ITEM, xml

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "tiff": "http://ns.adobe.com/tiff/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "digiKam": "http://www.digikam.org/ns/1.0/",
}
for key, uri in NS.items():
    ET.register_namespace(key, uri)


def q(prefix, name):
    return "{" + NS[prefix] + "}" + name


DATE_NAMES = {
    "SubSecDateTimeOriginal",
    "SubSecCreateDate",
    "DateTimeOriginal",
    "CreationDate",
    "CreateDate",
    "MediaCreateDate",
    "DateTimeCreated",
    "GPSDateTime",
    "DateTimeUTC",
    "SonyDateTime2",
    "SourceImageCreateTime",
    "SubSecTimeOriginal",
    "OffsetTimeOriginal",
    "OffsetTimeDigitized",
}


def timezone_offset(zone, when):
    require(
        isinstance(zone, str) and zone, "Fuso del catalogo assente: non lo indovino"
    )
    if zone in ("UTC", "Etc/UTC", "GMT", "Z"):
        return dt.timedelta(0)
    m = re.fullmatch(r"(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?", zone)
    if m:
        return dt.timedelta(
            minutes=(1 if m[1] == "+" else -1) * (int(m[2]) * 60 + int(m[3] or 0))
        )
    try:
        return when.astimezone(ZoneInfo(zone)).utcoffset()
    except Exception as e:
        raise Stop("Fuso non interpretabile: " + zone) from e


def capture_stamp(s):
    a, e = s["asset"], s["exif"]
    capture = instant(a["fileCreatedAt"])
    local = instant(a["localDateTime"])
    require(
        equal_date(e.get("dateTimeOriginal"), a["fileCreatedAt"]),
        "EXIF e data timeline sorgente divergono: non cestino una sorgente ambigua",
    )
    offset = local - capture
    require(
        abs(offset) <= dt.timedelta(hours=14) and offset.total_seconds() % 60 == 0,
        "Data locale non coerente con un offset valido",
    )
    require(
        timezone_offset(e.get("timeZone"), capture) == offset,
        "Fuso e localDateTime della sorgente divergono",
    )
    for field in ("fileCreatedAt", "localDateTime", "fileModifiedAt"):
        require(
            instant(a[field]).microsecond % 1000 == 0,
            "Precisione sub-millisecondo non rappresentabile dalle API: " + field,
        )
    require(
        e.get("rating") in (None, 1, 2, 3, 4, 5),
        "Rating non reimportabile in modo fedele con questa versione",
    )
    value = capture.astimezone(dt.timezone(offset))
    return value.isoformat(timespec="milliseconds")


def make_sidecar(s, existing=None):
    capture = capture_stamp(s)
    e = s["exif"]
    if existing:
        root = copy.deepcopy(xml(existing))
        for node in root.iter():
            for key in [node.tag, *node.attrib]:
                require(
                    not any(
                        x in key
                        for x in (
                            "MotionPhoto",
                            "MicroVideo",
                            "ContentIdentifier",
                            "MediaGroupUUID",
                        )
                    )
                    and not key.startswith(
                        ("{" + CAM + "}", "{" + CONT + "}", "{" + ITEM + "}")
                    ),
                    "Sidecar motion/identificatore di pairing non supportato: originale lasciato attivo",
                )
    else:
        root = ET.Element(q("x", "xmpmeta"))
        ET.SubElement(root, q("rdf", "RDF"))
    rdf = root.find(".//" + q("rdf", "RDF"))
    if root.tag == q("rdf", "RDF"):
        rdf = root
    require(rdf is not None, "Sidecar senza RDF")
    # Replace values in the COPY only; never alter the embedded EXIF or the source sidecar.
    changed = DATE_NAMES | {
        "ImageDescription",
        "Description",
        "description",
        "Rating",
        "TagsList",
        "HierarchicalSubject",
        "Keywords",
    }
    if e.get("latitude") is not None and e.get("longitude") is not None:
        changed |= {"GPSLatitude", "GPSLongitude", "GPSLatitudeRef", "GPSLongitudeRef"}
    for node in root.iter():
        for k in list(node.attrib):
            if k.rsplit("}", 1)[-1] in changed:
                del node.attrib[k]
        for child in list(node):
            if (
                child.tag != q("rdf", "Description")
                and child.tag.rsplit("}", 1)[-1] in changed
            ):
                node.remove(child)
    d = ET.SubElement(rdf, q("rdf", "Description"), {q("rdf", "about"): ""})
    d.set(q("exif", "DateTimeOriginal"), capture)
    d.set(q("xmp", "CreateDate"), capture)
    desc = e.get("description") or ""
    d.set(q("tiff", "ImageDescription"), desc)
    alt = ET.SubElement(ET.SubElement(d, q("dc", "description")), q("rdf", "Alt"))
    ET.SubElement(
        alt, q("rdf", "li"), {"{http://www.w3.org/XML/1998/namespace}lang": "x-default"}
    ).text = desc
    d.set(q("xmp", "Rating"), str(e.get("rating") or 0))
    for field, tag, positive, negative in [
        ("latitude", "GPSLatitude", "N", "S"),
        ("longitude", "GPSLongitude", "E", "W"),
    ]:
        value = e.get(field)
        if value is not None:
            value = float(value)
            require(math.isfinite(value), "Coordinate non finite")
            absolute = abs(value)
            deg = int(absolute)
            minutes = (absolute - deg) * 60
            d.set(
                q("exif", tag),
                f"{deg},{minutes:.12f}{positive if value >= 0 else negative}",
            )
    tags = e.get("tags") or []
    require(
        isinstance(tags, list) and all(isinstance(t, str) for t in tags),
        "Tags EXIF non riconosciuti",
    )
    bag = ET.SubElement(ET.SubElement(d, q("digiKam", "TagsList")), q("rdf", "Bag"))
    for t in tags:
        ET.SubElement(bag, q("rdf", "li")).text = t
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    require(len(data) <= 16 * 1024 * 1024, "Sidecar troppo grande")
    xml(data)
    return data


# Technical/camera properties must survive extraction on the NEW asset.
EXIF_FIELDS = (
    "bitsPerSample",
    "colorspace",
    "exifImageHeight",
    "exifImageWidth",
    "exposureTime",
    "fNumber",
    "focalLength",
    "iso",
    "lensModel",
    "make",
    "model",
    "orientation",
    "profileDescription",
    "projectionType",
    "description",
    "rating",
    "latitude",
    "longitude",
    "city",
    "state",
    "country",
)
DATE_FIELDS = ("fileCreatedAt", "localDateTime", "fileModifiedAt")


def target_differences(source, target, conversion):
    a = target.get("asset") or {}
    e = target.get("exif") or {}
    old = source["asset"]
    oe = source["exif"]
    errors = []
    expected = {
        "type": "IMAGE",
        "ownerId": old["ownerId"],
        "livePhotoVideoId": None,
        "deletedAt": None,
        "libraryId": None,
        "isExternal": False,
        "isOffline": False,
        "isEdited": False,
        "status": "active",
        "visibility": old["visibility"],
        "isFavorite": old["isFavorite"],
        "width": old["width"],
        "height": old["height"],
    }
    for k, v in expected.items():
        if a.get(k) != v:
            errors.append(k)
    try:
        if db_sha(a) != conversion["still_sha1"]:
            errors.append("checksum")
    except Stop:
        errors.append("checksum")
    if e.get("fileSizeInByte") != conversion["still_bytes"]:
        errors.append("fileSizeInByte")
    for k in DATE_FIELDS:
        try:
            if not equal_date(a.get(k), old.get(k)):
                errors.append(k)
        except Stop:
            errors.append(k)
    for k in ("dateTimeOriginal", "modifyDate"):
        try:
            if not equal_date(e.get(k), oe.get(k)):
                errors.append("exif." + k)
        except Stop:
            errors.append("exif." + k)
    try:
        when = instant(old["fileCreatedAt"])
        if timezone_offset(e.get("timeZone"), when) != timezone_offset(
            oe.get("timeZone"), when
        ):
            errors.append("timeZone")
    except Stop:
        errors.append("timeZone")
    for k in EXIF_FIELDS:
        left, right = e.get(k), oe.get(k)
        if k in ("latitude", "longitude") and left is not None and right is not None:
            if abs(float(left) - float(right)) > 1e-8:
                errors.append("exif." + k)
        elif left != right:
            errors.append("exif." + k)
    if e.get("livePhotoCID"):
        errors.append("livePhotoCID")
    if set(e.get("tags") or []) != set(oe.get("tags") or []):
        errors.append("exif.tags")
    return errors
