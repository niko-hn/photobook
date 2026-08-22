# photobook

A single-file Python web app that turns a folder of photos into a flip-through
album. No dependencies beyond the Python standard library.

## Usage

Run it from inside the folder of photos you want to browse (or point it at one
with `--folder`):

```bash
python3 photobook.py
```

Then open the printed URL (default `http://localhost:8000/`) in your browser.
Works the same way from WSL (Windows), Termux (Android - see below), or any
regular desktop terminal: the server and the browser are on the same
machine, so `localhost` just works.

Options:

```
--port PORT      port to listen on (default 8000)
--host HOST      host to bind (default 0.0.0.0)
--folder PATH    folder of photos to serve (default: current folder)
--open           open the album in your default browser automatically
```

Supported image types: `.jpg .jpeg .png .gif .webp .bmp`

## Try it with the sample photos

```bash
cd sample_photos
python3 ../photobook.py --open
```

## Features

- Front cover page (right-hand page of the spread) centered, showing the
  folder name and 5 random photo thumbnails.
- Page-by-page browsing with left/right arrow keys, on-screen arrows, or
  touch swipe.
- Two pages shown at a time (a spread), like an open book.
- Each page holds 1-3 photos in one of several varying layouts, framed like
  real photos.
- Click a photo to zoom it to a full, framed view on black; click again (or
  press Escape) to zoom back out.
- **Edit mode**: click the book's background, or press space, to toggle it.
  Each page then shows a black button (L on the left page, R on the right)
  that auto-relayouts just that page - same photos, new arrangement; press
  L or R on the keyboard instead of clicking if you prefer. Exiting edit
  mode (background click or space again) saves the current layout to
  `photobook_state.json` in the served folder, so it's what you'll see next
  time you run the app there. Photos added to the folder later show up as
  new pages appended at the end, without disturbing anything you've laid
  out. This only works against the live server (`python3 photobook.py`) -
  a static export has nowhere to save to.

More features to come.

## Run on Android (Termux)

No compiled dependencies, so [Termux](https://termux.dev/) is enough - no
Netlify deploy needed just to try something out:

```bash
pkg update && pkg install python git
git clone https://github.com/niko-hn/photobook
cd photobook
python photobook.py --folder sample_photos
```

Open the printed `http://localhost:8000/` in your phone's browser. Skip
`--open` on Termux - there's no browser launcher for Python's `webbrowser`
module to shell out to, so just tap the link yourself.

To browse your own photos instead of the sample set: run
`termux-setup-storage` once (grants Termux access to shared storage), then
point `--folder` at something like `~/storage/dcim/Camera`.

## Static export (Netlify, GitHub Pages, etc.)

`photobook.py` is a live server meant to run against whatever folder you
point it at, so it can't be hosted as-is on a static host like Netlify.
Instead, bake a specific folder's album into a self-contained static site:

```bash
python3 photobook.py --folder sample_photos --export dist
```

This writes `dist/index.html`, `dist/data.json` and `dist/photos/` - open
`dist/index.html` directly, or deploy the `dist` folder anywhere that
serves static files. It's the same app, minus the ability to browse a
different folder without re-exporting.

### Netlify

This repo includes a `netlify.toml` that runs the export above at build
time and publishes the result:

```toml
[build]
  command = "python3 photobook.py --folder sample_photos --export dist"
  publish = "dist"
```

In the Netlify dashboard: **Add new site → Import an existing project**,
point it at this repo, and it will build and deploy automatically (change
`--folder sample_photos` in `netlify.toml` to publish a different folder
of photos instead).

