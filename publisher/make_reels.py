"""릴스 만들기 (GitHub Actions에서 실행 — 여기선 인터넷이 됨).

reels/specs/<id>.json 마다 reels/out/<id>.mp4 가 없으면:
  장면별 검색어로 Pixabay 실사 영상을 받아(상업 이용·출처 표기 불필요) 9:16으로 자르고,
  큰 글자를 얹고, 장면을 이어 붙이고, 위쪽 진행 막대를 넣는다. 소리 없음(음악은 앱에서).
쓴 영상 출처는 reels/out/<id>.credits.json 에 남긴다.

spec: {"id", "title", "scenes": [{"query", "text", "sub"?, "dur"?}], "lang"?}
  - 무료만: Pixabay API 결과 중 pixabay.com 콘텐츠만 쓴다(유료 스톡 사이트 결과·링크는 거름).
  - 실제 숙소 객실을 보여 주는 장면은 만들지 않는다(그 숙소 사진이 아니면 사실과 달라짐). 도시·풍경·사물만.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(os.environ.get("MEDIA_REPO", "."))
SPECS, OUT = ROOT / "reels" / "specs", ROOT / "reels" / "out"
FONTS = ROOT / "publisher" / "fonts"
W, H, FPS = 1080, 1920, 30
FADE = 0.3
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36 yeodigo-reels/1.0",
      "Accept": "application/json, video/mp4, */*"}
TRANS = ["fade", "smoothleft", "fade", "slideup", "fade", "smoothleft", "fade"]


# ---------------- Pixabay ----------------
def pixabay_videos(query: str, key: str, opener=urllib.request.urlopen) -> list[dict]:
    q = urllib.parse.urlencode({"key": key, "q": query, "per_page": 30, "safesearch": "true", "video_type": "film"})
    req = urllib.request.Request(f"https://pixabay.com/api/videos/?{q}", headers=UA)
    try:
        with opener(req, timeout=30) as r:
            return json.loads(r.read().decode()).get("hits", [])
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Pixabay 검색 {e.code}: {e.read().decode(errors='replace')[:200]}") from None


BAN = ("woman", "man", "girl", "boy", "people", "person", "face", "tourists", "crowd", "car", "interior", "dashboard",
       "bmw", "audi", "mercedes", "tesla", "logo", "brand", "advertising")


def candidates(hits: list[dict], need_s: float, used: set[int], place: list[str] | None = None, exclude: list[str] | None = None) -> list[dict]:
    """조건 맞는 영상을 좋은 순서로. 무료(pixabay.com)만 · 태그에 장소 이름 · 사람·차·브랜드 제외 · 충분히 긺."""
    def score(h):
        v = (h.get("videos") or {}).get("large") or (h.get("videos") or {}).get("medium") or {}
        w, hh = v.get("width") or 0, v.get("height") or 0
        likes, dls = h.get("likes", 0), max(1, h.get("downloads", 0))
        return ((hh > w) * 2 + (max(w, hh) >= 1920) + (likes >= 30) + (likes / dls * 100 >= 1.5), likes, dls)
    # 무료만 (맥퀸님 2026-11-22): Pixabay 자체 콘텐츠(pixabay.com 페이지)만. 유료 스톡(iStock 등 광고 결과)은 받지 않는다.
    hits = [h for h in hits if str(h.get("pageURL", "")).startswith("https://pixabay.com/")]
    if place:  # 사실과 다른 장소 방지: 태그에 그 장소 이름이 있는 영상만 (교토 장면에 오사카성 X)
        hits = [h for h in hits if any(p.lower() in str(h.get("tags", "")).lower() for p in place)]
    ok = [h for h in hits if h.get("id") not in used and (h.get("duration") or 0) >= need_s + 0.5
          and not ({t.strip() for t in str(h.get("tags", "")).lower().split(",")} & set(BAN + tuple(exclude or ())))]
    return sorted(ok, key=score, reverse=True)


def pick(hits, need_s, used, place=None, exclude=None):
    c = candidates(hits, need_s, used, place, exclude)
    return c[0] if c else None


def by_id(vid: int, key: str, opener=urllib.request.urlopen) -> dict | None:
    q = urllib.parse.urlencode({"key": key, "id": vid})
    req = urllib.request.Request(f"https://pixabay.com/api/videos/?{q}", headers=UA)
    with opener(req, timeout=30) as r:
        hits = json.loads(r.read().decode()).get("hits", [])
    return hits[0] if hits else None


def thumb(h: dict) -> str:
    v = h.get("videos") or {}
    for k in ("medium", "small", "large", "tiny"):
        if v.get(k, {}).get("thumbnail"):
            return v[k]["thumbnail"]
    return ""


def contact_sheet(rows: list, out: Path) -> None:
    """장면마다 후보 4개 썸네일(고른 것 초록 테두리). 루틴(Claude)이 보고 더 맞는 걸 spec 장면의 pixabay_id로 바꿔 다시 만든다."""
    import io
    tw, th, pad, lab = 320, 180, 12, 56
    img = Image.new("RGB", (pad + 4 * (tw + pad) + 460, pad + len(rows) * (th + lab + pad)), (24, 26, 30))
    d = ImageDraw.Draw(img)
    f, fs = _font("Bold", 22), _font("SemiBold", 18)
    for r, (scene, cands, chosen) in enumerate(rows):
        y = pad + r * (th + lab + pad)
        d.text((pad + 4 * (tw + pad), y), f"{r + 1}. {scene['text'].replace(chr(10), ' ')}", font=f, fill=(255, 255, 255))
        d.text((pad + 4 * (tw + pad), y + 34), scene.get("query", ""), font=fs, fill=(180, 180, 180))
        for c, h in enumerate(cands[:4]):
            x = pad + c * (tw + pad)
            try:
                with urllib.request.urlopen(urllib.request.Request(thumb(h), headers=UA), timeout=30) as resp:
                    img.paste(Image.open(io.BytesIO(resp.read())).convert("RGB").resize((tw, th)), (x, y))
            except Exception:
                d.rectangle((x, y, x + tw, y + th), fill=(60, 60, 60))
            if h["id"] == chosen:
                d.rectangle((x - 4, y - 4, x + tw + 4, y + th + 4), outline=(46, 204, 170), width=6)
            d.text((x, y + th + 6), f"id {h['id']}", font=f, fill=(46, 204, 170) if h["id"] == chosen else (220, 220, 220))
            d.text((x, y + th + 32), str(h.get("tags", ""))[:34], font=fs, fill=(160, 160, 160))
    img.save(out, quality=85)


def video_url(h: dict) -> str:
    v = h["videos"]
    for k in ("large", "medium", "small"):
        if v.get(k, {}).get("url"):
            return v[k]["url"]
    raise ValueError("영상 주소 없음")


def download(url: str, dest: Path, opener=urllib.request.urlopen) -> None:
    req = urllib.request.Request(url, headers=UA)
    with opener(req, timeout=120) as r, open(dest, "wb") as f:
        f.write(r.read())


# ---------------- 글자 ----------------
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"Pretendard-{weight}.otf"), size)


HL = (255, 206, 84, 255)   # 강조 글자색 (==숫자==) — 노을·눈·밤 어디서도 잘 보이는 따뜻한 노랑


def _tokens(text: str) -> list[list[tuple[str, bool]]]:
    """줄마다 [(낱말, 강조?)]. ==…== 안은 강조."""
    out = []
    for para in text.split("\n"):
        words, hl = [], False
        for part in para.split("=="):
            for w in part.split(" "):
                if w:
                    words.append((w, hl))
            hl = not hl
        # '==' 경계에서 띄어쓰기 없이 붙은 조각은 앞 낱말에 붙임 (예: ==8,000엔==이 → 8,000엔|이)
        merged, raw = [], para.replace("==", "\0")
        idx = 0
        for w, h in words:
            j = raw.find(w.replace("==", ""), idx) if w else idx
            glued = merged and j > 0 and raw[j - 1] == "\0" and (j - 2 < 0 or raw[j - 2] != " ") and (len(raw) <= j - 1 or True)
            if glued and j - 1 > 0 and raw[j - 2] != " ":
                merged[-1].append((w, h))
            else:
                merged.append([(w, h)])
            idx = j + len(w)
        out.append(merged)
    return out


def _layout(draw, text: str, font, max_w: int) -> list[list[list[tuple[str, bool]]]]:
    """줄 → 낱말 묶음(붙은 조각 포함) 목록. 폭 넘으면 줄바꿈."""
    sp = draw.textlength(" ", font=font)
    lines = []
    for para in _tokens(text):
        cur, cw = [], 0.0
        for grp in para:
            gw = sum(draw.textlength(w, font=font) for w, _ in grp)
            add = gw + (sp if cur else 0)
            if cur and cw + add > max_w:
                lines.append(cur)
                cur, cw = [grp], gw
            else:
                cur.append(grp)
                cw += add
        lines.append(cur)
    return lines


def _draw_line(draw, x: int, y: int, line, font, color, shadow: bool = False) -> None:
    sp = draw.textlength(" ", font=font)
    for gi, grp in enumerate(line):
        if gi:
            x += sp
        for w, h in grp:
            draw.text((x, y + (3 if shadow else 0)), w, font=font, fill=(0, 0, 0, 150) if shadow else (HL if h else color))
            x += draw.textlength(w, font=font)


def text_layer(text: str, sub: str, out: Path, kicker: str = "") -> None:
    """가운데 조금 위(릴스 아래 캡션 자리 피함). 흰 굵은 글자 + 강조(==…==)는 노랑 + 은은한 그림자."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    grad = Image.new("L", (1, H))
    for y in range(H):
        mid = 1 - abs(y - H * 0.47) / (H * 0.47)
        grad.putpixel((0, y), int(40 + 70 * max(0, mid) ** 1.5))
    img.paste((0, 0, 0, 255), (0, 0, W, H), grad.resize((W, H)))
    d = ImageDraw.Draw(img)
    big, small, kick = _font("ExtraBold", 92), _font("SemiBold", 44), _font("Bold", 34)
    lines = _layout(d, text, big, W - 160)
    sub_lines = _layout(d, sub, small, W - 160) if sub else []
    lh, sh = 116, 62
    block = len(lines) * lh + (24 + len(sub_lines) * sh if sub_lines else 0) + (64 if kicker else 0)
    y0 = int(H * 0.47 - block / 2)
    rows, y = [], y0
    if kicker:
        rows.append(([[(kicker, False)]], kick, y, (232, 220, 196, 255)))
        y += 64
    for ln in lines:
        rows.append((ln, big, y, (255, 255, 255, 255)))
        y += lh
    if sub_lines:
        y += 24
        for ln in sub_lines:
            rows.append((ln, small, y, (235, 235, 235, 255)))
            y += sh
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for ln, f, yy, c in rows:
        _draw_line(sd, 80, yy, ln, f, c, shadow=True)
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(6)))
    d = ImageDraw.Draw(img)
    for ln, f, yy, c in rows:
        _draw_line(d, 80, yy, ln, f, c)
    img.save(out)


# ---------------- 영상 ----------------
def scene_clip(src: Path, overlay: Path, dur: float, out: Path, zoom_in: bool = True, focus_x: float = 0.5, start: float = 0.3) -> None:
    """focus_x: 가로 영상을 세로로 자를 때 어디를 남길지 (0 왼쪽 · 0.5 가운데 · 1 오른쪽). 주인공이 가장자리에 걸리면 조정."""
    n = round(dur * FPS)
    z = f"1+0.06*on/{n}" if zoom_in else f"1.06-0.06*on/{n}"
    fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}:(iw-{W})*{focus_x}:(ih-{H})/2,fps={FPS},"
          f"scale={W * 2}:{H * 2},zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},"
          f"eq=saturation=1.05:contrast=1.03[b];[b][1:v]overlay=0:0,format=yuv420p[v]")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(src), "-i", str(overlay),
                    "-filter_complex", fc, "-map", "[v]", "-an", "-frames:v", str(n), "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "19", "-r", str(FPS), str(out)], check=True)


def join(clips: list[Path], durs: list[float], out: Path) -> None:
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for c in clips:
        args += ["-i", str(c)]
    total = sum(durs)
    parts, prev, t = [], "[0:v]", 0.0
    for i in range(1, len(clips)):
        t += durs[i - 1]
        lab = f"[x{i}]"
        parts.append(f"{prev}[{i}:v]xfade=transition={TRANS[(i - 1) % len(TRANS)]}:duration={FADE}:offset={t:.3f}{lab}")
        prev = lab
    parts.append(f"color=c=0xE8DCC4:s={W}x8:r={FPS}:d={total:.2f}[bar];{prev}[bar]overlay=x='-W+W*t/{total:.2f}':y=150:shortest=1[v]")
    args += ["-filter_complex", ";".join(parts), "-map", "[v]", "-c:v", "libx264", "-preset", "medium", "-crf", "24", "-maxrate", "6M", "-bufsize", "12M",
             "-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart", str(out)]
    subprocess.run(args, check=True)


def make(spec: dict, key: str, fetch=pixabay_videos, dl=download) -> dict:
    scenes = spec["scenes"]
    durs = [float(s.get("dur") or (2.2 if i == 0 else 3.0 if i == len(scenes) - 1 else 2.6)) for i, s in enumerate(scenes)]
    used: set[int] = set()
    credits, clips, sheet_rows = [], [], []
    pool: list[dict] = []          # 이 릴스에서 쓴 영상 (다시 쓸 후보)
    reuse: dict[int, int] = {}
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for i, (s, d) in enumerate(zip(scenes, durs)):
            seg = d + (FADE if i < len(scenes) - 1 else 0)
            place = s.get("place", spec.get("place"))
            cands: list[dict] = []
            for q in [s["query"]] + list(s.get("fallback") or []) + list(place or []):
                seen = {x["id"] for x in cands}
                cands += [c for c in candidates(fetch(q, key), seg, used, place, spec.get("exclude")) if c["id"] not in seen]
                if len(cands) >= 4:
                    break
            h = by_id(int(s["pixabay_id"]), key) if s.get("pixabay_id") and fetch is pixabay_videos else (cands[0] if cands else None)
            start = 0.3
            if not h and pool:  # 그 장소 영상이 적으면(예: 보홀) 다른 도시 영상을 끌어오지 않고, 이미 쓴 같은 장소 영상의 다른 구간을 쓴다
                h = min(pool, key=lambda x: reuse.get(x["id"], 0))
                reuse[h["id"]] = reuse.get(h["id"], 0) + 1
                start = min(max(0.3, (h.get("duration") or 0) - seg - 0.3), 0.3 + reuse[h["id"]] * (seg + 0.5))
            if not h:
                raise RuntimeError(f"장면 {i + 1} '{s['query']}' 맞는 영상 없음")
            if h["id"] not in {x["id"] for x in pool}:
                pool.append(h)
            sheet_rows.append((s, ([h] + [c for c in cands if c["id"] != h["id"]])[:4], h["id"]))
            used.add(h["id"])
            src = td / f"src{i}.mp4"
            dl(video_url(h), src)
            ov = td / f"t{i}.png"
            text_layer(s["text"], s.get("sub", ""), ov, s.get("kicker", ""))
            c = td / f"c{i}.mp4"
            scene_clip(src, ov, seg, c, zoom_in=i % 2 == 0, focus_x=float(s.get("focus_x", 0.5)), start=start)
            clips.append(c)
            credits.append({"scene": i + 1, "query": s["query"], "pixabay_id": h["id"], "page": h.get("pageURL"), "user": h.get("user"), "tags": h.get("tags")})
        OUT.mkdir(parents=True, exist_ok=True)
        out = OUT / f"{spec['id']}.mp4"
        join(clips, durs, out)
        if fetch is pixabay_videos:
            contact_sheet(sheet_rows, OUT / f"{spec['id']}.candidates.jpg")
    meta = {"id": spec["id"], "duration": round(sum(durs), 1), "credits": credits, "license": "Pixabay Content License (상업 이용·출처 표기 불필요)"}
    (OUT / f"{spec['id']}.credits.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def main() -> int:
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key:
        print("PIXABAY_API_KEY 없음", file=sys.stderr)
        return 1
    only = os.environ.get("ONLY_ID", "").strip()
    bad = 0
    for f in sorted(SPECS.glob("*.json")):
        spec = json.loads(f.read_text(encoding="utf-8"))
        if only and spec["id"] != only:
            continue
        if (OUT / f"{spec['id']}.mp4").exists() and not only:
            continue
        try:
            m = make(spec, key)
            print(f"[릴스] {spec['id']} {m['duration']}초")
        except Exception as e:  # 한 편 실패해도 나머지는 계속
            bad += 1
            (OUT).mkdir(parents=True, exist_ok=True)
            (OUT / f"{spec['id']}.error.txt").write_text(str(e), encoding="utf-8")
            print(f"[실패] {spec['id']}: {e}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
