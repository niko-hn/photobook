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
From WSL, `localhost` URLs work directly in your Windows browser.

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

More features to come.
