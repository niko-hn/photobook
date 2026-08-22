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
import sys
import threading
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
        pages.append({"photos": lay_out_photo_group(group, rng)})
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
        return build_album(folder)
    return reconcile_new_photos(folder, album)


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
    padding:5%;
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
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

  /* ---- Cover ---- */
  .cover-wrap{
    display:flex;
    flex-direction:column;
    align-items:center;
    gap:28px;
    text-align:center;
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
    width:100%;
    max-width:340px;
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
  #lightbox.open{
    opacity:1;
    pointer-events:auto;
  }
  #lightboxFrame{
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
  #lightboxFrame img{
    display:block;
    width:100%;
    height:100%;
    object-fit:cover;
    object-position:center top;
  }

  .page-canvas{
    position:relative;
    width:100%;
    height:100%;
  }
  .page-canvas .frame{
    position:absolute;
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
  .flip-face{
    position:absolute;
    inset:0;
    background:var(--paper);
    padding:5%;
    display:flex;
    align-items:center;
    justify-content:center;
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
    font-size:11px;
    color:#f4ecdd99;
    letter-spacing:.1em;
  }

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
  <div id="progress"></div>
  <div id="toast"></div>
</div>

<script>
let DATA = null;
let spread = 0; // 0 = cover, 1..N = page pairs
let totalSpreads = 1;
let animating = false;

const FLIP_MS = 620;

const bookEl = document.getElementById('book');
const prevBtn = document.getElementById('prevBtn');
const nextBtn = document.getElementById('nextBtn');
const progressEl = document.getElementById('progress');

const PHOTO_PREFIX = '__PHOTO_PREFIX__';

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

function frameHtml(photo, num, pageIdx, photoIdx){
  const rot = photo.rot || 0;
  const pos = `left:${photo.x}%; top:${photo.y}%; width:${photo.w}%; height:${photo.h}%;`;
  const picked = moveMode && movingPhoto && movingPhoto.photo === photo;
  const numBtn = (num != null)
    ? `<button class="move-btn${picked ? ' picked' : ''}" data-page-idx="${pageIdx}" data-photo-idx="${photoIdx}" title="${picked ? 'Cancel move' : 'Move this picture'}">${num}</button>`
    : '';
  return `<div class="frame${picked ? ' picked' : ''}" data-rot="${rot}" style="${pos} transform:rotate(${rot}deg)">
            <img src="${PHOTO_PREFIX}${encodeURIComponent(photo.src)}" alt="">
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
              <img src="${PHOTO_PREFIX}${encodeURIComponent(src)}" alt="">
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
  const frames = page.photos.map((photo, i) => frameHtml(photo, startNum + i, pageIdx, i)).join('');
  return `<div class="page-canvas">${frames}</div>`;
}

// Returns the content ('cls' + 'inner' html + 'pageIdx') that belongs in a
// given left/right slot of a given spread, independent of what's currently
// rendered - used both for the static book and for flip-animation faces.
// pageIdx is null for the cover and blank slots (nothing to relayout there).
// Photo numbering is local to the spread: the left page starts at 1, and
// the right page continues on from wherever the left page left off - so
// every visible badge is a small, unambiguous number a keypress can target.
function slotAt(spreadIdx, side){
  if (spreadIdx === 0){
    if (side === 'left') return {cls: 'blank', inner: '', pageIdx: null};
    return {cls: '', inner: coverInner(), pageIdx: null};
  }
  const leftPageIdx = (spreadIdx - 1) * 2;
  const pageIdx = side === 'left' ? leftPageIdx : leftPageIdx + 1;
  const page = DATA.pages[pageIdx];
  if (!page) return {cls: 'blank', inner: '', pageIdx: null};

  let startNum = 1;
  if (side === 'right'){
    const leftPage = DATA.pages[leftPageIdx];
    startNum = 1 + (leftPage ? leftPage.photos.length : 0);
  }
  return {cls: '', inner: albumPageInner(page, startNum, pageIdx), pageIdx};
}

function pageHtml(slot, side){
  const letter = side === 'left' ? 'L' : 'R';
  const btn = slot.pageIdx !== null
    ? `<button class="relayout-btn ${side}" data-page-idx="${slot.pageIdx}" title="Auto-relayout this page">${letter}</button>`
    : '';
  const sideCls = side === 'left' ? 'page-left' : 'page-right';
  const pageIdxAttr = slot.pageIdx !== null ? ` data-page-idx="${slot.pageIdx}"` : '';
  return `<div class="page ${sideCls} ${slot.cls}"${pageIdxAttr}>${slot.inner}${btn}</div>`;
}

function fullSpreadHTML(spreadIdx){
  const l = slotAt(spreadIdx, 'left');
  const r = slotAt(spreadIdx, 'right');
  return pageHtml(l, 'left') + pageHtml(r, 'right');
}

function renderSpreadInstant(){
  bookEl.innerHTML = fullSpreadHTML(spread);
  updateChrome();
}

function updateChrome(){
  prevBtn.disabled = animating || spread === 0;
  nextBtn.disabled = animating || spread === totalSpreads - 1;
  progressEl.textContent = spread === 0 ? '' : `${spread} / ${totalSpreads - 1}`;
}

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
  bookEl.innerHTML = fullSpreadHTML(spread); // replaces the overlay too, now that it's done
  animating = false;
  updateChrome();
}

function flipForward(nextSpread){
  animating = true;
  updateChrome();

  const frontSlot = slotAt(spread, 'right');         // page currently showing, about to turn
  const backSlot = slotAt(nextSpread, 'left');        // revealed as its back, once fully turned
  const revealedRight = slotAt(nextSpread, 'right');  // sits underneath, revealed as the turning page uncovers it
  const staysLeft = slotAt(spread, 'left');           // untouched until the flip actually gets there

  bookEl.innerHTML = pageHtml(staysLeft, 'left') + pageHtml(revealedRight, 'right');

  const overlay = buildFlipOverlay('dir-fwd', 'right', frontSlot, backSlot);
  bookEl.appendChild(overlay);

  const anim = overlay.animate(
    [{transform: 'rotateY(0deg)'}, {transform: 'rotateY(-180deg)'}],
    {duration: FLIP_MS, easing: 'cubic-bezier(.45,.05,.35,1)'}
  );
  animateShade(overlay);
  anim.onfinish = () => { spread = nextSpread; finishFlip(); };
}

function flipBackward(nextSpread){
  animating = true;
  updateChrome();

  const frontSlot = slotAt(spread, 'left');          // page currently showing, about to turn back
  const backSlot = slotAt(nextSpread, 'right');       // revealed as its back, once fully turned
  const revealedLeft = slotAt(nextSpread, 'left');    // sits underneath, revealed as the turning page uncovers it
  const staysRight = slotAt(spread, 'right');         // untouched until the flip actually gets there

  bookEl.innerHTML = pageHtml(revealedLeft, 'left') + pageHtml(staysRight, 'right');

  const overlay = buildFlipOverlay('dir-back', 'left', frontSlot, backSlot);
  bookEl.appendChild(overlay);

  const anim = overlay.animate(
    [{transform: 'rotateY(0deg)'}, {transform: 'rotateY(180deg)'}],
    {duration: FLIP_MS, easing: 'cubic-bezier(.45,.05,.35,1)'}
  );
  animateShade(overlay);
  anim.onfinish = () => { spread = nextSpread; finishFlip(); };
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

// Same polaroid proportions as .frame (8px 8px 22px 8px), scaled up for
// a full-screen view instead of staying pinned at that fixed pixel size.
function fitFrameRect(naturalW, naturalH){
  const vw = window.innerWidth, vh = window.innerHeight;
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

function openLightbox(imgEl){
  if (animating || lightboxOpen) return;
  lightboxOpen = true;
  lightboxSourceImg = imgEl;
  const frameEl = imgEl.closest('.frame');
  lightboxSourceRot = parseFloat(frameEl?.dataset.rot || '0');
  lightboxSourcePadding = frameEl ? getComputedStyle(frameEl).padding : '8px 8px 22px 8px';

  const r = (frameEl || imgEl).getBoundingClientRect();
  lightboxImg.src = imgEl.src;

  lightboxFrame.style.transition = 'none';
  lightboxFrame.style.top = r.top + 'px';
  lightboxFrame.style.left = r.left + 'px';
  lightboxFrame.style.width = r.width + 'px';
  lightboxFrame.style.height = r.height + 'px';
  lightboxFrame.style.padding = lightboxSourcePadding;
  lightboxFrame.style.transform = `rotate(${lightboxSourceRot}deg)`;

  lightbox.classList.add('open');
  prevBtn.style.visibility = 'hidden';
  nextBtn.style.visibility = 'hidden';

  void lightboxFrame.offsetWidth; // force reflow so the next change transitions

  const onLoad = () => {
    const target = fitFrameRect(lightboxImg.naturalWidth, lightboxImg.naturalHeight);
    lightboxFrame.style.transition = '';
    lightboxFrame.style.top = target.top + 'px';
    lightboxFrame.style.left = target.left + 'px';
    lightboxFrame.style.width = target.width + 'px';
    lightboxFrame.style.height = target.height + 'px';
    lightboxFrame.style.padding = target.padding;
    lightboxFrame.style.transform = 'rotate(0deg)';
  };
  if (lightboxImg.complete) onLoad();
  else lightboxImg.onload = onLoad;
}

function closeLightbox(){
  if (!lightboxOpen) return;
  lightboxOpen = false;

  const frameEl = lightboxSourceImg ? lightboxSourceImg.closest('.frame') : null;
  const r = (frameEl || lightboxSourceImg) ? (frameEl || lightboxSourceImg).getBoundingClientRect() : null;
  lightbox.classList.remove('open');
  prevBtn.style.visibility = '';
  nextBtn.style.visibility = '';

  if (r){
    lightboxFrame.style.top = r.top + 'px';
    lightboxFrame.style.left = r.left + 'px';
    lightboxFrame.style.width = r.width + 'px';
    lightboxFrame.style.height = r.height + 'px';
    lightboxFrame.style.padding = lightboxSourcePadding;
    lightboxFrame.style.transform = `rotate(${lightboxSourceRot}deg)`;
  }
}

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

const toastEl = document.getElementById('toast');
let toastTimer = null;
function showToast(msg){
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2600);
}

function cancelMoveMode(){
  moveMode = false;
  movingPhoto = null;
  document.body.classList.remove('move-mode');
}

function toggleEditMode(){
  if (animating || lightboxOpen) return;
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

// Finds which (pageIdx, photoIdx) currently shows a given badge number on
// the spread that's on screen right now - same numbering slotAt() renders,
// so a keypress lands on exactly the photo whose badge it matches.
function findBadgeTarget(num){
  if (spread === 0 || num < 1) return null;
  const leftPageIdx = (spread - 1) * 2;
  const rightPageIdx = leftPageIdx + 1;
  const leftPage = DATA.pages[leftPageIdx];
  const rightPage = DATA.pages[rightPageIdx];

  if (leftPage && num <= leftPage.photos.length){
    return {pageIdx: leftPageIdx, photoIdx: num - 1};
  }
  if (rightPage){
    const rightStart = 1 + (leftPage ? leftPage.photos.length : 0);
    if (num >= rightStart && num < rightStart + rightPage.photos.length){
      return {pageIdx: rightPageIdx, photoIdx: num - rightStart};
    }
  }
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

  const relayoutBtn = e.target.closest('.relayout-btn');
  if (relayoutBtn){
    relayoutPage(parseInt(relayoutBtn.dataset.pageIdx, 10));
    return;
  }

  const moveBtn = e.target.closest('.move-btn');
  if (moveBtn){
    handleMoveButtonClick(parseInt(moveBtn.dataset.pageIdx, 10), parseInt(moveBtn.dataset.photoIdx, 10));
    return;
  }

  const img = e.target.closest('.frame img');
  if (img){ openLightbox(img); return; }

  if (moveMode){
    const pageEl = e.target.closest('.page');
    const pageIdx = pageEl ? pageEl.dataset.pageIdx : undefined;
    if (pageIdx !== undefined) performMoveToEnd(parseInt(pageIdx, 10));
    return; // stay in move mode on any other click (e.g. the blank cover)
  }

  toggleEditMode();
});

lightbox.addEventListener('click', () => closeLightbox());

function escapeHtml(s){
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

prevBtn.addEventListener('click', () => go(-1));
nextBtn.addEventListener('click', () => go(1));

document.addEventListener('keydown', (e) => {
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
  if (editMode && !animating && (e.key === 'r' || e.key === 'R')){
    const pageIdx = slotAt(spread, 'right').pageIdx;
    if (pageIdx !== null) relayoutPage(pageIdx);
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
    if (img) openLightbox(img);
    return;
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
  if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5){
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
  totalSpreads = 1 + Math.ceil(DATA.pages.length / 2);
  renderSpreadInstant();
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
        # An empty page (0 photos, after a move emptied it) is kept, not
        # dropped - otherwise page indices would shift on the next load.
        pages.append({"photos": photos})

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
            {"photos": [{**p, "src": remap(p["src"])} for p in page.get("photos", [])]}
            for page in album.get("pages", [])
        ],
    }


class AlbumHandler(BaseHTTPRequestHandler):
    album = None
    photo_index = {}
    folder = None

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/":
            html = HTML_PAGE.replace("__API_URL__", "/api/data").replace("__PHOTO_PREFIX__", "/photo/")
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
            state_path = self.folder / STATE_FILENAME
            state_path.write_text(json.dumps(album, indent=2), encoding="utf-8")
        except OSError as e:
            self._send_json({"ok": False, "error": str(e)}, status=500)
            return

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

    html = HTML_PAGE.replace("__API_URL__", "./data.json").replace("__PHOTO_PREFIX__", "./photos/")
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

    port = pick_port(args.host, args.port)
    server = ThreadingHTTPServer((args.host, port), AlbumHandler)

    display_host = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{display_host}:{port}/"
    print(f"Photobook for '{folder.name}' ({album['count']} photos)")
    print(f"Serving at {url}  (Ctrl+C to stop)")

    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
