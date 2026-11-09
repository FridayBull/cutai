#!/usr/bin/env python3
"""Selbsttest: python3 test_cutai.py (braucht ffmpeg, keine Videos)."""
import array, json, sys, tempfile, wave
from pathlib import Path

import cut

cut.OUT = Path(tempfile.mkdtemp())  # Test schreibt nie ins echte out/

# Drop-Frame-Timecode: 1 min 29,97 fps überspringt Frames 0 und 1
assert cut.tc_frames("00:00:59;29") == 1799
assert cut.tc_frames("00:01:00;02") == 1800
assert cut.tc_frames("00:10:00;00") == 17982

# Schnitte auf dem Beat ohne Drift: Summe der Shot-Längen = Gesamtlänge
plan = {"bpm": 152, "shots": [{"beats": b} for b in [4, 4, 2, 2, 1, 1, 1, 1] * 10]}
sh = cut.timeline(plan)
assert sum(s["len"] for s in sh) == round(sum(s["beats"] for s in plan["shots"]) * 60 / 152 * cut.FPS)
assert all(a["pos"] + a["len"] == b["pos"] for a, b in zip(sh, sh[1:]))

# Clip-Namen: voller Name oder eindeutiger Teil, nicht Teil einer anderen Zahl
cl = {"CAM_A1205945_0594_D": "a", "CAM_A1210000_0595_D": "b", "IMG_1234": "c"}
assert cut.clip(cl, "0594") == "a" and cut.clip(cl, "1234") == "c" and cut.clip(cl, "IMG_1234") == "c"
try:
    cut.clip(cl, "D")  # in zwei Namen -> Fehler statt falscher Clip
    sys.exit("mehrdeutiger Clip nicht erkannt")
except SystemExit as e:
    assert "2 Treffer" in str(e)

# BPM: Klick-Spur 128 BPM, 20 s
sr, bpm = 11025, 128
a = array.array("h", bytes(2 * sr * 20))
for k in range(int(20 * bpm / 60)):
    i = int(k * 60 / bpm * sr)
    for j in range(200):
        a[i + j] = 20000 if j % 20 < 10 else -20000
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "click.wav"
    with wave.open(str(f), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr), w.writeframes(a.tobytes())
    r = cut.beats(str(f))
assert abs(r["bpm"] - bpm) < 1.5, r["bpm"]

# Look: alte Pläne ergeben genau die alte Filterkette, Vorlagen und Shot-Werte greifen
import reel
OLD = ("hqdn3d=1.5:1.5:3:3,cas=0.7,curves=m='0/0 0.1/0.08 0.5/0.52 0.85/0.89 1/0.97',"
       "colorbalance=rs=-0.04:gs=0.01:bs=0.07:rh=0.05:bh=-0.04,eq=saturation=1.05:contrast=1,vignette=PI/5,noise=alls=5:allf=t")
SWR = "colorhold=color=0xE01010:similarity=0.08:blend=0.04,eq=saturation=1.4:contrast=1.3:brightness=-0.03"
assert reel.grade(reel.resolve_look({})) == OLD
assert reel.grade(reel.resolve_look({"look_filter": SWR})) == OLD.replace("eq=saturation=1.05:contrast=1", SWR)
assert reel.grade(reel.resolve_look({"look": "bw_red"})) == OLD.replace("eq=saturation=1.05:contrast=1", SWR)
assert reel.grade(reel.resolve_look({"look": "neutral"})) == "hqdn3d=1:1:2:2,cas=0.3"
assert reel.grade(reel.resolve_look({"look": {"preset": "neutral", "grain": 3}}, {"bw": True})).endswith("noise=alls=3:allf=t,hue=s=0")
assert "lut3d=file=" in reel.grade(reel.resolve_look({"look": {"lut": "~/x.cube"}}))

# Live-Status: Felder bleiben erhalten, neue überschreiben
(cut.OUT / "status.json").unlink()
cut.log("Shot 1", done=1, total=3)
cut.log("Shot 2", done=2)
st = json.loads((cut.OUT / "status.json").read_text())
assert st["done"] == 2 and st["total"] == 3 and st["msg"] == "Shot 2"
print("ok")
