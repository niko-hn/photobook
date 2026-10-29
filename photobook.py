#!/usr/bin/env python3
"""
photobook.py - a single-file photo album web app.

Run it inside a folder of photos and it starts a local webserver that
presents the images as a flip-through book: a front cover page followed
by spreads showing two pages at a time, each page laid out with 1-3
photos in a varying, framed-photo style.

Usage:
    python3 photobook.py [--port 8000] [--host 127.0.0.1] [--open]

No third-party dependencies - standard library only.
"""

import argparse
import json
import math
import mimetypes
import os
import random
import shutil
import socket
import struct
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

STATE_FILENAME = "photobook_state.json"

LAYOUTS_BY_COUNT = {
    1: ["1a", "1b"],
    2: ["2a", "2b", "2c"],
    3: ["3a", "3b", "3c"],
}

# Page-relative percentage rects {x, y, w, h} per photo slot for each named
# arrangement. These are the one true source of page layout - baked into
# each photo's x/y/w/h at generation time (and again by "auto-relayout"),
# rather than referenced by name, so a page's layout is always just plain
# numbers that a future drag/resize editor can read and overwrite freely.
LAYOUT_RECTS = {
    "1a": [{"x": 11, "y": 11, "w": 78, "h": 78}],
    "1b": [{"x": 20, "y": 4, "w": 60, "h": 92}],
    "2a": [
        {"x": 15, "y": 4, "w": 70, "h": 44},
        {"x": 15, "y": 52, "w": 70, "h": 44},
    ],
    "2b": [
        {"x": 4, "y": 15, "w": 44, "h": 70},
        {"x": 52, "y": 15, "w": 44, "h": 70},
    ],
    "2c": [
        {"x": 8, "y": 4, "w": 55, "h": 48},
        {"x": 37, "y": 48, "w": 55, "h": 48},
    ],
    "3a": [
        {"x": 4, "y": 6, "w": 52, "h": 88},
        {"x": 58, "y": 6, "w": 38, "h": 40},
        {"x": 58, "y": 54, "w": 38, "h": 40},
    ],
    "3b": [
        {"x": 2, "y": 20, "w": 29, "h": 60},
        {"x": 35.5, "y": 20, "w": 29, "h": 60},
        {"x": 69, "y": 20, "w": 29, "h": 60},
    ],
    "3c": [
        {"x": 20, "y": 5, "w": 60, "h": 42},
        {"x": 4, "y": 53, "w": 44, "h": 42},
        {"x": 52, "y": 53, "w": 44, "h": 42},
    ],
}


def find_photos(folder: Path):
    photos = []
    for entry in sorted(folder.iterdir()):
        if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS:
            photos.append(entry)
    return photos


JPEG_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_exif_orientation(app1):
    """EXIF orientation (1-8) from a JPEG APP1 segment's payload: None if
    the segment isn't EXIF at all (phones often add a second APP1 for XMP,
    which mustn't reset what the EXIF one said), 1 if it has no tag."""
    if not app1.startswith(b"Exif\x00\x00"):
        return None
    tiff = app1[6:]
    endian = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if endian is None or len(tiff) < 8:
        return 1
    ifd = struct.unpack(endian + "I", tiff[4:8])[0]
    if ifd + 2 > len(tiff):
        return 1
    count = struct.unpack(endian + "H", tiff[ifd:ifd + 2])[0]
    for i in range(count):
        entry = tiff[ifd + 2 + i * 12: ifd + 14 + i * 12]
        if len(entry) == 12 and struct.unpack(endian + "H", entry[:2])[0] == 0x0112:
            return struct.unpack(endian + "H", entry[8:10])[0]
    return 1


def _jpeg_size(f):
    width = height = None
    orientation = 1
    while True:
        byte = f.read(1)
        if not byte:
            break
        if byte != b"\xff":
            continue
        marker = f.read(1)
        while marker == b"\xff":  # fill bytes
            marker = f.read(1)
        if not marker or marker[0] in (0xD8, 0x01) or 0xD0 <= marker[0] <= 0xD7:
            continue  # standalone markers carry no length
        if marker[0] == 0xD9:
            break
        length_bytes = f.read(2)
        if len(length_bytes) < 2:
            break
        length = struct.unpack(">H", length_bytes)[0]
        if marker[0] == 0xE1:
            orientation = _jpeg_exif_orientation(f.read(length - 2)) or orientation
        elif marker[0] in JPEG_SOF_MARKERS:
            height, width = struct.unpack(">xHH", f.read(5))
            break
        else:
            f.seek(length - 2, os.SEEK_CUR)
    if width and orientation in (5, 6, 7, 8):  # rotated 90/270 degrees on display
        width, height = height, width
    return width, height


def image_aspect(path: Path):
    """Width / height of an image as browsers display it (EXIF rotation
    applied for JPEGs), read from the file header alone - or None if the
    format isn't recognized. Standard library only, so no Pillow."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            width = height = None
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                width, height = _jpeg_size(f)
            elif head[:8] == b"\x89PNG\r\n\x1a\n":
                width, height = struct.unpack(">II", head[16:24])
            elif head[:4] == b"GIF8":
                width, height = struct.unpack("<HH", head[6:10])
            elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                chunk = head[12:16]
                if chunk == b"VP8 ":
                    w, h = struct.unpack("<HH", head[26:30])
                    width, height = w & 0x3FFF, h & 0x3FFF
                elif chunk == b"VP8L":
                    bits = struct.unpack("<I", head[21:25])[0]
                    width, height = 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
                elif chunk == b"VP8X":
                    width = 1 + int.from_bytes(head[24:27], "little")
                    height = 1 + int.from_bytes(head[27:30], "little")
            elif head[:2] == b"BM":
                width, height = struct.unpack("<ii", head[18:26])
                height = abs(height)
    except (OSError, struct.error):
        return None
    if not width or not height:
        return None
    return round(width / height, 4)


def add_aspect_ratios(folder: Path, album: dict):
    """Give every page photo an 'ar' (width / height) so the client can
    fit frames to the photo's real shape instead of cropping it. Computed
    fresh on every load rather than stored in the saved state, so it can
    never go stale."""
    for page in album.get("pages", []):
        for photo in page.get("photos", []):
            ar = image_aspect(folder / photo["src"])
            if ar:
                photo["ar"] = ar
    return album


def grid_rects(n):
    """Fallback layout for photo counts the curated LAYOUT_RECTS templates
    don't cover (0, or more than 3 - which "move" can produce by piling
    photos onto one page): a plain, evenly-spaced grid."""
    if n <= 0:
        return []
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    gap = 4
    cell_w = (100 - gap * (cols - 1)) / cols
    cell_h = (100 - gap * (rows - 1)) / rows
    inset = min(cell_w, cell_h) * 0.08

    rects = []
    for i in range(n):
        r, c = divmod(i, cols)
        items_in_row = min(cols, n - r * cols)
        row_offset = (cols - items_in_row) * (cell_w + gap) / 2
        rects.append({
            "x": round(row_offset + c * (cell_w + gap) + inset, 2),
            "y": round(r * (cell_h + gap) + inset, 2),
            "w": round(cell_w - inset * 2, 2),
            "h": round(cell_h - inset * 2, 2),
        })
    return rects


def rects_for_count(count, rng):
    if count <= 0:
        return []
    if count in LAYOUTS_BY_COUNT:
        layout = rng.choice(LAYOUTS_BY_COUNT[count])
        return LAYOUT_RECTS[layout]
    return grid_rects(count)


def lay_out_photo_group(group, rng):
    """Turn a list of Paths into a page's photo list, each with a freshly
    picked position (x/y/w/h) and rotation."""
    rects = rects_for_count(len(group), rng)
    return [
        {
            "src": p.name,
            "x": rect["x"], "y": rect["y"], "w": rect["w"], "h": rect["h"],
            "rot": round(rng.uniform(-4.5, 4.5), 1),
        }
        for p, rect in zip(group, rects)
    ]


def build_pages(photos, rng):
    remaining = photos[:]
    rng.shuffle(remaining)

    pages = []
    idx = 0
    while idx < len(remaining):
        left = len(remaining) - idx
        choices = [n for n in (1, 2, 3) if n <= left]
        size = rng.choice(choices)
        group = remaining[idx: idx + size]
        idx += size
        pages.append({"photos": lay_out_photo_group(group, rng), "headline": ""})
    return pages


def build_album(folder: Path):
    photos = find_photos(folder)
    rng = random.Random()

    cover_choices = photos[:]
    rng.shuffle(cover_choices)
    cover = [p.name for p in cover_choices[:5]]

    return {
        "folder": folder.name or str(folder),
        "count": len(photos),
        "cover": cover,
        "pages": build_pages(photos, rng),
        "version": 0,
    }


def load_state(folder: Path):
    """Load a previously-saved album from STATE_FILENAME, if present and
    valid. Returns None if there's nothing usable to load."""
    state_path = folder / STATE_FILENAME
    if not state_path.is_file():
        return None
    try:
        album = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(album, dict) or not isinstance(album.get("pages"), list):
        return None
    album.setdefault("version", 0)
    return album


def reconcile_new_photos(folder: Path, album: dict):
    """Append any photo files present in the folder but not referenced
    anywhere in the (possibly saved/edited) album, as new auto-laid-out
    pages, so nothing added to the folder later gets silently dropped."""
    known = set(album.get("cover", []))
    for page in album.get("pages", []):
        for photo in page.get("photos", []):
            known.add(photo.get("src"))

    all_photos = find_photos(folder)
    new_photos = [p for p in all_photos if p.name not in known]

    if new_photos:
        rng = random.Random()
        album = dict(album)
        album["pages"] = list(album.get("pages", [])) + build_pages(new_photos, rng)

    album["count"] = len(all_photos)
    return album


def get_album(folder: Path):
    """The album to serve/export for a folder: a saved+edited state if one
    exists (reconciled against any photos added since), else a fresh
    random one."""
    album = load_state(folder)
    if album is None:
        album = build_album(folder)
    else:
        album = reconcile_new_photos(folder, album)
    return add_aspect_ratios(folder, album)


HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Photobook</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 32 32%22><text y=%2224%22 font-size=%2226%22>&#128248;</text></svg>">
<style>
  :root{
    --paper:#f4ecdd;
    --paper-dark:#ece1cc;
    --ink:#3a2f27;
    --ink-soft:#7a6a5a;
    --spine:#00000022;
    --accent:#b5652f;
  }
  *{box-sizing:border-box;}
  html,body{height:100%;}
  body{
    margin:0;
    font-family:"Iowan Old Style","Palatino Linotype",Georgia,"Times New Roman",serif;
    background:radial-gradient(ellipse at center, #2b241d 0%, #17130f 100%);
    color:var(--ink);
    overflow:hidden;
    -webkit-user-select:none;
    user-select:none;
    touch-action:pan-y;
  }
  #app{
    position:relative;
    width:100%;
    height:100%;
  }
  #book{
    position:absolute;
    top:10px;
    left:30px;
    right:30px;
    bottom:25px;
    display:flex;
    border-radius:6px;
    box-shadow:0 30px 60px -15px #000000aa, 0 0 0 1px #00000033;
    overflow:hidden;
    perspective:2400px;
  }
  .page{
    position:relative;
    flex:1 1 50%;
    background:
      linear-gradient(90deg, #00000010, transparent 24px),
      var(--paper);
    padding:clamp(18px, 2.5vw, 40px);
    display:flex;
    flex-direction:column;
    align-items:stretch;
    justify-content:flex-start;
    overflow:hidden;
  }
  .page.page-right{
    background:
      linear-gradient(270deg, #00000010, transparent 24px),
      var(--paper);
  }
  .page.blank{
    background:var(--paper-dark);
  }
  #book::after{
    content:"";
    position:absolute;
    left:50%;
    top:0;
    bottom:0;
    width:26px;
    margin-left:-13px;
    background:linear-gradient(90deg, transparent, var(--spine) 45%, var(--spine) 55%, transparent);
    pointer-events:none;
    z-index:5;
  }

  /* ---- Single-page mode (portrait screens): one page at a time, as a
     centered 2:3 page instead of stretching to the window's full height ---- */
  body.single-page #book{
    left:10px;
    right:10px;
    width:min(calc(100vw - 20px), calc((100vh - 35px) * 2 / 3));
    height:min(calc(100vh - 35px), calc((100vw - 20px) * 3 / 2));
    width:min(calc(100vw - 20px), calc((100dvh - 35px) * 2 / 3));
    height:min(calc(100dvh - 35px), calc((100vw - 20px) * 3 / 2));
    margin:auto;
  }
  body.single-page #book::after{ display:none; }
  /* Swiping turns the page on a phone; the side arrows would just take
     width away from it. */
  body.single-page .nav-btn{ display:none; }

  /* ---- Cover ---- */
  .cover-wrap{
    display:flex;
    flex-direction:column;
    align-items:center;
    gap:28px;
    text-align:center;
    margin:auto 0;
  }
  .cover-title{
    font-size:clamp(22px,3.6vw,40px);
    letter-spacing:.03em;
    color:var(--ink);
    font-weight:600;
    text-transform:capitalize;
  }
  .cover-sub{
    margin-top:-16px;
    font-size:13px;
    letter-spacing:.12em;
    text-transform:uppercase;
    color:var(--ink-soft);
  }
  .cover-thumbs{
    position:relative;
    width:320px; /* the thumbs' fixed positions span ~320px; this centers them */
    max-width:100%;
    height:170px;
  }
  .cover-thumbs .frame{
    position:absolute;
    width:120px;
    height:120px;
  }
  .cover-hint{
    font-size:12px;
    color:var(--ink-soft);
    letter-spacing:.08em;
  }

  /* ---- Index (captions as a table of contents) ---- */
  .index-wrap{
    display:flex;
    flex-direction:column;
    min-height:0;
    width:100%;
    max-width:520px;
    margin:auto;
  }
  .index-title{
    text-align:center;
    font-size:13px;
    letter-spacing:.12em;
    text-transform:uppercase;
    color:var(--ink-soft);
    margin-bottom:18px;
  }
  .index-list{
    list-style:none;
    margin:0;
    padding:0;
    overflow-y:auto;
  }
  .index-entry{
    display:flex;
    align-items:baseline;
    gap:8px;
    width:100%;
    padding:7px 6px;
    background:none;
    border:none;
    border-radius:4px;
    font:inherit;
    font-size:clamp(14px, 1.5vw, 18px);
    color:var(--ink);
    text-align:left;
    cursor:pointer;
  }
  .index-entry:hover{ background:#0000000a; }
  .index-dots{
    flex:1 1 12px;
    border-bottom:1px dotted #00000045;
    transform:translateY(-4px);
  }
  .index-num{
    flex:none;
    color:var(--ink-soft);
    font-variant-numeric:tabular-nums;
  }
  /* The page counter, with a "contents" icon when there's an index to go
     back to - the whole thing is the (generously sized) tap target. */
  .index-btn{
    display:inline-flex;
    align-items:center;
    gap:7px;
    padding:5px 10px;
    background:none;
    border:none;
    color:inherit;
    font:inherit;
    letter-spacing:inherit;
    pointer-events:none;
  }
  .index-btn svg{ display:none; width:15px; height:15px; }
  .index-btn.to-index{ pointer-events:auto; cursor:pointer; }
  .index-btn.to-index svg{ display:block; }
  .index-btn.to-index:hover{ color:#f4ecdd; }

  /* ---- Photo frames ---- */
  .frame{
    background:#fff;
    padding:8px 8px 22px 8px;
    box-shadow:0 10px 22px -8px #00000066, 0 1px 0 #fff inset;
    border-radius:2px;
  }
  .frame img{
    display:block;
    width:100%;
    height:100%;
    object-fit:cover;
    object-position:center top;
    background:#ddd3c2;
    cursor:zoom-in;
  }

  /* ---- Lightbox ---- */
  #lightbox{
    position:fixed;
    inset:0;
    background:#000;
    opacity:0;
    pointer-events:none;
    transition:opacity .3s ease;
    z-index:100;
  }
  #lightbox{ touch-action:none; } /* pinch/pan handled in JS, not by the browser */
  body.single-page .lb-frame{ box-shadow:none; }
  #lightbox.open{
    opacity:1;
    pointer-events:auto;
  }
  .lb-frame{
    position:fixed;
    background:#fff;
    box-shadow:0 30px 80px -12px #000000cc;
    cursor:zoom-out;
    transition:
      top .38s cubic-bezier(.3,.7,.3,1),
      left .38s cubic-bezier(.3,.7,.3,1),
      width .38s cubic-bezier(.3,.7,.3,1),
      height .38s cubic-bezier(.3,.7,.3,1),
      padding .38s cubic-bezier(.3,.7,.3,1),
      transform .38s cubic-bezier(.3,.7,.3,1);
  }
  .lb-frame img{
    display:block;
    width:100%;
    height:100%;
    object-fit:cover;
    object-position:center top;
  }

  .page-canvas{
    position:relative;
    width:100%;
    flex:1 1 auto;
    min-height:0;
  }
  .page-canvas .frame{
    position:absolute;
  }
  /* Phone layout (single-page mode): each photo is centered in a cell,
     its frame sized to the photo's own shape (--ar = width/height) so
     nothing gets cropped. 16px/30px are the frame's padding. */
  .fit-cell{
    position:absolute;
    container-type:size;
    display:flex;
    align-items:center;
    justify-content:center;
  }
  .page-canvas .fit-cell .frame{
    position:relative;
    flex:none;
    box-sizing:content-box;
    width:min(calc(100cqw - 16px), calc((100cqh - 30px) * var(--ar)));
    height:calc(min(calc(100cqw - 16px), calc((100cqh - 30px) * var(--ar))) / var(--ar));
  }

  /* ---- Edit mode ---- */
  body.edit-mode .page:not(.blank)::after{
    content:"";
    position:absolute;
    inset:14px;
    border:1px dashed #00000030;
    pointer-events:none;
  }
  body.edit-mode #book{ cursor:pointer; }
  body.move-mode #book{ cursor:cell; }
  .relayout-btn{
    display:none;
    position:absolute;
    top:14px;
    width:30px;
    height:30px;
    background:#000;
    color:#fff;
    border:none;
    font:700 14px/30px inherit;
    text-align:center;
    padding:0;
    cursor:pointer;
    z-index:15;
    border-radius:3px;
    box-shadow:0 4px 10px -2px #00000066;
  }
  .relayout-btn:hover{ background:#222; }
  .relayout-btn.left{ left:14px; }
  .relayout-btn.right{ right:14px; }
  body.edit-mode .relayout-btn{ display:block; }
  /* The phone layout is computed from the photos' shapes, so there's
     nothing for "relayout" to change there. */
  body.edit-mode.single-page .relayout-btn{ display:none; }

  .move-btn{
    display:none;
    position:absolute;
    top:-9px;
    left:-9px;
    min-width:22px;
    height:22px;
    padding:0 5px;
    background:#000;
    color:#fff;
    border:2px solid #fff;
    font:700 12px/18px inherit;
    text-align:center;
    cursor:pointer;
    z-index:16;
    border-radius:11px;
    box-shadow:0 3px 8px -2px #00000066;
  }
  .move-btn:hover{ background:#222; }
  .move-btn.picked{ background:var(--accent); }
  body.edit-mode .move-btn{ display:block; }
  .frame.picked{ outline:3px solid var(--accent); outline-offset:2px; }

  /* A page with no photos still needs a visible, numbered drop target -
     otherwise it's just blank space with no clue it's clickable, and no
     badge for a keyboard move to land on. */
  .empty-slot{
    display:none;
    position:absolute;
    inset:8%;
    align-items:center;
    justify-content:center;
    border:2px dashed #00000030;
    border-radius:10px;
  }
  body.edit-mode .empty-slot{ display:flex; }
  .empty-slot .move-btn{
    position:static;
    width:34px;
    height:34px;
    min-width:34px;
    border-radius:50%;
    font-size:14px;
    line-height:30px;
  }

  .page-headline{
    flex:0 0 auto;
    text-align:center;
    font-weight:700;
    font-size:clamp(20px, 3.2vw, 38px);
    color:var(--ink);
    letter-spacing:-.01em;
    line-height:1.2;
    /* Never clipped: the whole caption shows and the photos below make
       room. Clipping to a fixed height cut off descenders (g, p, y) in
       fonts with tall metrics, like Android's serif fallback. */
    margin-bottom:14px;
    pointer-events:none;
  }
  .headline-btn{
    display:none;
    position:absolute;
    top:14px;
    left:50%;
    transform:translateX(-50%);
    width:30px;
    height:30px;
    background:#000;
    color:#fff;
    border:none;
    font:700 13px/30px inherit;
    text-align:center;
    padding:0;
    cursor:pointer;
    z-index:15;
    border-radius:3px;
    box-shadow:0 4px 10px -2px #00000066;
  }
  .headline-btn:hover{ background:#222; }
  body.edit-mode .headline-btn{ display:block; }

  /* ---- Headline editor ---- */
  #headlineModal{
    position:fixed;
    inset:0;
    background:#00000066;
    backdrop-filter:blur(2px);
    display:flex;
    align-items:center;
    justify-content:center;
    opacity:0;
    pointer-events:none;
    transition:opacity .2s ease;
    z-index:200;
  }
  #headlineModal.open{
    opacity:1;
    pointer-events:auto;
  }
  .headline-card{
    background:var(--paper);
    border-radius:10px;
    box-shadow:0 30px 80px -20px #000000cc, 0 0 0 1px #00000022;
    padding:26px 26px 18px;
    width:min(90vw, 420px);
    transform:translateY(10px) scale(.97);
    transition:transform .2s ease;
  }
  #headlineModal.open .headline-card{
    transform:translateY(0) scale(1);
  }
  .headline-card label{
    display:block;
    font-size:11px;
    letter-spacing:.1em;
    text-transform:uppercase;
    color:var(--ink-soft);
    margin-bottom:10px;
  }
  .headline-card input{
    display:block;
    width:100%;
    box-sizing:border-box;
    font:italic 20px/1.4 "Iowan Old Style","Palatino Linotype",Georgia,serif;
    color:var(--ink);
    background:transparent;
    border:none;
    border-bottom:2px solid #00000022;
    padding:4px 2px 10px;
    outline:none;
  }
  .headline-card input:focus{ border-bottom-color:var(--accent); }
  .headline-actions{
    display:flex;
    justify-content:space-between;
    align-items:center;
    margin-top:22px;
  }
  .headline-actions-right{ display:flex; gap:8px; }
  .headline-card button{
    font:600 12px/1 "Iowan Old Style","Palatino Linotype",Georgia,serif;
    letter-spacing:.03em;
    padding:9px 16px;
    border-radius:6px;
    border:none;
    cursor:pointer;
    background:none;
  }
  #headlineClear{ color:var(--ink-soft); }
  #headlineClear:hover{ color:var(--ink); }
  #headlineCancel{ color:var(--ink-soft); }
  #headlineCancel:hover{ background:#00000010; }
  #headlineSave{ background:var(--ink); color:#fff; }
  #headlineSave:hover{ background:#221a15; }

  #toast{
    position:absolute;
    bottom:52px;
    left:50%;
    transform:translateX(-50%) translateY(8px);
    background:#000000cc;
    color:#f4ecdd;
    font-size:12px;
    letter-spacing:.04em;
    padding:8px 16px;
    border-radius:5px;
    opacity:0;
    pointer-events:none;
    transition:opacity .25s, transform .25s;
    z-index:30;
  }
  #toast.show{
    opacity:1;
    transform:translateX(-50%) translateY(0);
  }

  /* ---- Page flip ---- */
  .flip-overlay{
    position:absolute;
    top:0;
    bottom:0;
    width:50%;
    transform-style:preserve-3d;
    pointer-events:none;
    z-index:20;
  }
  .flip-overlay.right{ left:50%; transform-origin:left center; }
  .flip-overlay.left{ left:0; transform-origin:right center; }
  .flip-overlay.single{ left:0; width:100%; transform-origin:left center; }
  .flip-face{
    position:absolute;
    inset:0;
    background:var(--paper);
    padding:clamp(18px, 2.5vw, 40px);
    display:flex;
    flex-direction:column;
    align-items:stretch;
    justify-content:flex-start;
    overflow:hidden;
    backface-visibility:hidden;
    box-shadow:0 0 40px -4px #00000066;
  }
  .flip-face.blank{ background:var(--paper-dark); }
  .flip-overlay.dir-fwd .flip-face.back{ transform:rotateY(180deg); }
  .flip-overlay.dir-back .flip-face.back{ transform:rotateY(-180deg); }
  .flip-shade{
    position:absolute;
    inset:0;
    background:linear-gradient(90deg, #00000000, #00000060);
    opacity:0;
    pointer-events:none;
  }
  .flip-overlay.left .flip-shade{
    background:linear-gradient(270deg, #00000000, #00000060);
  }

  /* ---- Nav ---- */
  .nav-btn{
    position:absolute;
    top:10px;
    bottom:25px;
    width:30px;
    border:none;
    background:transparent;
    color:#f4ecdd99;
    font-size:18px;
    cursor:pointer;
    display:flex;
    align-items:center;
    justify-content:center;
    z-index:10;
    transition:color .15s, background .15s;
  }
  .nav-btn:hover{ color:#f4ecdd; background:#ffffff14; }
  .nav-btn:disabled{ opacity:0; pointer-events:none; }
  #prevBtn{ left:0; }
  #nextBtn{ right:0; }

  #progress{
    position:absolute;
    left:30px;
    right:30px;
    bottom:0;
    height:25px;
    display:flex;
    align-items:center;
    justify-content:center;
    gap:10px;
    font-size:11px;
    color:#f4ecdd99;
    letter-spacing:.1em;
  }

  .spread-btn{
    display:none;
    width:20px;
    height:20px;
    padding:0;
    background:#f4ecdd22;
    color:#f4ecdd;
    border:none;
    font:700 11px/20px inherit;
    text-align:center;
    cursor:pointer;
    border-radius:3px;
  }
  .spread-btn:hover{ background:#f4ecdd3a; }
  .spread-btn:disabled{ opacity:.3; cursor:default; pointer-events:none; }
  body.edit-mode .spread-btn{ display:block; }

  #empty{
    color:#f4ecdd;
    text-align:center;
    font-size:16px;
    line-height:1.6;
  }

  .fade-enter{ animation:pageIn .32s ease; }
  @keyframes pageIn{
    from{ opacity:0; transform:scale(.985); }
    to{ opacity:1; transform:scale(1); }
  }
</style>
</head>
<body>
<div id="app">
  <button id="prevBtn" class="nav-btn" aria-label="Previous">&#8249;</button>
  <div id="book"></div>
  <button id="nextBtn" class="nav-btn" aria-label="Next">&#8250;</button>
  <div id="progress">
    <button id="addSpreadBtn" class="spread-btn" title="Add a new spread after this one (A)">A</button>
    <button id="indexBtn" class="index-btn" title="Contents" aria-label="Contents">
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4h12M2 8h12M2 12h12" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" fill="none"/></svg>
      <span id="progressText"></span>
    </button>
    <button id="deleteSpreadBtn" class="spread-btn" title="Delete this spread, merging its photos into the next page (D)">D</button>
  </div>
  <div id="toast"></div>
</div>

<script>
let DATA = null;
let spread = 0; // 0 = cover, 1..N = page pairs (single pages in single-page mode)
let totalSpreads = 1;
let animating = false;

// Portrait screens (phones held upright) show one page at a time instead
// of a squashed two-page spread. Everything that maps between spreads and
// pages goes through the helpers below, so both modes share one code path.
const singlePageQuery = window.matchMedia('(max-aspect-ratio: 4/5)');
let singlePage = singlePageQuery.matches;
let modeChangePending = false;

// An album with captions gets an index (table of contents) of them: on the
// cover spread's otherwise blank left page in spreads - no extra spread -
// and as its own page right after the cover in single-page mode, which
// shifts every later page along by one (indexOffset).
function hasIndex(){ return DATA.pages.some(p => p.headline); }
function indexOffset(){ return singlePage && hasIndex() ? 1 : 0; }
function indexSpread(){ return singlePage ? 1 : 0; }

function pagesPerSpread(){ return singlePage ? 1 : 2; }
// Negative for the cover and the index page - no album page there.
function firstPageOf(spreadIdx){ return (spreadIdx - 1 - indexOffset()) * pagesPerSpread(); }
function spreadOfPage(pageIdx){ return Math.floor(pageIdx / pagesPerSpread()) + 1 + indexOffset(); }
function countSpreads(){ return 1 + indexOffset() + Math.ceil(DATA.pages.length / pagesPerSpread()); }

const FLIP_MS = 620;

const bookEl = document.getElementById('book');
const prevBtn = document.getElementById('prevBtn');
const nextBtn = document.getElementById('nextBtn');
const progressEl = document.getElementById('progressText');
const indexBtn = document.getElementById('indexBtn');
const addSpreadBtn = document.getElementById('addSpreadBtn');
const deleteSpreadBtn = document.getElementById('deleteSpreadBtn');

const PHOTO_PREFIX = '__PHOTO_PREFIX__';
// Read-only (--read-only, and every static export): edit mode never turns
// on, so browsing can't change or save anything.
const READ_ONLY = __READ_ONLY__;

// A save can rename a file so the same name ends up pointing at different
// bytes (e.g. two photos swapping places). The browser caches /photo/<name>
// by URL, so without a cache-busting query string a stale cached image can
// keep showing under a reused name after reordering - looking exactly like
// a photo-ordering bug even though the file and saved order are correct.
// DATA.version changes on every save, so appending it here forces a fresh
// fetch whenever anything could have moved.
function photoUrl(src){
  return `${PHOTO_PREFIX}${encodeURIComponent(src)}?v=${DATA.version || 0}`;
}

// Mirrors LAYOUTS_BY_COUNT / LAYOUT_RECTS in photobook.py - kept identical
// so "auto-relayout" produces the same kind of arrangements as a fresh
// album, without needing a round-trip to the server.
const LAYOUTS_BY_COUNT = {
  1: ['1a', '1b'],
  2: ['2a', '2b', '2c'],
  3: ['3a', '3b', '3c'],
};
const LAYOUT_RECTS = {
  '1a': [{x: 11, y: 11, w: 78, h: 78}],
  '1b': [{x: 20, y: 4, w: 60, h: 92}],
  '2a': [{x: 15, y: 4, w: 70, h: 44}, {x: 15, y: 52, w: 70, h: 44}],
  '2b': [{x: 4, y: 15, w: 44, h: 70}, {x: 52, y: 15, w: 44, h: 70}],
  '2c': [{x: 8, y: 4, w: 55, h: 48}, {x: 37, y: 48, w: 55, h: 48}],
  '3a': [{x: 4, y: 6, w: 52, h: 88}, {x: 58, y: 6, w: 38, h: 40}, {x: 58, y: 54, w: 38, h: 40}],
  '3b': [{x: 2, y: 20, w: 29, h: 60}, {x: 35.5, y: 20, w: 29, h: 60}, {x: 69, y: 20, w: 29, h: 60}],
  '3c': [{x: 20, y: 5, w: 60, h: 42}, {x: 4, y: 53, w: 44, h: 42}, {x: 52, y: 53, w: 44, h: 42}],
};

// Fallback for counts the curated templates above don't cover (0, or more
// than 3 - which "move" can produce by piling photos onto one page).
function gridRects(n){
  if (n <= 0) return [];
  const cols = Math.ceil(Math.sqrt(n));
  const rows = Math.ceil(n / cols);
  const gap = 4;
  const cellW = (100 - gap * (cols - 1)) / cols;
  const cellH = (100 - gap * (rows - 1)) / rows;
  const inset = Math.min(cellW, cellH) * 0.08;

  const rects = [];
  for (let i = 0; i < n; i++){
    const r = Math.floor(i / cols), c = i % cols;
    const itemsInRow = Math.min(cols, n - r * cols);
    const rowOffset = (cols - itemsInRow) * (cellW + gap) / 2;
    rects.push({
      x: rowOffset + c * (cellW + gap) + inset,
      y: r * (cellH + gap) + inset,
      w: cellW - inset * 2,
      h: cellH - inset * 2,
    });
  }
  return rects;
}

// With a 'cell' (phone layout), the frame is centered in that cell and
// sized to the photo's shape by CSS (.fit-cell); without one, the frame
// fills the photo's stored x/y/w/h and the image is cropped to fit it.
function frameHtml(photo, num, pageIdx, photoIdx, cell){
  const rot = photo.rot || 0;
  const pos = `left:${photo.x}%; top:${photo.y}%; width:${photo.w}%; height:${photo.h}%;`;
  const picked = moveMode && movingPhoto && movingPhoto.photo === photo;
  const numBtn = (num != null)
    ? `<button class="move-btn${picked ? ' picked' : ''}" data-page-idx="${pageIdx}" data-photo-idx="${photoIdx}" title="${picked ? 'Cancel move' : 'Move this picture'}">${num}</button>`
    : '';
  if (cell){
    return `<div class="fit-cell" style="left:${cell.x}%; top:${cell.y}%; width:${cell.w}%; height:${cell.h}%;">
              <div class="frame${picked ? ' picked' : ''}" data-rot="${rot}" style="--ar:${photo.ar}; transform:rotate(${rot}deg)">
                <img src="${photoUrl(photo.src)}" alt="">
                ${numBtn}
              </div>
            </div>`;
  }
  return `<div class="frame${picked ? ' picked' : ''}" data-rot="${rot}" style="${pos} transform:rotate(${rot}deg)">
            <img src="${photoUrl(photo.src)}" alt="">
            ${numBtn}
          </div>`;
}

function coverInner(){
  const thumbs = DATA.cover.map((src, i) => {
    const positions = [
      {top:'10px', left:'10px', rot:-8},
      {top:'0px', left:'110px', rot:5},
      {top:'50px', left:'55px', rot:-2},
      {top:'20px', left:'200px', rot:9},
      {top:'70px', left:'160px', rot:-6},
    ];
    const p = positions[i % positions.length];
    return `<div class="frame" data-rot="${p.rot}" style="position:absolute; top:${p.top}; left:${p.left}; width:110px; height:110px; transform:rotate(${p.rot}deg); z-index:${i}">
              <img src="${photoUrl(src)}" alt="">
            </div>`;
  }).join('');

  return `
    <div class="cover-wrap">
      <div class="cover-sub">Photobook</div>
      <div class="cover-title">${escapeHtml(DATA.folder)}</div>
      <div class="cover-thumbs">${thumbs}</div>
      <div class="cover-hint">${DATA.count} photo${DATA.count === 1 ? '' : 's'} &middot; swipe or press &#8594; to open</div>
    </div>
  `;
}

function albumPageInner(page, startNum, pageIdx){
  if (page.photos.length === 0){
    return `<div class="page-canvas">${emptySlotHtml(startNum, pageIdx)}</div>`;
  }
  const cells = singlePage ? phoneCells(page) : null;
  const frames = page.photos.map((photo, i) => frameHtml(photo, startNum + i, pageIdx, i, cells && cells[i])).join('');
  return `<div class="page-canvas">${frames}</div>`;
}

// ---- Phone layout: the stored layouts are designed for a two-page
// spread, and on a tall, narrow phone page they turn landscape photos into
// cropped strips. In single-page mode a page's arrangement is instead
// chosen from the photos' actual shapes (photo.ar, width / height): try
// rows, columns and the mixed 3-photo arrangements, and keep the one that
// shows the photos largest and most evenly sized. Deterministic, so a page
// looks the same every time it's drawn. ----

const PHONE_GAP = 0.035;                       // between cells, as a fraction of page height
const PHONE_SPLITS = [0.38, 0.44, 0.5, 0.56, 0.62];

// Area a photo of aspect 'ar' covers when fitted, uncropped, into a cell.
function fittedArea(cellW, cellH, ar){
  const w = Math.min(cellW, cellH * ar);
  return w * (w / ar);
}

// Candidate arrangements for n photos on a canvas 'aspect' wide and 1
// tall, each a list of {x, y, w, h} cells in reading order.
function phoneCandidates(n, aspect){
  const g = PHONE_GAP, C = aspect, out = [];
  const R = (x, y, w, h) => ({x, y, w, h});
  if (n === 1) out.push([R(0, 0, C, 1)]);
  if (n === 2){
    for (const s of PHONE_SPLITS){
      const a = (1 - g) * s, b = (C - g) * s;
      out.push([R(0, 0, C, a), R(0, a + g, C, 1 - a - g)]);   // stacked
      out.push([R(0, 0, b, 1), R(b + g, 0, C - b - g, 1)]);   // side by side
    }
  }
  if (n === 3){
    const h3 = (1 - 2 * g) / 3, w3 = (C - 2 * g) / 3;
    out.push([R(0, 0, C, h3), R(0, h3 + g, C, h3), R(0, 2 * (h3 + g), C, h3)]);
    out.push([R(0, 0, w3, 1), R(w3 + g, 0, w3, 1), R(2 * (w3 + g), 0, w3, 1)]);
    const halfW = (C - g) / 2, halfH = (1 - g) / 2;
    for (const s of PHONE_SPLITS){
      const a = (1 - g) * s, b = (C - g) * s;
      out.push([R(0, 0, C, a), R(0, a + g, halfW, 1 - a - g), R(halfW + g, a + g, halfW, 1 - a - g)]);
      out.push([R(0, 0, halfW, a), R(halfW + g, 0, halfW, a), R(0, a + g, C, 1 - a - g)]);
      out.push([R(0, 0, b, 1), R(b + g, 0, C - b - g, halfH), R(b + g, halfH + g, C - b - g, halfH)]);
      out.push([R(0, 0, b, halfH), R(0, halfH + g, b, halfH), R(b + g, 0, C - b - g, 1)]);
    }
  }
  return out;
}

// Cells (in % of the page canvas) for each of a page's photos, or null to
// fall back to the stored layout (more than 3 photos, or a photo whose
// shape isn't known).
function phoneCells(page){
  const ars = page.photos.map(p => p.ar);
  if (!ars.length || ars.length > 3 || ars.some(ar => !ar)) return null;
  // Rough width/height of a single page's photo area, which a headline
  // makes a little shorter. Only steers the choice - the frames themselves
  // are fitted to the real cell size by CSS.
  const aspect = page.headline ? 0.72 : 0.64;
  let best = null, bestScore = -1;
  for (const cells of phoneCandidates(ars.length, aspect)){
    const areas = cells.map((c, i) => fittedArea(c.w, c.h, ars[i]));
    const total = areas.reduce((a, b) => a + b, 0);
    const score = total * Math.pow(Math.min(...areas) / Math.max(...areas), 0.25);
    if (score > bestScore){ bestScore = score; best = cells; }
  }
  return best.map(c => ({x: c.x / aspect * 100, y: c.y * 100, w: c.w / aspect * 100, h: c.h * 100}));
}

// A visible "drop it here" target for a page with no photos: a dashed box
// carrying the same kind of numbered badge every photo gets, so an empty
// page is both an obvious mouse target and reachable by digit key, same as
// a real photo would be - not just an invisible click on blank background.
function emptySlotHtml(num, pageIdx){
  return `<div class="empty-slot">
            <button class="move-btn" data-page-idx="${pageIdx}" data-photo-idx="${EMPTY_SLOT}" title="Drop picture here">${num}</button>
          </div>`;
}

// Returns the content ('cls' + 'inner' html + 'pageIdx') that belongs in a
// given left/right slot of a given spread, independent of what's currently
// rendered - used both for the static book and for flip-animation faces.
// pageIdx is null for the cover and blank slots (nothing to relayout there).
// Photo numbering is local to the spread: the left page starts at 1, and
// the right page continues on from wherever the left page left off - so
// every visible badge is a small, unambiguous number a keypress can target.
// In single-page mode the one visible page is the 'right' slot and the
// 'left' slot is always empty.
function slotAt(spreadIdx, side){
  const blank = {cls: 'blank', inner: '', pageIdx: null};
  if (singlePage && side === 'left') return blank;
  if (spreadIdx === 0 && side === 'right') return {cls: '', inner: coverInner(), pageIdx: null};
  if (spreadIdx === indexSpread() && (singlePage || side === 'left')){
    return hasIndex() ? {cls: '', inner: indexInner(), pageIdx: null} : blank;
  }
  const leftPageIdx = firstPageOf(spreadIdx);
  const pageIdx = (side === 'left' || singlePage) ? leftPageIdx : leftPageIdx + 1;
  const page = DATA.pages[pageIdx];
  if (!page) return {cls: 'blank', inner: '', pageIdx: null};

  let startNum = 1;
  if (side === 'right' && !singlePage){
    const leftPage = DATA.pages[leftPageIdx];
    // An empty page still claims one badge number (its "drop here"
    // placeholder), so every page - even an empty one - has a number a
    // keypress can reach, the same way move mode already targets one.
    startNum = 1 + (leftPage ? Math.max(1, leftPage.photos.length) : 0);
  }
  // The headline lives inside 'inner' (not appended separately by pageHtml)
  // so it's included wherever this slot's content ends up rendered -
  // including the flip-face overlay mid-animation, not just the static page.
  const headlineHtml = page.headline
    ? `<div class="page-headline">${escapeHtml(page.headline)}</div>`
    : '';
  const inner = headlineHtml + albumPageInner(page, startNum, pageIdx);
  return {cls: '', inner, pageIdx};
}

// The index: every captioned page, with its page number. Each entry is a
// button that jumps straight to that page (see jumpToPage).
function indexInner(){
  const entries = DATA.pages.map((page, pageIdx) => page.headline
    ? `<li><button class="index-entry" data-page-idx="${pageIdx}">
         <span class="index-caption">${escapeHtml(page.headline)}</span>
         <span class="index-dots"></span>
         <span class="index-num">${pageIdx + 1}</span>
       </button></li>`
    : '').join('');
  return `<div class="index-wrap">
            <div class="index-title">Contents</div>
            <ol class="index-list">${entries}</ol>
          </div>`;
}

// Quick navigation (index entries): straight to a page, with a short fade
// instead of flipping through everything in between.
function jumpToPage(pageIdx){
  if (animating || !DATA.pages[pageIdx]) return;
  spread = spreadOfPage(pageIdx);
  renderSpreadInstant();
  fadeInBook();
}

function pageHtml(slot, side){
  const letter = side === 'left' ? 'L' : 'R';
  const relayoutBtn = slot.pageIdx !== null
    ? `<button class="relayout-btn ${side}" data-page-idx="${slot.pageIdx}" title="Auto-relayout this page">${letter}</button>`
    : '';
  const headlineLetter = side === 'left' ? 'M' : 'N';
  const headlineBtn = slot.pageIdx !== null
    ? `<button class="headline-btn" data-page-idx="${slot.pageIdx}" title="Edit this page's caption">${headlineLetter}</button>`
    : '';
  const sideCls = side === 'left' ? 'page-left' : 'page-right';
  const pageIdxAttr = slot.pageIdx !== null ? ` data-page-idx="${slot.pageIdx}"` : '';
  // slot.inner already carries the headline (see slotAt) ahead of the photo
  // canvas, so it renders correctly here AND in the flip-face overlay.
  return `<div class="page ${sideCls} ${slot.cls}"${pageIdxAttr}>${slot.inner}${relayoutBtn}${headlineBtn}</div>`;
}

function fullSpreadHTML(spreadIdx){
  if (singlePage) return pageHtml(slotAt(spreadIdx, 'right'), 'right');
  const l = slotAt(spreadIdx, 'left');
  const r = slotAt(spreadIdx, 'right');
  return pageHtml(l, 'left') + pageHtml(r, 'right');
}

function renderSpreadInstant(){
  bookEl.innerHTML = fullSpreadHTML(spread);
  updateChrome();
}

function updateChrome(){
  const offset = indexOffset();
  prevBtn.disabled = animating || spread === 0;
  nextBtn.disabled = animating || spread === totalSpreads - 1;
  // Counts album spreads/pages only - the cover and index page show none.
  progressEl.textContent = spread - offset < 1 ? '' : `${spread - offset} / ${totalSpreads - 1 - offset}`;
  indexBtn.classList.toggle('to-index', hasIndex() && spread !== indexSpread());
  addSpreadBtn.disabled = animating;
  deleteSpreadBtn.disabled = animating || firstPageOf(spread) < 0;
  syncHistory();
}

// ---- Browser/phone back button: a full-screen photo goes back to its
// page, an album page back to the index (or the cover, without one), and
// from there back leaves the album as usual. History holds at most three
// entries of ours - 'start' (cover/index), 'page', 'photo' - so turning
// pages never piles up entries: the 'page' entry is just updated. Leaving
// a level on screen (closing the photo, the contents icon, paging back to
// the cover) pops its entry, so Back never does nothing. ----

let historyReady = false;  // not until the album is loaded and laid out
let expectPop = false;     // a history.back() we made ourselves: skip its popstate

function historyView(){ return history.state && history.state.view; }

function setHistory(method, state){
  try { history[method](state, ''); } catch (e) { /* e.g. a sandboxed frame */ }
}

function popOurEntry(){
  expectPop = true;
  history.back();
}

function syncHistory(){
  if (!historyReady || animating || lightboxOpen) return;
  const pageIdx = firstPageOf(spread);
  if (pageIdx >= 0){
    setHistory(historyView() === 'page' ? 'replaceState' : 'pushState', {view: 'page', pageIdx});
  } else if (historyView() === 'page'){
    popOurEntry();
  }
}

function fadeInBook(){
  bookEl.classList.remove('fade-enter');
  void bookEl.offsetWidth; // restart the animation
  bookEl.classList.add('fade-enter');
}

// The start of the album: the index if there is one, else the cover.
function showStart(){
  spread = hasIndex() ? indexSpread() : 0;
  renderSpreadInstant();
  fadeInBook();
}

function whenIdle(fn){
  if (animating) setTimeout(() => whenIdle(fn), 100);
  else fn();
}

window.addEventListener('popstate', (e) => {
  if (expectPop){ expectPop = false; return; }
  if (!DATA || !DATA.count) return;
  const view = e.state && e.state.view;
  if (lightboxOpen && view !== 'photo') closeLightbox();
  if (view === 'photo') return; // forward into a photo already closed: nothing to reopen
  const onPage = firstPageOf(spread) >= 0;
  if (view === 'page'){
    // Forward from the index: back to the page it was on. Back from a
    // photo: stay on whatever page swiping through photos reached.
    if (!onPage && DATA.pages[e.state.pageIdx]) whenIdle(() => jumpToPage(e.state.pageIdx));
    else syncHistory();
  } else if (onPage){
    whenIdle(showStart);
  }
});

function buildFlipOverlay(dirClass, sideClass, frontSlot, backSlot){
  const overlay = document.createElement('div');
  overlay.className = `flip-overlay ${sideClass} ${dirClass}`;
  overlay.innerHTML =
    `<div class="flip-face front ${frontSlot.cls}">${frontSlot.inner}</div>` +
    `<div class="flip-face back ${backSlot.cls}">${backSlot.inner}</div>` +
    `<div class="flip-shade"></div>`;
  return overlay;
}

function animateShade(overlay){
  const shade = overlay.querySelector('.flip-shade');
  shade.animate(
    [{opacity: 0}, {opacity: .55, offset: .5}, {opacity: 0}],
    {duration: FLIP_MS, easing: 'linear'}
  );
}

function finishFlip(){
  animating = false;
  if (modeChangePending){ applyPageMode(); return; }
  bookEl.innerHTML = fullSpreadHTML(spread); // replaces the overlay too, now that it's done
  updateChrome();
}

// The animation's 'finish' event can fire a moment before the browser has
// actually painted that final composited frame (more visible with real GPU
// compositing than in the testing here). Swapping the DOM synchronously in
// that callback risks a stale frame - the tail end of the turning page -
// still being what's on screen for an instant. Waiting two animation
// frames is a standard way to guarantee "wait until it's actually painted".
function afterPaint(callback){
  requestAnimationFrame(() => requestAnimationFrame(callback));
}

// Single-page flips turn the whole page around its left edge: forward
// turns the current page away to uncover the next one, backward brings
// the previous page back over the current one.
function flipSingle(nextSpread, forward){
  const turning = slotAt(forward ? spread : nextSpread, 'right');
  const under = slotAt(forward ? nextSpread : spread, 'right');

  bookEl.innerHTML = pageHtml(under, 'right');
  const overlay = buildFlipOverlay('dir-fwd', 'single', turning, {cls: 'blank', inner: ''});
  bookEl.appendChild(overlay);

  const frames = [{transform: 'rotateY(0deg)'}, {transform: 'rotateY(-180deg)'}];
  const anim = overlay.animate(forward ? frames : frames.reverse(),
    {duration: FLIP_MS, easing: 'cubic-bezier(.45,.05,.35,1)', fill: 'forwards'});
  animateShade(overlay);
  anim.onfinish = () => { spread = nextSpread; afterPaint(finishFlip); };
}

function flipForward(nextSpread){
  animating = true;
  updateChrome();
  if (singlePage){ flipSingle(nextSpread, true); return; }

  const frontSlot = slotAt(spread, 'right');         // page currently showing, about to turn
  const backSlot = slotAt(nextSpread, 'left');        // revealed as its back, once fully turned
  const revealedRight = slotAt(nextSpread, 'right');  // sits underneath, revealed as the turning page uncovers it
  const staysLeft = slotAt(spread, 'left');           // untouched until the flip actually gets there

  bookEl.innerHTML = pageHtml(staysLeft, 'left') + pageHtml(revealedRight, 'right');

  const overlay = buildFlipOverlay('dir-fwd', 'right', frontSlot, backSlot);
  bookEl.appendChild(overlay);

  const anim = overlay.animate(
    [{transform: 'rotateY(0deg)'}, {transform: 'rotateY(-180deg)'}],
    {duration: FLIP_MS, easing: 'cubic-bezier(.45,.05,.35,1)', fill: 'forwards'}
  );
  animateShade(overlay);
  anim.onfinish = () => { spread = nextSpread; afterPaint(finishFlip); };
}

function flipBackward(nextSpread){
  animating = true;
  updateChrome();
  if (singlePage){ flipSingle(nextSpread, false); return; }

  const frontSlot = slotAt(spread, 'left');          // page currently showing, about to turn back
  const backSlot = slotAt(nextSpread, 'right');       // revealed as its back, once fully turned
  const revealedLeft = slotAt(nextSpread, 'left');    // sits underneath, revealed as the turning page uncovers it
  const staysRight = slotAt(spread, 'right');         // untouched until the flip actually gets there

  bookEl.innerHTML = pageHtml(revealedLeft, 'left') + pageHtml(staysRight, 'right');

  const overlay = buildFlipOverlay('dir-back', 'left', frontSlot, backSlot);
  bookEl.appendChild(overlay);

  const anim = overlay.animate(
    [{transform: 'rotateY(0deg)'}, {transform: 'rotateY(180deg)'}],
    {duration: FLIP_MS, easing: 'cubic-bezier(.45,.05,.35,1)', fill: 'forwards'}
  );
  animateShade(overlay);
  anim.onfinish = () => { spread = nextSpread; afterPaint(finishFlip); };
}

function go(delta){
  if (animating || lightboxOpen || delta === 0) return;
  const next = spread + delta;
  if (next < 0 || next > totalSpreads - 1) return;
  if (delta > 0) flipForward(next);
  else flipBackward(next);
}

// ---- Lightbox: shared-element zoom from a clicked frame to a full,
// black-backed view, and back again on a second click. ----

const lightbox = document.createElement('div');
lightbox.id = 'lightbox';
const lightboxFrame = document.createElement('div');
lightboxFrame.id = 'lightboxFrame';
lightboxFrame.className = 'lb-frame'; // styled by class, so slide ghosts (clones) look the same
const lightboxImg = document.createElement('img');
lightboxImg.id = 'lightboxImg';
lightboxImg.alt = '';
lightboxFrame.appendChild(lightboxImg);
lightbox.appendChild(lightboxFrame);
document.body.appendChild(lightbox);

let lightboxOpen = false;
let lightboxSourceImg = null;
let lightboxSourceRot = 0;
let lightboxSourcePadding = '';
let lightboxPageIdx = null;   // null when opened from a cover thumbnail -
let lightboxPhotoIdx = null;  // there's no page sequence to navigate from there

// Same polaroid proportions as .frame (8px 8px 22px 8px), scaled up for
// a full-screen view instead of staying pinned at that fixed pixel size.
// On a portrait phone (single-page mode) the photo instead fills the
// screen edge to edge, with no frame.
function fitFrameRect(naturalW, naturalH){
  const vw = window.innerWidth, vh = window.innerHeight;
  if (singlePage){
    const ratio = Math.min(vw / naturalW, vh / naturalH);
    const w = naturalW * ratio, h = naturalH * ratio;
    return {top: (vh - h) / 2, left: (vw - w) / 2, width: w, height: h, padding: '0px'};
  }
  const padX = Math.min(34, Math.max(14, vw * 0.02));
  const padTop = padX;
  const padBottom = padX * 2.6;

  const availW = vw * 0.92 - padX * 2;
  const availH = vh * 0.92 - padTop - padBottom;
  const ratio = Math.min(availW / naturalW, availH / naturalH);
  const imgW = naturalW * ratio, imgH = naturalH * ratio;
  const frameW = imgW + padX * 2, frameH = imgH + padTop + padBottom;

  return {
    top: (vh - frameH) / 2,
    left: (vw - frameW) / 2,
    width: frameW,
    height: frameH,
    padding: `${padTop}px ${padX}px ${padBottom}px ${padX}px`,
  };
}

function openLightbox(imgEl, pageIdx, photoIdx){
  if (animating || lightboxOpen) return;
  lightboxOpen = true;
  lightboxPageIdx = pageIdx ?? null;
  lightboxPhotoIdx = photoIdx ?? null;
  lightboxSourceImg = imgEl;
  const frameEl = imgEl.closest('.frame');
  lightboxSourceRot = parseFloat(frameEl?.dataset.rot || '0');
  lightboxSourcePadding = frameEl ? getComputedStyle(frameEl).padding : '8px 8px 22px 8px';

  const r = (frameEl || imgEl).getBoundingClientRect();
  lightboxImg.onload = null; // see goToLightboxPhoto
  lightboxImg.src = imgEl.src;

  lightboxFrame.style.transition = 'none';
  lightboxFrame.style.top = r.top + 'px';
  lightboxFrame.style.left = r.left + 'px';
  lightboxFrame.style.width = r.width + 'px';
  lightboxFrame.style.height = r.height + 'px';
  lightboxFrame.style.padding = lightboxSourcePadding;
  lightboxFrame.style.transform = `rotate(${lightboxSourceRot}deg)`;

  lightbox.classList.add('open');
  setHistory('pushState', {view: 'photo', pageIdx: lightboxPageIdx});
  prevBtn.style.visibility = 'hidden';
  nextBtn.style.visibility = 'hidden';

  void lightboxFrame.offsetWidth; // force reflow so the next change transitions

  resetZoom(false);
  preloadNeighbors();
  const onLoad = () => {
    lightboxFrame.style.transition = '';
    fitLightboxFrame();
    lightboxFrame.style.transform = 'rotate(0deg)';
  };
  if (lightboxImg.complete) onLoad();
  else lightboxImg.onload = () => { lightboxImg.onload = null; onLoad(); };
}

// Sizes the open lightbox's frame to the current photo and screen.
function fitLightboxFrame(){
  const target = fitFrameRect(lightboxImg.naturalWidth, lightboxImg.naturalHeight);
  lightboxFrame.style.top = target.top + 'px';
  lightboxFrame.style.left = target.left + 'px';
  lightboxFrame.style.width = target.width + 'px';
  lightboxFrame.style.height = target.height + 'px';
  lightboxFrame.style.padding = target.padding;
}

function closeLightbox(){
  if (!lightboxOpen) return;
  lightboxOpen = false;
  // Closed on screen (tap, Escape) rather than by Back: drop its entry too.
  if (historyView() === 'photo') popOurEntry();
  resetZoom(false);
  endSlide();
  lightboxFrame.style.transition = ''; // zoom back out to the page with the usual animation

  const frameEl = lightboxSourceImg ? lightboxSourceImg.closest('.frame') : null;
  const r = (frameEl || lightboxSourceImg) ? (frameEl || lightboxSourceImg).getBoundingClientRect() : null;
  lightbox.classList.remove('open');
  prevBtn.style.visibility = '';
  nextBtn.style.visibility = '';
  lightboxPageIdx = null;
  lightboxPhotoIdx = null;

  if (r){
    lightboxFrame.style.top = r.top + 'px';
    lightboxFrame.style.left = r.left + 'px';
    lightboxFrame.style.width = r.width + 'px';
    lightboxFrame.style.height = r.height + 'px';
    lightboxFrame.style.padding = lightboxSourcePadding;
    lightboxFrame.style.transform = `rotate(${lightboxSourceRot}deg)`;
  }
}

// Every photo in the whole album, in reading order (page order, then
// photo order within each page) - what arrow keys walk through while the
// lightbox is open.
function flatPhotoList(){
  const list = [];
  DATA.pages.forEach((page, pageIdx) => {
    page.photos.forEach((_, photoIdx) => list.push({pageIdx, photoIdx}));
  });
  return list;
}

// The photo 'delta' steps away from the one in the lightbox, or null at
// either end of the album (no wraparound) or when it was opened from a
// cover thumbnail (no sequence to walk).
function neighborPhoto(delta){
  if (lightboxPageIdx === null) return null;
  const flat = flatPhotoList();
  const curIdx = flat.findIndex(p => p.pageIdx === lightboxPageIdx && p.photoIdx === lightboxPhotoIdx);
  if (curIdx === -1) return null;
  return flat[curIdx + delta] || null;
}

// Warms the browser cache with the photos either side, so a swipe can
// slide the next one in right away instead of waiting for it to load.
function preloadNeighbors(){
  for (const delta of [-1, 1]){
    const n = neighborPhoto(delta);
    if (n) new Image().src = photoUrl(DATA.pages[n.pageIdx].photos[n.photoIdx].src);
  }
}

// ---- Sliding between photos: the current one slides off to one side as
// the next slides in from the other. 'fromX' is where a swipe left the
// current photo when the finger lifted, so the slide carries on from
// there. The outgoing photo is a throwaway clone of the frame (a "ghost"),
// so the real frame can already take on the next photo. ----

const SLIDE_MS = 300;
let sliding = false;
let slideTimer = null;

function setFrameX(x, ms){
  lightboxFrame.style.transition = ms ? `transform ${ms}ms ease-out` : 'none';
  lightboxFrame.style.transform = `translateX(${x}px)`;
}

function endSlide(){
  clearTimeout(slideTimer);
  lightbox.querySelectorAll('.lb-ghost').forEach(g => g.remove());
  lightboxFrame.style.visibility = '';
  lightboxFrame.style.transition = '';
  lightboxFrame.style.transform = 'rotate(0deg)';
  sliding = false;
}

function shiftLightboxPhoto(delta, fromX = 0){
  const target = neighborPhoto(delta);
  if (!target || sliding){
    if (fromX) setFrameX(0, 200); // nowhere to go: spring back
    return;
  }
  sliding = true;
  // Both photos move the same distance, side by side like a carousel
  // with a small gap, rather than each crossing the full screen.
  const step = window.innerWidth + 16;

  const ghost = lightboxFrame.cloneNode(true);
  ghost.removeAttribute('id');
  ghost.querySelector('img').removeAttribute('id');
  ghost.classList.add('lb-ghost');
  ghost.style.transition = 'none';
  ghost.style.transform = `translateX(${fromX}px)`;
  lightbox.appendChild(ghost);

  lightboxFrame.style.visibility = 'hidden';
  lightboxFrame.style.transition = 'none';
  goToLightboxPhoto(target.pageIdx, target.photoIdx, () => {
    if (!sliding) return; // closed meanwhile
    setFrameX(fromX + delta * step, 0);
    lightboxFrame.style.visibility = '';
    void lightboxFrame.offsetWidth; // start from off-screen, not from 0
    setFrameX(0, SLIDE_MS);
    ghost.style.transition = `transform ${SLIDE_MS}ms ease-out`;
    ghost.style.transform = `translateX(${-delta * step}px)`;
    slideTimer = setTimeout(() => { endSlide(); preloadNeighbors(); }, SLIDE_MS + 30);
  });
}

function goToLightboxPhoto(pageIdx, photoIdx, onReady){
  const photo = DATA.pages[pageIdx] && DATA.pages[pageIdx].photos[photoIdx];
  if (!photo) return;

  lightboxPageIdx = pageIdx;
  lightboxPhotoIdx = photoIdx;

  // The book underneath is hidden behind the lightbox's opaque backdrop,
  // but its page state still needs to track along - otherwise closing the
  // lightbox would land back on whatever spread was showing before.
  const targetSpread = spreadOfPage(pageIdx);
  if (targetSpread !== spread){
    spread = targetSpread;
    bookEl.innerHTML = fullSpreadHTML(spread);
    updateChrome();
  }

  const img = findPhotoImg(pageIdx, photoIdx);
  const frameEl = img ? img.closest('.frame') : null;
  lightboxSourceImg = img;
  lightboxSourceRot = parseFloat(frameEl?.dataset.rot || '0');
  lightboxSourcePadding = frameEl ? getComputedStyle(frameEl).padding : lightboxSourcePadding;

  resetZoom(false);
  // Clear any earlier onload first: a cached image can be 'complete' right
  // away yet still fire 'load' later, which would re-run a stale callback.
  lightboxImg.onload = null;
  lightboxImg.src = photoUrl(photo.src);
  const ready = () => { lightboxImg.onload = null; fitLightboxFrame(); if (onReady) onReady(); };
  if (lightboxImg.complete) ready();
  else lightboxImg.onload = ready;
}

// ---- Lightbox touch gestures: pinch to zoom (and drag to pan while
// zoomed), double-tap to zoom in/out, swipe left/right for the next/previous
// photo when not zoomed. The zoom is a transform on the <img> alone, so it
// can spill past the frame across the whole (black) screen. ----

const MAX_ZOOM = 5, DOUBLE_TAP_ZOOM = 2.5, DOUBLE_TAP_MS = 260;
let zoom = {scale: 1, x: 0, y: 0};  // x/y: pan offset of the image's center, px
let pinch = null;   // pinch in progress: starting distance, midpoint, zoom
let pan = null;     // one-finger drag in progress while zoomed
let lightboxMultiTouch = false;  // this gesture used 2+ fingers - not a swipe
let lightboxTouchMoved = false;  // this gesture moved - not a tap
let tapTimer = null;

function applyZoom(animate){
  lightboxImg.style.transition = animate ? 'transform .25s ease' : 'none';
  lightboxImg.style.transform = zoom.scale === 1 ? '' : `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.scale})`;
}

function resetZoom(animate){
  zoom = {scale: 1, x: 0, y: 0};
  pinch = pan = null;
  applyZoom(animate);
}

// The image's untransformed center, in screen coordinates.
function imageCenter(){
  const r = lightboxFrame.getBoundingClientRect();
  return {
    x: r.left + lightboxImg.offsetLeft + lightboxImg.offsetWidth / 2,
    y: r.top + lightboxImg.offsetTop + lightboxImg.offsetHeight / 2,
  };
}

// Pans no further than it takes to bring an edge of the zoomed image to the
// edge of the screen (and not at all along a side that still fits).
function clampPan(){
  const maxX = Math.max(0, (lightboxImg.offsetWidth * zoom.scale - window.innerWidth) / 2);
  const maxY = Math.max(0, (lightboxImg.offsetHeight * zoom.scale - window.innerHeight) / 2);
  zoom.x = Math.max(-maxX, Math.min(maxX, zoom.x));
  zoom.y = Math.max(-maxY, Math.min(maxY, zoom.y));
}

// Zooms to 'scale' keeping the image point that was under 'from' (at
// zoom 'start') under the screen point 'to'.
function zoomAround(start, from, to, scale){
  const c = imageCenter();
  const localX = (from.x - c.x - start.x) / start.scale;
  const localY = (from.y - c.y - start.y) / start.scale;
  zoom = {scale, x: to.x - c.x - localX * scale, y: to.y - c.y - localY * scale};
  clampPan();
}

function touchDistance(t){ return Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY); }
function touchMidpoint(t){ return {x: (t[0].clientX + t[1].clientX) / 2, y: (t[0].clientY + t[1].clientY) / 2}; }

// One-finger horizontal drag while not zoomed: the photo follows the
// finger, and on release either slides on to the next/previous photo or
// springs back.
let swipe = null;

function finishSwipe(){
  const s = swipe;
  swipe = null;
  if (!s || !s.horizontal) return;
  const dist = Math.abs(s.dx), speed = dist / Math.max(1, Date.now() - s.t);
  const far = dist > Math.min(90, window.innerWidth * 0.22) || (dist > 30 && speed > 0.4);
  if (far) shiftLightboxPhoto(s.dx < 0 ? 1 : -1, s.dx);
  else setFrameX(0, 200);
}

lightbox.addEventListener('touchstart', (e) => {
  if (e.touches.length === 1){
    lightboxMultiTouch = false;
    lightboxTouchMoved = false;
    // A new touch mid-slide jumps to the end of that slide, so quick
    // swipes in a row each count.
    if (sliding){ endSlide(); preloadNeighbors(); }
    if (zoom.scale === 1){
      swipe = {x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now(), dx: 0, horizontal: null};
    }
  }
  if (e.touches.length >= 2){
    e.preventDefault();
    lightboxMultiTouch = true;
    if (swipe && swipe.horizontal) setFrameX(0, 150); // a second finger turns it into a pinch
    swipe = null;
    pan = null;
    pinch = {dist: touchDistance(e.touches), mid: touchMidpoint(e.touches), start: {...zoom}};
  } else if (zoom.scale > 1){
    pan = {x: e.touches[0].clientX, y: e.touches[0].clientY, start: {...zoom}};
  }
}, {passive: false});

lightbox.addEventListener('touchmove', (e) => {
  e.preventDefault(); // no page scroll or browser zoom while the lightbox is up
  if (pinch && e.touches.length >= 2){
    const scale = Math.max(1, Math.min(MAX_ZOOM, pinch.start.scale * touchDistance(e.touches) / pinch.dist));
    zoomAround(pinch.start, pinch.mid, touchMidpoint(e.touches), scale);
    applyZoom(false);
    lightboxTouchMoved = true;
  } else if (pan){
    const dx = e.touches[0].clientX - pan.x, dy = e.touches[0].clientY - pan.y;
    if (Math.abs(dx) + Math.abs(dy) > 6) lightboxTouchMoved = true;
    zoom = {...pan.start, x: pan.start.x + dx, y: pan.start.y + dy};
    clampPan();
    applyZoom(false);
  } else if (swipe){
    const dx = e.touches[0].clientX - swipe.x, dy = e.touches[0].clientY - swipe.y;
    if (swipe.horizontal === null && Math.abs(dx) + Math.abs(dy) > 8){
      swipe.horizontal = Math.abs(dx) > Math.abs(dy);
    }
    if (swipe.horizontal){
      lightboxTouchMoved = true;
      // Rubber-band at either end of the album.
      swipe.dx = neighborPhoto(dx < 0 ? 1 : -1) ? dx : dx * 0.3;
      setFrameX(swipe.dx, 0);
    }
  }
}, {passive: false});

lightbox.addEventListener('touchend', (e) => {
  if (pinch && e.touches.length < 2){
    pinch = null;
    if (zoom.scale < 1.05) resetZoom(true);
    // Lifting one finger of a pinch carries on as a pan with the other.
    else if (e.touches.length === 1) pan = {x: e.touches[0].clientX, y: e.touches[0].clientY, start: {...zoom}};
  }
  if (e.touches.length === 0){
    pan = null;
    finishSwipe();
  }
});

lightbox.addEventListener('touchcancel', () => {
  if (swipe && swipe.horizontal) setFrameX(0, 200);
  swipe = pinch = pan = null;
});

// A single tap closes (or, when zoomed in, zooms back out first); a double
// tap toggles zoom at that spot. Telling them apart means waiting briefly
// after the first tap.
lightbox.addEventListener('click', (e) => {
  if (lightboxTouchMoved){ lightboxTouchMoved = false; return; }
  if (tapTimer){
    clearTimeout(tapTimer);
    tapTimer = null;
    if (zoom.scale > 1) resetZoom(true);
    else {
      const point = {x: e.clientX, y: e.clientY};
      zoomAround({scale: 1, x: 0, y: 0}, point, point, DOUBLE_TAP_ZOOM);
      applyZoom(true);
    }
    return;
  }
  tapTimer = setTimeout(() => {
    tapTimer = null;
    if (zoom.scale > 1) resetZoom(true);
    else closeLightbox();
  }, DOUBLE_TAP_MS);
});

window.addEventListener('resize', () => {
  if (!lightboxOpen) return;
  resetZoom(false);
  fitLightboxFrame();
});

// ---- Edit mode: click the book background or press space to toggle.
// While on, an "R" button appears on each page to auto-relayout it.
// Saves the current album to disk (via the server) the moment it's
// turned off. ----

let editMode = false;
const lastLayoutByPage = {}; // pageIdx -> last layout name, just to avoid repeats

// Move mode: pick up a photo (click its number badge), page around freely,
// then click another photo's number badge to insert the picked-up one
// right before it. Clicking the picked photo's own badge again cancels.
let moveMode = false;
let movingPhoto = null; // {photo, srcPageIdx, srcPhotoIdx}

// Sentinel photoIdx for an empty page's placeholder badge (see
// emptySlotHtml) - there's no real photo at that index to pick up, just a
// drop target for whatever's currently picked up.
const EMPTY_SLOT = -1;

const toastEl = document.getElementById('toast');
let toastTimer = null;
function showToast(msg){
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2600);
}

// ---- Headline editor: the M/N buttons (top-middle of each page, edit
// mode only) open this to set/change that page's caption. ----

const headlineModal = document.createElement('div');
headlineModal.id = 'headlineModal';
headlineModal.innerHTML = `
  <div class="headline-card">
    <label for="headlineInput">Page caption</label>
    <input type="text" id="headlineInput" maxlength="120" placeholder="Add a headline for this page…">
    <div class="headline-actions">
      <button type="button" id="headlineClear">Clear</button>
      <div class="headline-actions-right">
        <button type="button" id="headlineCancel">Cancel</button>
        <button type="button" id="headlineSave">Save</button>
      </div>
    </div>
  </div>
`;
document.body.appendChild(headlineModal);
const headlineInput = document.getElementById('headlineInput');

let headlineEditingPageIdx = null;

function openHeadlineEditor(pageIdx){
  if (animating) return;
  const page = DATA.pages[pageIdx];
  if (!page) return;
  headlineEditingPageIdx = pageIdx;
  headlineInput.value = page.headline || '';
  headlineModal.classList.add('open');
  headlineInput.focus();
  headlineInput.select();
}

function closeHeadlineEditor(){
  headlineModal.classList.remove('open');
  headlineEditingPageIdx = null;
}

function saveHeadlineEditor(){
  if (headlineEditingPageIdx === null) return;
  const page = DATA.pages[headlineEditingPageIdx];
  if (page) page.headline = headlineInput.value.trim();
  const pageIdx = headlineEditingPageIdx;
  closeHeadlineEditor();
  // The first caption added (or the last one cleared) adds or removes the
  // index, which in single-page mode moves every page along by one.
  spread = spreadOfPage(pageIdx);
  totalSpreads = countSpreads();
  renderSpreadInstant();
}

document.getElementById('headlineSave').addEventListener('click', saveHeadlineEditor);
document.getElementById('headlineCancel').addEventListener('click', closeHeadlineEditor);
document.getElementById('headlineClear').addEventListener('click', () => {
  headlineInput.value = '';
  saveHeadlineEditor();
});
headlineModal.addEventListener('click', (e) => {
  if (e.target === headlineModal) closeHeadlineEditor();
});

function cancelMoveMode(){
  moveMode = false;
  movingPhoto = null;
  document.body.classList.remove('move-mode');
}

function toggleEditMode(){
  if (READ_ONLY || animating || lightboxOpen) return;
  editMode = !editMode;
  document.body.classList.toggle('edit-mode', editMode);
  if (!editMode){
    cancelMoveMode();
    saveState();
  }
}

function saveState(){
  const payload = {
    folder: DATA.folder,
    count: DATA.count,
    cover: DATA.cover,
    pages: DATA.pages,
  };
  fetch('/api/save', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  }).then(res => {
    if (!res.ok) throw new Error('save failed');
    return res.json();
  }).then(result => {
    // The server renames photo files to match reading order on every save,
    // so refresh DATA (new filenames) and redraw or the on-screen <img>s
    // would keep pointing at names that no longer exist on disk.
    if (result && result.album){
      DATA = result.album;
      bookEl.innerHTML = fullSpreadHTML(spread);
    }
  }).catch(() => {
    showToast('Could not save (no server to save to)');
  });
}

// Computes and applies a fresh layout to a page in place - no re-render,
// so callers can lay out more than one page and render once at the end.
function applyLayoutToPage(pageIdx, avoidRepeat){
  const page = DATA.pages[pageIdx];
  if (!page || !page.photos.length) return;

  const count = page.photos.length;
  const choices = LAYOUTS_BY_COUNT[count];
  let rects;
  if (choices){
    let layout = choices[Math.floor(Math.random() * choices.length)];
    if (avoidRepeat && choices.length > 1 && layout === lastLayoutByPage[pageIdx]){
      layout = choices[(choices.indexOf(layout) + 1) % choices.length];
    }
    lastLayoutByPage[pageIdx] = layout;
    rects = LAYOUT_RECTS[layout];
  } else {
    rects = gridRects(count);
  }

  page.photos.forEach((photo, i) => {
    const rect = rects[i] || rects[rects.length - 1];
    photo.x = rect.x; photo.y = rect.y; photo.w = rect.w; photo.h = rect.h;
    photo.rot = Math.round((Math.random() * 9 - 4.5) * 10) / 10;
  });
}

function relayoutPage(pageIdx){
  if (animating) return;
  applyLayoutToPage(pageIdx, true);
  bookEl.innerHTML = fullSpreadHTML(spread);
}

// Inserts a new, empty spread right after the one on screen (spread 0, the
// cover, counts as "before page 1" here) and jumps straight to it - no flip
// animation, same as every other structural edit-mode action.
function addSpreadAfterCurrent(){
  if (animating) return;
  // After the current spread's pages; from the cover or index, page 1.
  const insertAt = Math.max(0, firstPageOf(spread + 1));
  const newPages = Array.from({length: pagesPerSpread()}, () => ({photos: [], headline: ''}));
  DATA.pages.splice(insertAt, 0, ...newPages);
  totalSpreads = countSpreads();
  spread = spreadOfPage(insertAt);
  bookEl.innerHTML = fullSpreadHTML(spread);
  updateChrome();
}

// Deletes the current spread's page(s) and folds whatever photos were on
// them into the page that follows - falling back to the page before, or
// keeping them as their own page, if there's nothing after - so this can
// never silently lose photos, only move where they live.
function deleteCurrentSpread(){
  const leftPageIdx = firstPageOf(spread);
  if (animating || leftPageIdx < 0) return; // the cover or the index
  const removeCount = Math.min(pagesPerSpread(), DATA.pages.length - leftPageIdx);
  if (removeCount <= 0) return;

  const removed = DATA.pages.splice(leftPageIdx, removeCount);
  const mergedPhotos = removed.reduce((all, page) => all.concat(page.photos), []);

  if (mergedPhotos.length){
    if (leftPageIdx < DATA.pages.length){
      DATA.pages[leftPageIdx].photos = mergedPhotos.concat(DATA.pages[leftPageIdx].photos);
      applyLayoutToPage(leftPageIdx, false);
    } else if (leftPageIdx > 0){
      const prevIdx = leftPageIdx - 1;
      DATA.pages[prevIdx].photos = DATA.pages[prevIdx].photos.concat(mergedPhotos);
      applyLayoutToPage(prevIdx, false);
    } else {
      DATA.pages.push({photos: mergedPhotos, headline: ''});
      applyLayoutToPage(DATA.pages.length - 1, false);
    }
  }

  totalSpreads = countSpreads();
  spread = Math.min(spread, totalSpreads - 1);
  bookEl.innerHTML = fullSpreadHTML(spread);
  updateChrome();
}

// Finds which (pageIdx, photoIdx) currently shows a given badge number on
// the spread that's on screen right now - same numbering slotAt() renders,
// so a keypress lands on exactly the photo (or empty-page placeholder)
// whose badge it matches.
function findBadgeTarget(num){
  const leftPageIdx = firstPageOf(spread);
  if (leftPageIdx < 0 || num < 1) return null; // the cover or the index
  const rightPageIdx = leftPageIdx + 1;
  const leftPage = DATA.pages[leftPageIdx];
  const rightPage = singlePage ? undefined : DATA.pages[rightPageIdx];

  if (leftPage){
    if (leftPage.photos.length === 0){
      if (num === 1) return {pageIdx: leftPageIdx, photoIdx: EMPTY_SLOT};
    } else if (num <= leftPage.photos.length){
      return {pageIdx: leftPageIdx, photoIdx: num - 1};
    }
  }
  if (rightPage){
    const rightStart = 1 + (leftPage ? Math.max(1, leftPage.photos.length) : 0);
    if (rightPage.photos.length === 0){
      if (num === rightStart) return {pageIdx: rightPageIdx, photoIdx: EMPTY_SLOT};
    } else if (num >= rightStart && num < rightStart + rightPage.photos.length){
      return {pageIdx: rightPageIdx, photoIdx: num - rightStart};
    }
  }

  // Numbering on this spread is contiguous with no gaps, so a digit that
  // didn't match anything above is past the last badge in use - still
  // resolves somewhere sensible (append to the end of the last page) rather
  // than doing nothing, so overshooting the count is forgiving, not a dead
  // keypress.
  if (rightPage) return {pageIdx: rightPageIdx, photoIdx: EMPTY_SLOT};
  if (leftPage) return {pageIdx: leftPageIdx, photoIdx: EMPTY_SLOT};
  return null;
}

// The move-btn badges carry data-page-idx/data-photo-idx and exist in the
// DOM (just visually hidden outside edit mode), so they double as a way to
// find a given photo's <img> on screen without a separate lookup table.
function findPhotoImg(pageIdx, photoIdx){
  const btn = document.querySelector(`.move-btn[data-page-idx="${pageIdx}"][data-photo-idx="${photoIdx}"]`);
  const frame = btn ? btn.closest('.frame') : null;
  return frame ? frame.querySelector('img') : null;
}

function handleMoveButtonClick(pageIdx, photoIdx){
  if (photoIdx === EMPTY_SLOT){
    // An empty page's placeholder badge: nothing to pick up there, but if
    // something's already picked up, this is exactly where "drop it here"
    // belongs - same as clicking that page's blank background.
    if (moveMode) performMoveToEnd(pageIdx);
    return;
  }

  const photo = DATA.pages[pageIdx].photos[photoIdx];
  if (!photo) return;

  if (!moveMode){
    movingPhoto = {photo, srcPageIdx: pageIdx, srcPhotoIdx: photoIdx};
    moveMode = true;
    document.body.classList.add('move-mode');
    bookEl.innerHTML = fullSpreadHTML(spread);
    return;
  }

  if (photo === movingPhoto.photo){
    cancelMoveMode();
    bookEl.innerHTML = fullSpreadHTML(spread);
    return;
  }

  performMove(pageIdx, photoIdx);
}

function performMove(destPageIdx, destPhotoIdx){
  const {photo, srcPageIdx, srcPhotoIdx} = movingPhoto;

  DATA.pages[srcPageIdx].photos.splice(srcPhotoIdx, 1);

  let insertAt = destPhotoIdx;
  if (destPageIdx === srcPageIdx && destPhotoIdx > srcPhotoIdx){
    insertAt -= 1;
  }
  DATA.pages[destPageIdx].photos.splice(insertAt, 0, photo);

  applyLayoutToPage(srcPageIdx, false);   // may now be empty - a no-op then
  applyLayoutToPage(destPageIdx, false);

  cancelMoveMode();
  bookEl.innerHTML = fullSpreadHTML(spread);
}

// Dropping on a page's blank background (rather than a specific photo's
// number badge) appends the picked-up photo to the end of that page -
// also the only way to target a page that's currently empty.
function performMoveToEnd(destPageIdx){
  const {photo, srcPageIdx, srcPhotoIdx} = movingPhoto;

  DATA.pages[srcPageIdx].photos.splice(srcPhotoIdx, 1);
  DATA.pages[destPageIdx].photos.push(photo);

  applyLayoutToPage(srcPageIdx, false);
  applyLayoutToPage(destPageIdx, false);

  cancelMoveMode();
  bookEl.innerHTML = fullSpreadHTML(spread);
}

bookEl.addEventListener('click', (e) => {
  if (animating) return;

  const indexEntry = e.target.closest('.index-entry');
  if (indexEntry){
    jumpToPage(parseInt(indexEntry.dataset.pageIdx, 10));
    return;
  }

  const relayoutBtn = e.target.closest('.relayout-btn');
  if (relayoutBtn){
    relayoutPage(parseInt(relayoutBtn.dataset.pageIdx, 10));
    return;
  }

  const headlineBtn = e.target.closest('.headline-btn');
  if (headlineBtn){
    openHeadlineEditor(parseInt(headlineBtn.dataset.pageIdx, 10));
    return;
  }

  const moveBtn = e.target.closest('.move-btn');
  if (moveBtn){
    handleMoveButtonClick(parseInt(moveBtn.dataset.pageIdx, 10), parseInt(moveBtn.dataset.photoIdx, 10));
    return;
  }

  const img = e.target.closest('.frame img');
  if (img){
    const frameBtn = img.closest('.frame').querySelector('.move-btn');
    const pageIdx = frameBtn ? parseInt(frameBtn.dataset.pageIdx, 10) : null;
    const photoIdx = frameBtn ? parseInt(frameBtn.dataset.photoIdx, 10) : null;
    openLightbox(img, pageIdx, photoIdx);
    return;
  }

  if (moveMode){
    const pageEl = e.target.closest('.page');
    const pageIdx = pageEl ? pageEl.dataset.pageIdx : undefined;
    if (pageIdx !== undefined) performMoveToEnd(parseInt(pageIdx, 10));
    return; // stay in move mode on any other click (e.g. the blank cover)
  }

  toggleEditMode();
});


function escapeHtml(s){
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// The page counter (with its contents icon) is the way back to the index.
indexBtn.addEventListener('click', () => {
  if (animating || !hasIndex() || spread === indexSpread()) return;
  showStart();
});

prevBtn.addEventListener('click', () => go(-1));
nextBtn.addEventListener('click', () => go(1));
addSpreadBtn.addEventListener('click', addSpreadAfterCurrent);
deleteSpreadBtn.addEventListener('click', deleteCurrentSpread);

document.addEventListener('keydown', (e) => {
  if (headlineModal.classList.contains('open')){
    if (e.key === 'Escape'){ closeHeadlineEditor(); return; }
    if (e.key === 'Enter'){ saveHeadlineEditor(); return; }
    return; // let every other key type normally into the input, nothing else
  }
  if (e.key === 'Escape'){
    if (lightboxOpen){ closeLightbox(); return; }
    if (moveMode){ cancelMoveMode(); bookEl.innerHTML = fullSpreadHTML(spread); return; }
    return;
  }
  if (e.key === ' ' || e.code === 'Space'){ e.preventDefault(); toggleEditMode(); return; }
  if (editMode && !animating && (e.key === 'l' || e.key === 'L')){
    const pageIdx = slotAt(spread, 'left').pageIdx;
    if (pageIdx !== null) relayoutPage(pageIdx);
    return;
  }
  if (editMode && !animating && !singlePage && (e.key === 'r' || e.key === 'R')){
    const pageIdx = slotAt(spread, 'right').pageIdx;
    if (pageIdx !== null) relayoutPage(pageIdx);
    return;
  }
  if (editMode && !animating && (e.key === 'm' || e.key === 'M')){
    const pageIdx = slotAt(spread, 'left').pageIdx;
    if (pageIdx !== null){ e.preventDefault(); openHeadlineEditor(pageIdx); }
    return;
  }
  if (editMode && !animating && (e.key === 'n' || e.key === 'N')){
    const pageIdx = slotAt(spread, 'right').pageIdx;
    if (pageIdx !== null){ e.preventDefault(); openHeadlineEditor(pageIdx); }
    return;
  }
  if (editMode && !animating && (e.key === 'a' || e.key === 'A')){
    addSpreadAfterCurrent();
    return;
  }
  if (editMode && !animating && (e.key === 'd' || e.key === 'D')){
    deleteCurrentSpread();
    return;
  }
  if (editMode && !animating && /^[1-9]$/.test(e.key)){
    const target = findBadgeTarget(parseInt(e.key, 10));
    if (target) handleMoveButtonClick(target.pageIdx, target.photoIdx);
    return;
  }
  if (!editMode && !animating && /^[1-9]$/.test(e.key)){
    const target = findBadgeTarget(parseInt(e.key, 10));
    const img = target ? findPhotoImg(target.pageIdx, target.photoIdx) : null;
    if (img) openLightbox(img, target.pageIdx, target.photoIdx);
    return;
  }
  if (lightboxOpen){
    if (e.key === 'ArrowRight'){ shiftLightboxPhoto(1); return; }
    if (e.key === 'ArrowLeft'){ shiftLightboxPhoto(-1); return; }
  }
  if (e.key === 'ArrowRight') go(1);
  if (e.key === 'ArrowLeft') go(-1);
});

let touchStartX = null, touchStartY = null;
document.addEventListener('touchstart', (e) => {
  touchStartX = e.changedTouches[0].clientX;
  touchStartY = e.changedTouches[0].clientY;
}, {passive:true});

document.addEventListener('touchend', (e) => {
  if (touchStartX === null) return;
  const dx = e.changedTouches[0].clientX - touchStartX;
  const dy = e.changedTouches[0].clientY - touchStartY;
  touchStartX = null;
  // (In the lightbox, swipes are handled by its own touch handlers.)
  if (!lightboxOpen && Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5){
    go(dx < 0 ? 1 : -1);
  }
}, {passive:true});

fetch('__API_URL__').then(r => r.json()).then(data => {
  DATA = data;
  if (!DATA.count){
    bookEl.style.display = 'flex';
    bookEl.style.alignItems = 'center';
    bookEl.style.justifyContent = 'center';
    bookEl.innerHTML = '<div id="empty">No photos found in this folder.<br>Add some .jpg / .png files and refresh.</div>';
    prevBtn.style.display = 'none';
    nextBtn.style.display = 'none';
    return;
  }
  applyPageMode();
  // A reload keeps its history entry: reopen on the page it was showing.
  const restoreIdx = history.state && history.state.pageIdx;
  if (restoreIdx != null && DATA.pages[restoreIdx]){
    jumpToPage(restoreIdx);
    setHistory('replaceState', {view: 'page', pageIdx: restoreIdx});
  } else {
    setHistory('replaceState', {view: 'start'});
  }
  historyReady = true;
});

// Switch between spreads and single pages (e.g. on rotating a phone),
// staying on the same page: the first page showing before the switch is
// the one showing after it.
function applyPageMode(){
  modeChangePending = false;
  const wasSingle = document.body.classList.contains('single-page');
  if (wasSingle !== singlePage){
    const oldOffset = wasSingle && hasIndex() ? 1 : 0;
    const pageIdx = (spread - 1 - oldOffset) * (wasSingle ? 1 : 2);
    if (pageIdx >= 0) spread = spreadOfPage(pageIdx);
    // Leaving single-page mode from the index page: it lives on the cover
    // spread there. (The cover itself stays the cover, spread 0.)
    else if (spread > 0) spread = indexSpread();
  }
  document.body.classList.toggle('single-page', singlePage);
  totalSpreads = countSpreads();
  spread = Math.min(spread, totalSpreads - 1);
  renderSpreadInstant();
  // An open lightbox zooms back out to its photo's frame, which was just
  // re-rendered - point it at the new element.
  if (lightboxOpen && lightboxPageIdx !== null){
    lightboxSourceImg = findPhotoImg(lightboxPageIdx, lightboxPhotoIdx) || lightboxSourceImg;
  }
  // ...and re-fit it: framed in spreads, edge to edge in single-page mode.
  if (lightboxOpen){
    resetZoom(false);
    fitLightboxFrame();
  }
}

singlePageQuery.addEventListener('change', (e) => {
  singlePage = e.matches;
  if (!DATA || !DATA.count) return;
  if (animating) modeChangePending = true; // finishFlip applies it
  else applyPageMode();
});
</script>
</body>
</html>
"""


def sanitize_album_payload(payload, folder: Path):
    """Validate/clean a client-submitted album before it's trusted enough
    to write to disk: every photo src must name a real file that's
    already in this folder (never take a path from the client as-is -
    that's how you get path traversal), and every position field must
    actually be a number."""
    if not isinstance(payload, dict):
        return None

    known_names = {p.name for p in find_photos(folder)}

    def clean_src(entry):
        src = entry.get("src") if isinstance(entry, dict) else None
        return src if isinstance(src, str) and src in known_names else None

    def num(entry, key, default):
        value = entry.get(key, default)
        return float(value) if isinstance(value, (int, float)) else default

    cover = [s for s in payload.get("cover", []) if isinstance(s, str) and s in known_names]

    pages = []
    for page in payload.get("pages", []):
        if not isinstance(page, dict):
            continue
        photos = []
        for photo in page.get("photos", []):
            src = clean_src(photo)
            if src is None:
                continue
            photos.append({
                "src": src,
                "x": num(photo, "x", 0.0), "y": num(photo, "y", 0.0),
                "w": num(photo, "w", 20.0), "h": num(photo, "h", 20.0),
                "rot": num(photo, "rot", 0.0),
            })
        headline = page.get("headline", "")
        if not isinstance(headline, str):
            headline = ""
        # An empty page (0 photos, after a move emptied it) is kept, not
        # dropped - otherwise page indices would shift on the next load.
        pages.append({"photos": photos, "headline": headline[:200]})

    return {
        "folder": folder.name or str(folder),
        "count": len(known_names),
        "cover": cover,
        "pages": pages,
    }


def rename_photos_to_order(folder: Path, album: dict):
    """Rename every photo file on disk to '<folder name> NNNN.<ext>', NNNN
    being its 1-based position walking the pages in order (page order,
    then photo order within each page) - so filenames always match
    reading order. Only files whose name would actually change are
    touched, and those go through a unique temp name first in a
    separate pass, so overlapping old/new name sets can never collide
    mid-rename (e.g. a straight swap between two photos). Returns a new
    album dict with every src updated to match what's now on disk."""
    ordered_srcs = [
        photo["src"]
        for page in album.get("pages", [])
        for photo in page.get("photos", [])
    ]

    width = max(4, len(str(len(ordered_srcs))))
    targets = {}  # old name -> new name, only where it actually differs
    for i, old_name in enumerate(ordered_srcs):
        ext = Path(old_name).suffix
        new_name = f"{folder.name} {i + 1:0{width}d}{ext}"
        if new_name != old_name and (folder / old_name).is_file():
            targets[old_name] = new_name

    temp_names = {}
    for old_name in targets:
        temp_name = f".rename-tmp-{uuid.uuid4().hex}{Path(old_name).suffix}"
        (folder / old_name).rename(folder / temp_name)
        temp_names[old_name] = temp_name

    for old_name, new_name in targets.items():
        (folder / temp_names[old_name]).rename(folder / new_name)

    def remap(name):
        return targets.get(name, name)

    return {
        **album,
        "cover": [remap(s) for s in album.get("cover", [])],
        "pages": [
            {**page, "photos": [{**p, "src": remap(p["src"])} for p in page.get("photos", [])]}
            for page in album.get("pages", [])
        ],
    }


def render_page(api_url, photo_prefix, read_only):
    return (HTML_PAGE.replace("__API_URL__", api_url)
            .replace("__PHOTO_PREFIX__", photo_prefix)
            .replace("__READ_ONLY__", "true" if read_only else "false"))


class AlbumHandler(BaseHTTPRequestHandler):
    album = None
    photo_index = {}
    folder = None
    read_only = False

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/":
            html = render_page("/api/data", "/photo/", self.read_only)
            self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/data":
            self._send_json(self.album)
        elif path.startswith("/photo/"):
            name = path[len("/photo/"):]
            self._serve_photo(name)
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path != "/api/save":
            self.send_error(404, "Not found")
            return
        if self.read_only:
            self._send_json({"ok": False, "error": "read-only"}, status=403)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "invalid JSON"}, status=400)
            return

        album = sanitize_album_payload(payload, self.folder)
        if album is None:
            self._send_json({"ok": False, "error": "invalid payload"}, status=400)
            return

        try:
            album = rename_photos_to_order(self.folder, album)
            # Renaming can reassign a filename to different photo bytes (a
            # straight swap between two positions, say). Browsers cache
            # /photo/<name> responses by URL, so without something in the
            # URL that changes on every save, a stale cached image can keep
            # showing under a reused name - looking exactly like a photo
            # ordering bug. Bumping this each save and having the client
            # append it as a query string (see PHOTO_PREFIX usages) forces
            # a fresh fetch for every photo after any save.
            album["version"] = int(time.time() * 1000)
            state_path = self.folder / STATE_FILENAME
            state_path.write_text(json.dumps(album, indent=2), encoding="utf-8")
        except OSError as e:
            self._send_json({"ok": False, "error": str(e)}, status=500)
            return
        album = add_aspect_ratios(self.folder, album)

        AlbumHandler.album = album
        AlbumHandler.photo_index = {
            src: self.folder / src
            for src in set(album["cover"]) | {p["src"] for page in album["pages"] for p in page["photos"]}
        }
        self._send_json({"ok": True, "album": album})

    def _serve_photo(self, name):
        full_path = self.photo_index.get(name)
        if full_path is None or not full_path.is_file():
            self.send_error(404, "Photo not found")
            return
        content_type = mimetypes.guess_type(full_path.name)[0] or "application/octet-stream"
        data = full_path.read_bytes()
        self._send_bytes(data, content_type, cache=True)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, content_type, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)


def export_static(folder: Path, out_dir: Path):
    """Bake a folder's album into a self-contained static site: index.html,
    data.json and a photos/ directory. No server required - works on any
    static host (Netlify, GitHub Pages, S3, a plain file:// open, etc.)."""
    album = get_album(folder)

    out_dir.mkdir(parents=True, exist_ok=True)
    photos_dir = out_dir / "photos"
    photos_dir.mkdir(exist_ok=True)

    referenced = set(album["cover"])
    for page in album["pages"]:
        for photo in page["photos"]:
            referenced.add(photo["src"])

    for name in referenced:
        shutil.copy2(folder / name, photos_dir / name)

    (out_dir / "data.json").write_text(json.dumps(album), encoding="utf-8")

    # A static site has nowhere to save edits to, so it's always read-only.
    html = render_page("./data.json", "./photos/", read_only=True)
    (out_dir / "index.html").write_text(html, encoding="utf-8")

    return album


def pick_port(host, start_port):
    port = start_port
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                port += 1
    return start_port


def main():
    parser = argparse.ArgumentParser(description="Serve a photo album for the current folder.")
    parser.add_argument("--port", type=int, default=8000, help="port to listen on (default 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="host to bind (default 0.0.0.0)")
    parser.add_argument("--open", action="store_true", help="open the album in your default browser")
    parser.add_argument("--folder", default=".", help="folder of photos to serve (default: current folder)")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="just browse: no edit mode, nothing is saved or renamed",
    )
    parser.add_argument(
        "--export",
        metavar="DIR",
        help="write a self-contained static site (index.html + data.json + photos/) to DIR and exit, instead of serving",
    )
    args = parser.parse_args()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"Not a folder: {folder}", file=sys.stderr)
        sys.exit(1)

    if args.export:
        out_dir = Path(args.export).resolve()
        album = export_static(folder, out_dir)
        print(f"Exported '{folder.name}' ({album['count']} photos) to {out_dir}")
        print(f"Open {out_dir / 'index.html'} in a browser, or deploy the folder to any static host.")
        return

    album = get_album(folder)
    photo_index = {p["src"]: folder / p["src"] for p in [{"src": s} for s in album["cover"]]}
    for page in album["pages"]:
        for photo in page["photos"]:
            photo_index[photo["src"]] = folder / photo["src"]

    AlbumHandler.album = album
    AlbumHandler.photo_index = photo_index
    AlbumHandler.folder = folder
    AlbumHandler.read_only = args.read_only

    port = pick_port(args.host, args.port)
    server = ThreadingHTTPServer((args.host, port), AlbumHandler)

    display_host = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{display_host}:{port}/"
    print(f"Photobook for '{folder.name}' ({album['count']} photos)")
    print(f"Serving at {url}  (Ctrl+C to stop)")
    if args.read_only:
        print("Read-only: edit mode is off and nothing will be saved.")

    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
