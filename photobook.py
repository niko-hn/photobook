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
import mimetypes
import os
import random
import shutil
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

COVER_LAYOUTS = ["1a", "1b"]
LAYOUTS_BY_COUNT = {
    1: ["1a", "1b"],
    2: ["2a", "2b", "2c"],
    3: ["3a", "3b", "3c"],
}


def find_photos(folder: Path):
    photos = []
    for entry in sorted(folder.iterdir()):
        if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS:
            photos.append(entry)
    return photos


def build_album(folder: Path):
    photos = find_photos(folder)
    rng = random.Random()

    cover_choices = photos[:]
    rng.shuffle(cover_choices)
    cover = [p.name for p in cover_choices[:5]]

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
        layout = rng.choice(LAYOUTS_BY_COUNT[size])
        page_photos = [
            {"src": p.name, "rot": round(rng.uniform(-4.5, 4.5), 1)}
            for p in group
        ]
        pages.append({"photos": page_photos, "layout": layout})

    return {
        "folder": folder.name or str(folder),
        "count": len(photos),
        "cover": cover,
        "pages": pages,
    }


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
    display:flex;
    align-items:center;
    justify-content:center;
  }
  #book{
    position:relative;
    display:flex;
    width:min(94vw, 1200px);
    height:min(80vh, 780px);
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
  #lightboxImg{
    position:fixed;
    object-fit:cover;
    box-shadow:0 20px 60px -10px #000000cc;
    cursor:zoom-out;
    transition:
      top .38s cubic-bezier(.3,.7,.3,1),
      left .38s cubic-bezier(.3,.7,.3,1),
      width .38s cubic-bezier(.3,.7,.3,1),
      height .38s cubic-bezier(.3,.7,.3,1),
      transform .38s cubic-bezier(.3,.7,.3,1);
  }

  .page-grid{
    width:100%;
    height:100%;
    display:grid;
    gap:18px;
  }
  /* 1 photo layouts */
  .layout-1a .page-grid, .layout-1b .page-grid{
    grid-template-columns:1fr;
    grid-template-rows:1fr;
    place-items:center;
  }
  .layout-1a .frame{ width:78%; height:78%; }
  .layout-1b .frame{ width:60%; height:92%; }

  /* 2 photo layouts */
  .layout-2a .page-grid{ grid-template-columns:1fr; grid-template-rows:1fr 1fr; place-items:center; }
  .layout-2b .page-grid{ grid-template-columns:1fr 1fr; grid-template-rows:1fr; place-items:center; }
  .layout-2c .page-grid{ grid-template-columns:1fr 1fr; grid-template-rows:1fr; place-items:center; }
  .layout-2a .frame{ width:70%; height:44%; }
  .layout-2b .frame{ width:88%; height:70%; }
  .layout-2c .frame:nth-child(1){ width:70%; height:55%; align-self:start; justify-self:end; }
  .layout-2c .frame:nth-child(2){ width:70%; height:55%; align-self:end; justify-self:start; }

  /* 3 photo layouts */
  .layout-3a .page-grid{ grid-template-columns:1.3fr 1fr; grid-template-rows:1fr 1fr; }
  .layout-3a .frame:nth-child(1){ grid-row:1 / 3; width:92%; height:92%; align-self:center; justify-self:center; }
  .layout-3a .frame:nth-child(2){ width:88%; height:80%; align-self:center; justify-self:center; }
  .layout-3a .frame:nth-child(3){ width:88%; height:80%; align-self:center; justify-self:center; }
  .layout-3b .page-grid{ grid-template-columns:1fr 1fr 1fr; grid-template-rows:1fr; place-items:center; }
  .layout-3b .frame{ width:90%; height:60%; }
  .layout-3c .page-grid{ grid-template-columns:1fr 1fr; grid-template-rows:1fr 1fr; }
  .layout-3c .frame:nth-child(1){ grid-column:1 / 3; width:60%; height:80%; align-self:center; justify-self:center; }
  .layout-3c .frame:nth-child(2){ width:82%; height:75%; align-self:center; justify-self:center; }
  .layout-3c .frame:nth-child(3){ width:82%; height:75%; align-self:center; justify-self:center; }

  .page-label{
    position:absolute;
    bottom:14px;
    font-size:11px;
    color:var(--ink-soft);
    letter-spacing:.08em;
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
    top:50%;
    transform:translateY(-50%);
    width:46px;
    height:46px;
    border-radius:50%;
    border:none;
    background:#ffffff22;
    color:#f4ecdd;
    font-size:20px;
    cursor:pointer;
    display:flex;
    align-items:center;
    justify-content:center;
    z-index:10;
    backdrop-filter:blur(2px);
    transition:opacity .15s, background .15s;
  }
  .nav-btn:hover{ background:#ffffff3a; }
  .nav-btn:disabled{ opacity:0; pointer-events:none; }
  #prevBtn{ left:14px; }
  #nextBtn{ right:14px; }

  #progress{
    position:absolute;
    bottom:18px;
    left:50%;
    transform:translateX(-50%);
    font-size:12px;
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

function frameHtml(photo){
  const rot = photo.rot || 0;
  return `<div class="frame" data-rot="${rot}" style="transform:rotate(${rot}deg)">
            <img src="${PHOTO_PREFIX}${encodeURIComponent(photo.src)}" alt="">
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

function albumPageInner(page){
  const frames = page.photos.map(frameHtml).join('');
  return `<div class="page-grid">${frames}</div>`;
}

// Returns the content ('cls' + 'inner' html) that belongs in a given
// left/right slot of a given spread, independent of what's currently
// rendered - used both for the static book and for flip-animation faces.
function slotAt(spreadIdx, side){
  if (spreadIdx === 0){
    if (side === 'left') return {cls: 'blank', inner: ''};
    return {cls: '', inner: coverInner()};
  }
  const pageIdx = (spreadIdx - 1) * 2 + (side === 'left' ? 0 : 1);
  const page = DATA.pages[pageIdx];
  if (!page) return {cls: 'blank', inner: ''};
  return {cls: 'layout-' + page.layout, inner: albumPageInner(page)};
}

function fullSpreadHTML(spreadIdx){
  const l = slotAt(spreadIdx, 'left');
  const r = slotAt(spreadIdx, 'right');
  return `<div class="page page-left ${l.cls}">${l.inner}</div>` +
         `<div class="page page-right ${r.cls}">${r.inner}</div>`;
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

  bookEl.innerHTML =
    `<div class="page page-left ${staysLeft.cls}">${staysLeft.inner}</div>` +
    `<div class="page page-right ${revealedRight.cls}">${revealedRight.inner}</div>`;

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

  bookEl.innerHTML =
    `<div class="page page-left ${revealedLeft.cls}">${revealedLeft.inner}</div>` +
    `<div class="page page-right ${staysRight.cls}">${staysRight.inner}</div>`;

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
const lightboxImg = document.createElement('img');
lightboxImg.id = 'lightboxImg';
lightboxImg.alt = '';
lightbox.appendChild(lightboxImg);
document.body.appendChild(lightbox);

let lightboxOpen = false;
let lightboxSourceImg = null;
let lightboxSourceRot = 0;

function fitRect(naturalW, naturalH){
  const maxW = window.innerWidth * 0.92;
  const maxH = window.innerHeight * 0.92;
  const ratio = Math.min(maxW / naturalW, maxH / naturalH);
  const w = naturalW * ratio, h = naturalH * ratio;
  return {top: (window.innerHeight - h) / 2, left: (window.innerWidth - w) / 2, width: w, height: h};
}

function openLightbox(imgEl){
  if (animating || lightboxOpen) return;
  lightboxOpen = true;
  lightboxSourceImg = imgEl;
  lightboxSourceRot = parseFloat(imgEl.closest('.frame')?.dataset.rot || '0');

  const r = imgEl.getBoundingClientRect();
  lightboxImg.src = imgEl.src;

  lightboxImg.style.transition = 'none';
  lightboxImg.style.top = r.top + 'px';
  lightboxImg.style.left = r.left + 'px';
  lightboxImg.style.width = r.width + 'px';
  lightboxImg.style.height = r.height + 'px';
  lightboxImg.style.transform = `rotate(${lightboxSourceRot}deg)`;

  lightbox.classList.add('open');
  prevBtn.style.visibility = 'hidden';
  nextBtn.style.visibility = 'hidden';

  void lightboxImg.offsetWidth; // force reflow so the next change transitions

  const onLoad = () => {
    const target = fitRect(lightboxImg.naturalWidth, lightboxImg.naturalHeight);
    lightboxImg.style.transition = '';
    lightboxImg.style.top = target.top + 'px';
    lightboxImg.style.left = target.left + 'px';
    lightboxImg.style.width = target.width + 'px';
    lightboxImg.style.height = target.height + 'px';
    lightboxImg.style.transform = 'rotate(0deg)';
  };
  if (lightboxImg.complete) onLoad();
  else lightboxImg.onload = onLoad;
}

function closeLightbox(){
  if (!lightboxOpen) return;
  lightboxOpen = false;

  const r = lightboxSourceImg ? lightboxSourceImg.getBoundingClientRect() : null;
  lightbox.classList.remove('open');
  prevBtn.style.visibility = '';
  nextBtn.style.visibility = '';

  if (r){
    lightboxImg.style.top = r.top + 'px';
    lightboxImg.style.left = r.left + 'px';
    lightboxImg.style.width = r.width + 'px';
    lightboxImg.style.height = r.height + 'px';
    lightboxImg.style.transform = `rotate(${lightboxSourceRot}deg)`;
  }
}

bookEl.addEventListener('click', (e) => {
  const img = e.target.closest('.frame img');
  if (img) openLightbox(img);
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
  if (e.key === 'Escape'){ closeLightbox(); return; }
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
            body = json.dumps(self.album).encode("utf-8")
            self._send_bytes(body, "application/json")
        elif path.startswith("/photo/"):
            name = path[len("/photo/"):]
            self._serve_photo(name)
        else:
            self.send_error(404, "Not found")

    def _serve_photo(self, name):
        full_path = self.photo_index.get(name)
        if full_path is None or not full_path.is_file():
            self.send_error(404, "Photo not found")
            return
        content_type = mimetypes.guess_type(full_path.name)[0] or "application/octet-stream"
        data = full_path.read_bytes()
        self._send_bytes(data, content_type, cache=True)

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
    album = build_album(folder)

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

    album = build_album(folder)
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
