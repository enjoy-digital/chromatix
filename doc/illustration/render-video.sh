#!/bin/sh
# Render the ChromatiX promo video + hero stills (deterministic, frame by frame).
# Needs: google-chrome, node, ffmpeg, imagemagick. three.js is loaded from jsDelivr.
# Usage: ./render-video.sh [width] [height] [fps]
set -e
W=${1:-1920}; H=${2:-1080}; FPS=${3:-30}; PORT=8767
cd "$(dirname "$0")"
[ -d node_modules/puppeteer-core ] || PUPPETEER_SKIP_DOWNLOAD=1 npm install --silent
python3 -m http.server $PORT >/dev/null 2>&1 & SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
sleep 1
URL=http://localhost:$PORT/video.html
node capture.js "$URL" chromatix.mp4 $W $H $FPS
node sheet.js "$URL?still"           hero 3200 1800 0 && mv hero_00000.png chromatix-hero.png
node sheet.js "$URL?still&overlay=0" hero 3200 1800 0 && mv hero_00000.png chromatix-hero-notext.png
