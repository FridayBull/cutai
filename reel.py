#!/usr/bin/env python3
"""cutai reel: fertiges Video aus einem Plan, Schnitte auf den Beat, Effekte, Look. Vorher: cut.py scan (+ energy).

  python3 reel.py plan.json          -> out/NAME.mp4 (+ NAME_sheet.jpg zur Kontrolle)
  python3 reel.py plan.json --look   -> nur 1 Bild je Shot, Vorher/Nachher in der Live-Ansicht (Look in Sekunden prüfen)

Plan: {"name", "song", "song_in": s, "fade": s, "size": [w,h], "fps": 30,
       "cuts": [s...] (Schnittzeiten inkl. 0 und Ende) ODER "bpm" + je Shot "beats",
       "shots": [{"clip": "0594", "in": 30 | "auto", "mask": "fg"|"person"|"none", "zoom": 0.06,
                   "frame": [cx, cy, vergrößerung] | "auto" (Smart Reframe, Standard bei Hochformat),
                   "tight": vergrößerung bei auto, "flash": false (harter Schnitt ohne Silhouette),
                   "fx": ["zoom", "shake", "rgb", "flash"] (Übergang in diesen Shot), "bw": true,
                   "fit": true (ganzes Bild + unscharfer Hintergrund)}],
       "people_bw": sättigung (Personen per Vision entsättigen, Raum bleibt farbig),
       "song_parts": [[a, b], ...] (Song aus Teilen, z.B. Takte wiederholen),
       "look": "VORLAGE" (looks/VORLAGE.json) | {"preset": "VORLAGE", ...eigene Werte} (alle Felder: LOOK unten),
       je Shot "look": {...} überschreibt einzelne Werte, "silhouette_color": "white", "flash_frames": 2}
Nur stdlib + ffmpeg + ./segment und ./people (Apple Vision, macOS 14+).
"""
import json, shutil, subprocess, sys, time
from pathlib import Path

from cut import OUT, clip, clips, log

ROOT = Path(__file__).resolve().parent
FPS = 30  # Standard, Plan-Feld "fps" überschreibt
FLASH = 2  # Frames je Seite des Schnitts mit Silhouette (Plan: "flash_frames")
LOOKS = ROOT / "looks"

# Standard-Look (= looks/cinematic.json): entrauschen, Schatten absenken, kühle Schatten / warme Lichter, Vignette, Korn
LOOK = {"denoise": 1.5, "sharpen": 0.7, "lut": None, "curve": [[0, 0], [0.1, 0.08], [0.5, 0.52], [0.85, 0.89], [1, 0.97]],
        "shadows": [-0.04, 0.01, 0.07], "highlights": [0.05, 0, -0.04], "hold": None,
        "saturation": 1.05, "contrast": 1, "brightness": 0, "filter": None, "vignette": True, "grain": 5, "bw": False}


def resolve_look(plan, shot=None):
    """Look = Standard + Vorlage (looks/NAME.json) + eigene Werte aus Plan und Shot.
    Alte Felder (look_filter, saturation, contrast) wirken wie früher."""
    lk = plan.get("look", {})
    lk = {"preset": lk} if isinstance(lk, str) else dict(lk)
    base = json.loads((LOOKS / f"{lk.pop('preset')}.json").read_text()) if "preset" in lk else {}
    old = {k: plan[k] for k in ("saturation", "contrast") if k in plan}
    if "look_filter" in plan:  # früher: ersetzte den eq-Schritt
        old = {"saturation": 1, "contrast": 1, "brightness": 0, "filter": plan["look_filter"]}
    L = {**LOOK, **base, **old, **lk, **((shot or {}).get("look") or {})}
    if shot and shot.get("bw"):
        L["bw"] = True
    return L


def grade(L):
    """Look -> ffmpeg-Filterkette, Reihenfolge wie beim Color-Grading."""
    g = lambda v: f"{v:g}"
    f = []
    if L["denoise"]:
        d = L["denoise"]
        f.append(f"hqdn3d={g(d)}:{g(d)}:{g(2 * d)}:{g(2 * d)}")
    if L["sharpen"]:
        f.append(f"cas={g(L['sharpen'])}")
    if L["lut"]:  # .cube-Datei, z. B. aus DaVinci exportiert
        f.append(f"lut3d=file='{Path(L['lut']).expanduser()}'")
    if L["curve"] and L["curve"] != [[0, 0], [1, 1]]:
        f.append("curves=m='" + " ".join(f"{g(a)}/{g(b)}" for a, b in L["curve"]) + "'")
    cb = [f"{k}={g(v)}" for k, v in zip(("rs", "gs", "bs", "rh", "gh", "bh"), [*L["shadows"], *L["highlights"]]) if v]
    if cb:
        f.append("colorbalance=" + ":".join(cb))
    if L["hold"]:  # nur diese Farbe bleibt, Rest schwarz-weiß
        h = L["hold"]
        f.append(f"colorhold=color={h['color'].replace('#', '0x')}:similarity={g(h.get('similarity', 0.1))}:blend={g(h.get('blend', 0.03))}")
    if (L["saturation"], L["contrast"], L["brightness"]) != (1, 1, 0):
        f.append(f"eq=saturation={g(L['saturation'])}:contrast={g(L['contrast'])}" + (f":brightness={g(L['brightness'])}" if L["brightness"] else ""))
    if L["filter"]:
        f.append(L["filter"])
    if L["vignette"]:
        f.append("vignette=PI/5" if L["vignette"] is True else f"vignette=angle={g(L['vignette'])}")
    if L["grain"]:
        f.append(f"noise=alls={g(L['grain'])}:allf=t")
    if L["bw"]:
        f.append("hue=s=0")
    return ",".join(f) or "null"


GRADE = grade(LOOK)


def sh(*cmd):
    subprocess.run(cmd, check=True)


def best_in(key, dur, clip_dur):
    """Start des bewegungsreichsten Fensters (energy.json, Raster 0,2 s)."""
    f = OUT / "energy.json"
    e = f.exists() and json.loads(f.read_text()).get(key)
    if not e:
        return 0.0
    m, w = e["motion"], int(dur * 5)
    last = max(0, min(len(m), int((clip_dur - dur) * 5)) - w)
    i = max(range(last + 1), key=lambda i: sum(m[i:i + w]))
    return i / 5


def centroid(c, t, mode, tmp):
    """Schwerpunkt (x, y in 0-1) der Person(en) im Frame bei t s, None wenn keine."""
    jpg = tmp / "probe.jpg"
    sh("ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", c["file"], "-frames:v", "1",
       "-vf", "scale=640:640:force_original_aspect_ratio=decrease", str(jpg))
    out = subprocess.run([str(ROOT / "segment")] + (["--fg"] if mode == "fg" else []) + [str(jpg)],
                         capture_output=True, text=True).stdout
    mask = jpg.with_suffix(".mask.pgm")
    if "nichts" in out or not mask.exists():
        return None
    raw = mask.read_bytes()
    parts = raw.split(b"\n", 3)  # P5 / w h / 255 / Daten
    w, h = map(int, parts[1].split())
    px = parts[3]
    tot = sx = sy = 0
    for y in range(0, h, 2):
        row = px[y * w:(y + 1) * w]
        for x in range(0, w, 2):
            v = row[x]
            if v > 128:
                tot += v; sx += v * x; sy += v * y
    mask.unlink()
    return (sx / tot / w, sy / tot / h) if tot else None


def reframe(c, s, dur, mode, tmp):
    """Smart Reframe: Personen-Schwerpunkt bei Start/Mitte/Ende -> Ausschnitt folgt linear."""
    pts = [centroid(c, s["in"] + f * dur, mode, tmp) for f in (0.05, 0.5, 0.95)]
    pts = [p or next((q for q in pts if q), (0.5, 0.45)) for p in pts]
    return [round(p[0], 3) for p in pts], round(sum(p[1] for p in pts) / 3, 3)


def render_shot(c, s, n, W, H, frames_dir, idx, grade=GRADE):
    """n Frames ab s["in"] -> frames_dir/s{idx}_%04d.jpg (Ausschnitt, Look, Push-in)."""
    z = s.get("zoom", 0.05) / max(1, n)
    dur = n / FPS
    fr = s.get("frame", "auto" if H > W else [0.5, 0.5, 1])
    if fr == "auto":
        cxs, cy = reframe(c, s, dur, s.get("mask", "person") if s.get("mask") != "none" else "person", frames_dir)
        fz = s.get("tight", 1)
        s["frame_used"] = [cxs, cy, fz]
    else:
        cxs, cy, fz = [fr[0]] * 3, fr[1], fr[2]
    h2 = dur / 2
    cx = f"if(lt(t,{h2:.3f}),{cxs[0]}+({cxs[1] - cxs[0]:.3f})*t/{h2:.3f},{cxs[1]}+({cxs[2] - cxs[1]:.3f})*(t-{h2:.3f})/{h2:.3f})"
    cw = f"min(iw,ih*{W}/{H})/{fz}"
    if s.get("fit"):  # ganzes Bild mittig, dahinter dieselbe Szene groß + unscharf (Gruppen-Selfies)
        place = (f"split[a][b];[b]scale={2 * W}:{2 * H}:force_original_aspect_ratio=increase,crop={2 * W}:{2 * H},"
                 f"gblur=sigma=40,eq=brightness=-0.08[bg];[a]scale={round(2 * W * s.get('fit_zoom', 1.35))}:-2:flags=lanczos,crop={2 * W}:ih[fg];[bg][fg]overlay=0:(H-h)/2,")
    else:
        place = (f"crop='{cw}':'{cw}*{H}/{W}':'min(max(({cx})*iw-ow/2,0),iw-ow)':'min(max({cy}*ih-oh/2,0),ih-oh)',"
                 f"scale={2 * W}:{2 * H}:flags=lanczos,")
    # ein Durchlauf: Frames mit Look + mittlerer Frame ohne Look (Vorher/Nachher in der Live-Ansicht)
    fc = (f"[0:v]fps={FPS},{place}"
          f"zoompan=z='1+{z:.6f}*on':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={W}x{H}:fps={FPS},split[g][r];"
          f"[g]{grade},format=yuvj420p[o];[r]select='eq(n,{n // 2})',format=yuvj420p[raw]")
    sh("ffmpeg", "-v", "error", "-y", "-hwaccel", "videotoolbox", "-ss", f"{s['in']:.3f}", "-i", c["file"],
       "-filter_complex", fc, "-map", "[o]", "-frames:v", str(n), "-q:v", "2", str(frames_dir / f"s{idx:02d}_%04d.jpg"),
       "-map", "[raw]", "-frames:v", "1", "-q:v", "2", str(frames_dir / f"r{idx:02d}.jpg"))
    got = sorted(frames_dir.glob(f"s{idx:02d}_*.jpg"))
    if len(got) < n:  # Clip zu kurz: letzten Frame halten
        for k in range(len(got), n):
            shutil.copy(got[-1], frames_dir / f"s{idx:02d}_{k + 1:04d}.jpg")
    return sorted(frames_dir.glob(f"s{idx:02d}_*.jpg"))


def silhouette(jpg, mode, color="white"):
    """Person(en) im Bild mit einer Farbe füllen (Maske von Apple Vision, Kanten leicht gehärtet)."""
    args = [str(ROOT / "segment")] + (["--fg"] if mode == "fg" else []) + [str(jpg)]
    if "nichts" in subprocess.run(args, capture_output=True, text=True).stdout:
        return
    mask = jpg.with_suffix(".mask.pgm")
    tmp = jpg.with_name("fx_" + jpg.name)
    sh("ffmpeg", "-v", "error", "-y", "-i", str(jpg), "-i", str(mask), "-filter_complex",
       "[1][0]scale2ref[m][v];[m]format=gray,lutyuv=y='clip((val-50)*1.6,0,255)'[m2];"
       f"[v]split[v1][v2];[v2]drawbox=c={color}:t=fill[w];[w][m2]alphamerge[wm];[v1][wm]overlay",
       "-q:v", "2", str(tmp))
    tmp.replace(jpg)
    mask.unlink()


def fx_frame(jpg, vf):
    tmp = jpg.with_name("fx_" + jpg.name)
    sh("ffmpeg", "-v", "error", "-y", "-i", str(jpg), "-vf", vf, "-q:v", "2", str(tmp))
    tmp.replace(jpg)


def transition(out_frames, in_frames, fx, W, H, color="white"):
    """Übergangs-Effekte am Schnitt: zoom (Zoom-Through), shake, rgb (Farbversatz), flash (Weißblitz)."""
    if "zoom" in fx:  # alter Shot zoomt rein, neuer zoomt raus, mit Unschärfe
        for f, (z, b) in zip(out_frames[-4:], [(1.06, 0), (1.15, 2), (1.3, 4), (1.5, 7)]):
            fx_frame(f, f"crop=iw/{z}:ih/{z},scale={W}:{H},gblur=sigma={b}" if b else f"crop=iw/{z}:ih/{z},scale={W}:{H}")
        for f, (z, b) in zip(in_frames[:4], [(1.5, 7), (1.3, 4), (1.15, 2), (1.06, 0)]):
            fx_frame(f, f"crop=iw/{z}:ih/{z},scale={W}:{H},gblur=sigma={b}" if b else f"crop=iw/{z}:ih/{z},scale={W}:{H}")
    if "shake" in fx:
        for f, (dx, dy) in zip(in_frames[:6], [(28, -20), (-22, 16), (16, 12), (-10, -8), (6, 5), (-3, -2)]):
            fx_frame(f, f"scale=iw*1.08:-2,crop={W}:{H}:(iw-{W})/2+{dx}:(ih-{H})/2+{dy}")
    if "rgb" in fx:
        for f, d in zip(in_frames[:5], [16, 11, 7, 4, 2]):
            fx_frame(f, f"rgbashift=rh=-{d}:bh={d}:rv={d // 2}:bv=-{d // 2}")
    if "flash" in fx:
        for f, a in zip(in_frames[:3], [0.85, 0.5, 0.2]):
            fx_frame(f, f"drawbox=c={color}@{a}:t=fill")


def song_audio(plan, frames_dir):
    """Song (optional aus Teilen zusammengesetzt, z.B. Takte wiederholt) -> wav."""
    song = str(Path(plan["song"]).expanduser())
    parts = plan.get("song_parts")
    if not parts:
        return song, plan.get("song_in", 0)
    wav = frames_dir / "song.wav"
    fl = "".join(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS,afade=t=in:d=0.01,afade=t=out:st={b - a - 0.01:.3f}:d=0.01[p{i}];"
                 for i, (a, b) in enumerate(parts))
    fl += "".join(f"[p{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[a]"
    sh("ffmpeg", "-v", "error", "-y", "-i", song, "-filter_complex", fl, "-map", "[a]", str(wav))
    return str(wav), 0


def build(plan_path, look_only=False):
    global FPS
    plan = json.loads(Path(plan_path).read_text())
    W, H = plan.get("size", [1920, 1080])
    FPS = plan.get("fps", FPS)
    cl = clips()
    frames_dir = OUT / f"{plan['name']}_frames"
    shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir(parents=True)
    rel = lambda f: f"out/{frames_dir.name}/{f.name}"  # Pfad für die Live-Ansicht
    log(f"Plan {plan['name']}: {len(plan['shots'])} Shots", name=plan["name"], done=0,
        total=len(plan["shots"]) + (0 if look_only else 3), frame=None, raw=None, thumbs=[])
    if "bpm" in plan:  # Schnitte in Beats: cuts = kumulierte Beats * Beatlänge
        t, sec = 0, [0.0]
        for sh_ in plan["shots"]:
            t += sh_["beats"]
            sec.append(t * 60 / plan["bpm"])
    else:
        sec = plan["cuts"]
    cuts = [round(t * FPS) for t in sec]
    shots = []
    for i, s in enumerate(plan["shots"]):
        n = cuts[i + 1] - cuts[i]
        c = clip(cl, s["clip"])
        if s.get("in", "auto") == "auto":
            s["in"] = best_in(Path(c["file"]).stem, n / FPS, c["dur"])
        L = resolve_look(plan, s)
        if look_only:  # nur der mittlere Frame des Shots
            s = {**s, "in": s["in"] + n / FPS / 2}
        shots.append(render_shot(c, s, 1 if look_only else n, W, H, frames_dir, i, grade(L)))
        mid = rel(shots[-1][len(shots[-1]) // 2])
        log(f"Shot {i + 1}: {s['clip']} ab {s['in']:.1f}s, {n} Frames {s.get('frame_used', '')}", done=i + 1,
            frame=mid, raw=rel(frames_dir / f"r{i:02d}.jpg"), look=L,
            thumbs=[[rel(x[len(x) // 2]), rel(frames_dir / f"r{j:02d}.jpg")] for j, x in enumerate(shots)])
    if look_only:
        return log("Look-Vorschau fertig")
    if "people_bw" in plan:  # Personen (exakte Vision-Kontur) schwarz-weiß, Raum bleibt farbig
        log(subprocess.run([str(ROOT / "people"), str(plan["people_bw"])] +
                             [str(f) for s, sp in zip(shots, plan["shots"]) if sp.get("people_bw", True) for f in s],
                             capture_output=True, text=True, check=True).stdout.strip())
    log("Silhouetten und Übergänge", done=len(shots) + 1, frame=rel(shots[1][0]) if len(shots) > 1 else None, raw=None)
    # Silhouette: letzte k Frames vor und erste k Frames nach jedem Schnitt
    k, color = plan.get("flash_frames", FLASH), plan.get("silhouette_color", "white")
    for i in range(len(shots) - 1):
        if plan["shots"][i + 1].get("flash", True) is False:  # harter Schnitt ohne Silhouette
            continue
        for jpg, s in [(f, plan["shots"][i]) for f in shots[i][-k:]] + \
                      [(f, plan["shots"][i + 1]) for f in shots[i + 1][:k]] if k else []:
            if s.get("mask", "person") != "none":
                silhouette(jpg, s.get("mask", "person"), color)
    for i in range(len(shots) - 1):
        if plan["shots"][i + 1].get("fx"):
            transition(shots[i], shots[i + 1], plan["shots"][i + 1]["fx"], W, H, color)
    seq = OUT / f"{plan['name']}_seq.txt"
    seq.write_text("".join(f"file '{f}'\nduration {1 / FPS:.6f}\n" for s in shots for f in s))
    mp4 = OUT / f"{plan['name']}.mp4"
    song, song_in = song_audio(plan, frames_dir)
    total = cuts[-1] / FPS
    log("Video kodieren", done=len(shots) + 2)
    sh("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(seq),
       "-ss", str(song_in), "-i", song, "-map", "0:v", "-map", "1:a",
       "-vf", f"fps={FPS},format=yuv420p",
       "-af", f"afade=t=out:st={total - plan.get('fade', 0.01):.3f}:d={plan.get('fade', 0.01)}", "-c:v", "libx264", "-preset", "slow", "-crf", "16",
       "-c:a", "aac", "-b:a", "256k", "-t", f"{total:.3f}", "-movflags", "+faststart", str(mp4))
    # Kontrollbogen: Frame vor/nach jedem Schnitt + Shot-Mitte
    pick = [f for s in shots for f in (s[0], s[len(s) // 2], s[-1])]
    per_row = 1 if W > H else 3  # Shots pro Zeile
    rows = -(-len(shots) // per_row)
    pick += [pick[-1]] * (rows * per_row * 3 - len(pick))
    sh("ffmpeg", "-v", "error", "-y", *sum((["-i", str(f)] for f in pick), []), "-filter_complex",
       "".join(f"[{k}]scale={320 if W > H else 120}:-2[p{k}];" for k in range(len(pick))) +
       "".join(f"[p{k}]" for k in range(len(pick))) + f"xstack=inputs={len(pick)}:grid={per_row * 3}x{rows}",
       "-frames:v", "1", str(OUT / f"{plan['name']}_sheet.jpg"))
    shutil.rmtree(frames_dir)  # Zwischenbilder (~200 MB) weg, aus dem Plan jederzeit neu baubar
    log(f"fertig: {mp4.name} ({total:.1f}s)", done=len(shots) + 3, video=mp4.name, sheet=f"{plan['name']}_sheet.jpg", vt=time.time(),
        vcuts=[round(c / FPS, 3) for c in cuts],
        frame=None, raw=None, thumbs=[])


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    build(sys.argv[1], "--look" in sys.argv)
