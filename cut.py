#!/usr/bin/env python3
"""cutai: Claude plant den Schnitt, dieses Skript baut ihn. Arbeitet im aktuellen Ordner (out/).

  scan  ORDNER          -> out/clips.json + out/sheets/*.jpg (Kontaktbögen alle 2 s)
  energy -              -> out/energy.json (Bewegung/Helligkeit je 0,2 s, für "in": "auto")
  beats SONG            -> BPM, Beat-Raster, Energie je Takt (für Schnitte auf den Beat)
  build PLAN.json       -> out/NAME.fcpxml (Resolve-Import) + NAME_preview.mp4 + NAME_storyboard.jpg
  view  PORT            -> Live-Ansicht http://localhost:PORT (liest out/status.json alle 2 s)

Clip-Namen im Plan: Dateiname ohne Endung oder ein eindeutiger Teil davon ("0594" für DJI_..._0594_D.MP4).
Plan-Format: siehe examples/. Nur stdlib + ffmpeg.
"""
import array, functools, http.server, json, math, re, subprocess, sys, time
from fractions import Fraction
from pathlib import Path

OUT = Path.cwd() / "out"
FPS = Fraction(30000, 1001)  # ponytail: FCPXML/Vorschau fest auf 29,97 DF (DJI), aus ffprobe lesen wenn andere Kameras
VIDEO = {".mp4", ".mov", ".m4v", ".mkv", ".mxf"}


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def probe(f):
    j = json.loads(sh(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(f)]))
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    tc = j["format"].get("tags", {}).get("timecode") or v.get("tags", {}).get("timecode") or "00:00:00;00"
    return {"file": str(f), "dur": float(j["format"]["duration"]), "w": v["width"], "h": v["height"], "tc": tc}


def tc_frames(tc):
    """Drop-Frame-Timecode (29.97) -> Framenummer."""
    h, m, s, f = map(int, tc.replace(";", ":").split(":"))
    mins = h * 60 + m
    return (h * 3600 + m * 60 + s) * 30 + f - 2 * (mins - mins // 10)


def r(frames):  # FCPXML-Zeitangabe
    return f"{frames * 1001}/30000s"


def log(msg, **kw):
    """Ausgabe + out/status.json für die Live-Ansicht (atomar geschrieben)."""
    print(msg, flush=True)
    f = OUT / "status.json"
    try:
        st = json.loads(f.read_text())
    except (OSError, ValueError):
        st = {}
    st.update(kw, msg=msg, t=time.time())
    OUT.mkdir(exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    tmp.replace(f)


class View(http.server.SimpleHTTPRequestHandler):
    """Statische Dateien aus dem Projektordner, / = view.html, Range für Video (Safari)."""

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            body = (Path(__file__).resolve().parent / "view.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        f = Path(self.translate_path(self.path))
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if not (m and f.is_file()):
            return super().do_GET()
        size = f.stat().st_size
        a = int(m[1]) if m[1] else max(0, size - int(m[2] or 0))
        b = min(int(m[2]), size - 1) if m[1] and m[2] else size - 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(f)))
        self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.send_header("Content-Length", str(b - a + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with f.open("rb") as fh:
            fh.seek(a)
            self.wfile.write(fh.read(b - a + 1))

    def log_message(self, *a):
        pass


def view(port):
    print(f"Live-Ansicht: http://localhost:{port}  (Strg+C beendet)", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", int(port)), functools.partial(View, directory=str(Path.cwd()))).serve_forever()


def clips():
    return {Path(c["file"]).stem: c for c in json.loads((OUT / "clips.json").read_text())}


def clip(cl, k):
    """Clip per vollem Namen oder eindeutigem Namensteil (getrennt durch _ - . Leerzeichen)."""
    if k in cl:
        return cl[k]
    hits = [c for stem, c in cl.items() if k in re.split(r"[_\-. ]", stem)]
    if len(hits) != 1:
        sys.exit(f"Clip '{k}': {len(hits)} Treffer in out/clips.json")
    return hits[0]


# ---------- scan ----------
def scan(folder):
    (OUT / "sheets").mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in Path(folder).expanduser().iterdir() if f.suffix.lower() in VIDEO and not f.name.startswith("."))
    info = [probe(f) for f in files]
    log(f"scan: {len(info)} Clips in {folder}", done=0, total=len(info))
    (OUT / "clips.json").write_text(json.dumps(info, indent=1))
    for i, c in enumerate(info):
        stem = Path(c["file"]).stem
        if list((OUT / "sheets").glob(f"{stem}_*.jpg")):
            continue
        vf = "fps=1/2,scale=-2:180,tile=6x5:padding=2"  # Kachel k (0-basiert) in Bogen NN = (NN-1)*60 + 2k s
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-hwaccel", "videotoolbox", "-skip_frame", "nokey",
                        "-i", c["file"], "-vf", vf, "-fps_mode", "vfr", str(OUT / "sheets" / f"{stem}_%02d.jpg")])
        log(f"Kontaktbogen {stem} ({c['dur']:.0f}s, {c['w']}x{c['h']})", done=i + 1)


# ---------- energy ----------
def energy(_):
    """Bewegung (mittlere Differenz aufeinanderfolgender Frames) + Helligkeit je 0,2 s -> out/energy.json."""
    res = json.loads((OUT / "energy.json").read_text()) if (OUT / "energy.json").exists() else {}
    cl = clips()
    for i, (k, c) in enumerate(cl.items()):
        if k in res:
            continue
        tmp = OUT / "_m.txt", OUT / "_l.txt"
        vf = ("fps=5,scale=160:-2,format=gray,split[a][b];"
              f"[b]tblend=all_mode=difference,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file={tmp[0]}[d];"
              f"[a]signalstats,metadata=print:key=lavfi.signalstats.YAVG:file={tmp[1]}[l];[d][l]hstack")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-hwaccel", "videotoolbox", "-i", c["file"],
                        "-filter_complex", vf, "-f", "null", "-"])
        m, l = ([float(x.split("=")[1]) for x in t.read_text().splitlines() if "YAVG" in x] for t in tmp)
        res[k] = {"motion": [round(v, 1) for v in m], "luma": [round(v) for v in l]}  # je 0,2 s, 0-255
        (OUT / "energy.json").write_text(json.dumps(res))
        m = res[k]["motion"]
        log(f"Energie {k}: max. Bewegung {max(m, default=0)}", done=i + 1, total=len(cl))


# ---------- beats ----------
def beats(song):
    sr, hop = 11025, 256
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", song, "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                         check=True, capture_output=True).stdout
    a = array.array("h", raw)
    env = [math.log1p(sum(x * x for x in a[i:i + hop]) / hop) for i in range(0, len(a) - hop, hop)]
    onset = [max(0.0, env[i] - env[i - 1]) for i in range(1, len(env))]
    fr = sr / hop  # Envelope-Frames pro Sekunde
    # BPM per Autokorrelation, 85-175 BPM, leichte Vorliebe für 120-130 (Club)
    best = max(range(int(fr * 60 / 175), int(fr * 60 / 85) + 1),
               key=lambda L: sum(onset[i] * onset[i + L] for i in range(len(onset) - L))
               * math.exp(-0.5 * math.log2((fr * 60 / L) / 126) ** 2 / 0.5))
    bpm = fr * 60 / best
    # Phase: Offset, auf dem die Onsets am stärksten liegen
    phase = max(range(best), key=lambda p: sum(onset[p::best]))
    first = (phase + 1) / fr
    spb = 60 / bpm
    dur = len(a) / sr
    bars = []  # Lautheit je Takt (4 Beats) -> Drops finden
    for b in range(int((dur - first) / (4 * spb))):
        i0, i1 = int((first + b * 4 * spb) * fr), int((first + (b + 1) * 4 * spb) * fr)
        bars.append(round(sum(env[i0:i1]) / max(1, i1 - i0), 2))
    res = {"song": song, "bpm": round(bpm, 2), "first_beat": round(first, 3), "duration": round(dur, 2), "bar_energy": bars}
    print(json.dumps(res))
    log(f"Beats {Path(song).name}: {res['bpm']} BPM, erster Beat {res['first_beat']} s")
    return res


# ---------- build ----------
def timeline(plan):
    """Schnittpunkte frame-genau auf dem Beat (kumulativ gerundet, kein Drift)."""
    spb = 60 / plan["bpm"]
    t, out = 0, []
    for s in plan["shots"]:
        start = round(t * spb * FPS)
        t += s["beats"]
        out.append({**s, "pos": start, "len": round(t * spb * FPS) - start})
    return out


def fcpxml(plan, shots, cl):
    W, H = (1080, 1920) if plan.get("format", "9x16") == "9x16" else (1920, 1080)
    total = sum(s["len"] for s in shots)
    song_in = round(plan["song_in"] * FPS)
    used = sorted({s["clip"] for s in shots})
    res = [f'<format id="r1" frameDuration="1001/30000s" width="{W}" height="{H}" colorSpace="1-1-1 (Rec. 709)"/>']
    ids = {}
    for n, k in enumerate(used, start=2):
        c, ids[k] = clip(cl, k), f"r{n}"
        res.append(f'<asset id="r{n}" name="{Path(c["file"]).name}" src="file://{c["file"]}" start="{r(tc_frames(c["tc"]))}" '
                   f'duration="{r(int(c["dur"] * FPS))}" hasVideo="1" hasAudio="1" audioSources="1" audioChannels="2" audioRate="48000"/>')
    song = Path(plan["song"]).expanduser()
    sd = float(probe_audio(song))
    res.append(f'<asset id="rS" name="{song.name}" src="file://{song}" start="0s" duration="{r(int(sd * FPS))}" '
               'hasAudio="1" audioSources="1" audioChannels="2" audioRate="48000"/>')
    spine = []
    for i, s in enumerate(shots):
        c = clip(cl, s["clip"])
        st = tc_frames(c["tc"]) + round(s["in"] * FPS)
        song_xml = (f'<asset-clip name="{song.name}" ref="rS" lane="-1" offset="{r(st)}" start="{r(song_in)}" '
                    f'duration="{r(total)}" audioRole="music"/>') if i == 0 else ""
        mark = f'<marker start="{r(st)}" duration="{r(1)}" value="FLASH"/>' if s.get("fx") == "flash" else ""
        spine.append(f'<asset-clip name="{s["clip"]}" ref="{ids[s["clip"]]}" offset="{r(s["pos"])}" start="{r(st)}" '
                     f'duration="{r(s["len"])}" srcEnable="video" tcFormat="DF"><adjust-conform type="fill"/>{mark}{song_xml}</asset-clip>')
    nl = "\n      "
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.8">
  <resources>
      {nl.join(res)}
  </resources>
  <library><event name="cutai"><project name="{plan["name"]}">
    <sequence format="r1" duration="{r(total)}" tcStart="0s" tcFormat="DF" audioLayout="stereo" audioRate="48k"><spine>
      {nl.join(spine)}
    </spine></sequence>
  </project></event></library>
</fcpxml>
'''


def probe_audio(f):
    return sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)]).strip()


def preview(plan, shots, cl, mp4):
    W, H = (540, 960) if plan.get("format", "9x16") == "9x16" else (960, 540)
    args, fl = ["ffmpeg", "-v", "error", "-y"], []
    for i, s in enumerate(shots):
        args += ["-hwaccel", "videotoolbox", "-ss", f"{s['in']:.3f}", "-i", clip(cl, s["clip"])["file"]]
        f = (f"[{i}:v]fps=30000/1001,scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
             f"trim=end_frame={s['len']},setpts=PTS-STARTPTS")
        if s.get("fx") == "flash":
            f += ",fade=t=in:st=0:d=0.25:color=white"
        fl.append(f + f"[v{i}]")
    n, total = len(shots), sum(s["len"] for s in shots) / FPS
    args += ["-ss", str(plan["song_in"]), "-i", str(Path(plan["song"]).expanduser())]
    fl.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0,format=yuv420p[v]")
    fl.append(f"[{n}:a]atrim=0:{float(total):.3f},afade=t=out:st={float(total) - 1.5:.3f}:d=1.5[a]")
    args += ["-filter_complex", ";".join(fl), "-map", "[v]", "-map", "[a]",
             "-c:v", "h264_videotoolbox", "-b:v", "6M", "-c:a", "aac", "-b:a", "192k", str(mp4)]
    subprocess.run(args, check=True)


def storyboard(plan, shots, mp4, jpg):
    sel = "+".join(f"eq(n\\,{s['pos'] + min(5, s['len'] // 2)})" for s in shots)
    cols = 6
    rows = math.ceil(len(shots) / cols)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp4), "-vf",
                    f"select='{sel}',scale=-2:320,tile={cols}x{rows}:padding=4", "-frames:v", "1", "-fps_mode", "vfr", str(jpg)], check=True)


def build(plan_path):
    plan = json.loads(Path(plan_path).read_text())
    cl = clips()
    for s in plan["shots"]:  # Plan gegen Clip-Längen prüfen
        c = clip(cl, s["clip"])
        need = s["in"] + s["beats"] * 60 / plan["bpm"]
        if need > c["dur"]:
            sys.exit(f"Shot {s['clip']} @ {s['in']}s braucht {need:.1f}s, Clip hat nur {c['dur']:.1f}s")
    shots = timeline(plan)
    base = OUT / plan["name"]
    base.with_suffix(".fcpxml").write_text(fcpxml(plan, shots, cl))
    mp4 = OUT / f"{plan['name']}_preview.mp4"
    preview(plan, shots, cl, mp4)
    storyboard(plan, shots, mp4, OUT / f"{plan['name']}_storyboard.jpg")
    log(f"{len(shots)} Shots, {float(sum(s['len'] for s in shots) / FPS):.1f}s -> {base.name}.fcpxml / {mp4.name}",
        video=mp4.name, sheet=f"{plan['name']}_storyboard.jpg", vt=time.time())


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    {"scan": scan, "energy": energy, "beats": beats, "build": build, "view": view}[sys.argv[1]](" ".join(sys.argv[2:]))
