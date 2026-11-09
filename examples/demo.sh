#!/bin/sh
# Demo without your own footage: generates 4 synthetic clips + a 120 BPM beat, then renders a 9:16 reel.
#   sh examples/demo.sh [folder]        (default: ./cutai-demo)
#   then: cd cutai-demo && python3 <cutai>/cut.py view 8765   ->  http://localhost:8765
set -e
R=$(cd "$(dirname "$0")/.." && pwd)
D=${1:-cutai-demo}
mkdir -p "$D/clips" && cd "$D"
clip() { [ -f "clips/$1.mp4" ] || ffmpeg -v error -y -f lavfi -i "$2" -t 8 -c:v libx264 -pix_fmt yuv420p "clips/$1.mp4"; }
clip fractal "mandelbrot=s=960x540:rate=30:start_scale=1.5:end_scale=0.02"
clip life    "life=s=160x90:rate=30:mold=8:ratio=0.3:life_color=#ff3020:death_color=#3040a0,scale=960:540:flags=neighbor"
clip bars    "testsrc2=s=960x540:rate=30"
clip waves   "gradients=s=960x540:rate=30:speed=0.03:c0=#ff0066:c1=#00ccff:c2=#ffcc00:n=3"
[ -f song.wav ] || ffmpeg -v error -y -f lavfi -i "aevalsrc='0.9*sin(2*PI*55*t)*exp(-25*mod(t,0.5))+0.2*sin(2*PI*880*t)*exp(-60*mod(t+0.25,0.5))':s=44100:d=20" song.wav
python3 "$R/cut.py" scan clips
python3 "$R/cut.py" beats song.wav >/dev/null
python3 "$R/reel.py" "$R/examples/demo.json" ${LOOK_ONLY:+--look}
