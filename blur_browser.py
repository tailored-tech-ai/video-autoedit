#!/usr/bin/env python3
"""
Блюр адресной строки и панели закладок браузера в проекте Tight Studio.

Как находит: из исходной записи берутся ключевые кадры (примерно раз в 1.3 с, без
декодирования всего 4K), верхняя полоса кадра распознаётся встроенным в macOS Vision
(frame_ocr.swift). Строка с адресом узнаётся по форме: домен, порт, путь. Закладки это
строка текста сразу под адресом. Маска во всю ширину кадра закрывает обе строки, по
времени покрывает участок от предыдущего ключевого кадра до следующего: лучше
лишняя секунда блюра, чем секунда открытого адреса.

Маски ставятся в проект через мост Tight Studio и едут вместе с зумом. Маски во всю
ширину кадра считаются своими: при повторном запуске они удаляются и ставятся заново,
ручные маски не трогаются.

Отдельно, на открытом проекте:
    python3 blur_browser.py            # показать, что найдено
    python3 blur_browser.py --apply    # поставить маски
"""

import argparse
import json
import re
import statistics
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
OCR_BIN = Path.home() / "Library/Caches/tight-publish/frame_ocr"
BAND = 0.15        # доля кадра сверху, где ищем браузер
INTENSITY = 75
URL = re.compile(r"(https?://|www\.|localhost|[a-z0-9-]{2,}\.[a-z]{2,}|:\d{3,5})")


def ocr_tool() -> Path:
    src = HERE / "frame_ocr.swift"
    if not OCR_BIN.exists() or OCR_BIN.stat().st_mtime < src.stat().st_mtime:
        OCR_BIN.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["swiftc", "-O", str(src), "-o", str(OCR_BIN)], check=True)
    return OCR_BIN


def keyframe_bands(source: Path, workdir: Path) -> list[tuple[float, list[dict]]]:
    """Время ключевого кадра и распознанные строки его верхней полосы (y в долях кадра)."""
    times = [float(t.strip(",")) for t in subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
         "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(source)],
        capture_output=True, text=True, check=True).stdout.split() if t.strip(",")]
    subprocess.run(["ffmpeg", "-v", "error", "-skip_frame", "nokey", "-i", str(source),
                    "-vf", f"crop=iw:ih*{BAND}:0:0", "-fps_mode", "passthrough",
                    str(workdir / "%06d.png")], check=True)
    files = sorted(workdir.glob("*.png"))
    if len(files) != len(times):
        raise RuntimeError(f"ключевых кадров {len(times)}, картинок {len(files)}")

    tool = ocr_tool()
    chunks = [files[i::4] for i in range(4)]
    with ThreadPoolExecutor(4) as pool:
        outs = pool.map(lambda c: subprocess.run([str(tool), *map(str, c)], capture_output=True,
                                                 text=True, check=True).stdout, chunks)
    by_file = {}
    for out in outs:
        for line in out.splitlines():
            d = json.loads(line)
            by_file[d["file"]] = [{**l, "y": l["y"] * BAND, "h": l["h"] * BAND} for l in d["lines"]]
    return [(t, by_file.get(str(f), [])) for t, f in zip(times, files)]


def center(l: dict) -> float:
    return l["y"] + l["h"] / 2


def confident_url(lines: list[dict]) -> dict | None:
    """Строка адреса, прочитанная уверенно: похожа на URL и стоит слева."""
    urls = [l for l in lines if l["x"] < 0.35 and URL.search(l["text"].lower().replace(" ", ""))]
    return min(urls, key=lambda l: l["y"]) if urls else None


def bookmarks_below(lines: list[dict], c: float) -> float | None:
    """Центр строки закладок под адресом: несколько надписей в ряд или одна длинная."""
    marks = [l for l in lines if c + 0.008 < center(l) < c + 0.04]
    if len(marks) >= 2 or any(l["w"] > 0.1 for l in marks):
        return statistics.median(center(l) for l in marks)
    return None


def learn_layouts(frames) -> list[dict]:
    """Где на этой записи стоит строка адреса. Окно может переезжать, мест бывает несколько."""
    hits = []
    for _, lines in frames:
        url = confident_url(lines)
        if url:
            hits.append({"x": url["x"], "c": center(url), "b": bookmarks_below(lines, center(url))})
    layouts = []
    for h in sorted(hits, key=lambda h: h["c"]):
        group = next((g for g in layouts if abs(g["c"] - h["c"]) < 0.004 and abs(g["x"] - h["x"]) < 0.03), None)
        if group:
            group["hits"].append(h)
        else:
            layouts.append({"x": h["x"], "c": h["c"], "hits": [h]})
    out = []
    for g in layouts:
        if len(g["hits"]) < 3:  # случайное совпадение со страницей
            continue
        bs = [h["b"] for h in g["hits"] if h["b"]]
        out.append({"x": statistics.median(h["x"] for h in g["hits"]),
                    "c": statistics.median(h["c"] for h in g["hits"]),
                    "b": statistics.median(bs) if len(bs) >= len(g["hits"]) / 3 else None})
    return out


def find_layout(lines: list[dict], layouts: list[dict]) -> dict | None:
    """Браузер на кадре: в месте строки адреса есть текст, пусть и нечитаемый."""
    for g in layouts:
        if any(abs(center(l) - g["c"]) < 0.004 and abs(l["x"] - g["x"]) < 0.03 for l in lines):
            return g
    return None


def mask_ranges(frames, duration: float) -> list[dict]:
    """Маски по времени исходника: [{start, end, y, height}]."""
    layouts = learn_layouts(frames)
    rows = [(t, find_layout(lines, layouts)) for t, lines in frames]
    gaps = [g["b"] - g["c"] for g in layouts if g["b"]]
    pitch = statistics.median(gaps) if gaps else 0.025  # от адреса до закладок
    bands = []
    for t, g in rows:
        if not g:
            bands.append(None)
            continue
        top = max(0.0, g["c"] - 0.7 * pitch)
        bottom = (g["b"] or g["c"]) + 0.7 * pitch
        bands.append((round(top / 0.0025) * 0.0025, round(bottom / 0.0025) * 0.0025))

    ranges = []
    times = [t for t, _ in rows]
    for i, band in enumerate(bands):
        if band is None:
            continue
        start = times[i - 1] if i > 0 else 0.0
        end = times[i + 1] if i + 1 < len(times) else duration
        last = ranges[-1] if ranges else None
        if last and abs(last["y"] - band[0]) < 0.004 and abs(last["y"] + last["height"] - band[1]) < 0.004 \
                and start <= last["end"]:
            last["end"] = end
        else:
            ranges.append({"start": start, "end": end, "y": band[0], "height": band[1] - band[0]})
    for r in ranges:
        r["end"] = min(r["end"], duration - 0.02)
    return ranges


def to_timeline(ts, src_start: float, src_end: float) -> list[tuple[float, float]]:
    """Отрезок исходника во время клипа с учётом вырезанных кусков и скорости."""
    segs, offset = [], 0
    while True:
        d = ts.call("timeline.inspect", offset=offset, limit=100)["data"]
        segs += d["segments"]
        offset += len(d["segments"])
        if not d.get("hasMore"):
            break
    out = []
    for s in segs:
        if not s.get("kept", True):
            continue
        a, b = max(src_start, s["sourceStartTime"]), min(src_end, s["sourceEndTime"])
        if b > a:
            speed = s.get("speed") or 1
            out.append((s["startTime"] + (a - s["sourceStartTime"]) / speed,
                        s["startTime"] + (b - s["sourceStartTime"]) / speed))
    return out


def own_masks(ts) -> list[str]:
    ids, offset = [], 0
    while True:
        d = ts.call("annotations.inspect", offset=offset, limit=100)["data"]
        for a in d["items"]:
            area = a.get("maskArea") or a.get("area") or {}
            if a.get("kind") == "mask" and area.get("x", 1) == 0 and area.get("width", 0) >= 0.999:
                ids.append(a["id"])
        offset += len(d["items"])
        if not d.get("hasMore"):
            return ids


def blur(ts, clip: dict, project_path: Path, apply: bool) -> int:
    source = project_path / "Recordings" / clip["id"] / "Resources/original.mp4"
    if not source.exists():
        print(f"   ⚠️ нет исходника {source}, блюр пропущен")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        frames = keyframe_bands(source, Path(tmp))
    ranges = mask_ranges(frames, clip["durationSeconds"])
    layouts = learn_layouts(frames)
    seen = sum(1 for _, lines in frames if find_layout(lines, layouts))
    print(f"🫥 Браузер с адресом на {seen} из {len(frames)} ключевых кадров, масок: {len(ranges)}")
    for r in ranges:
        print(f"   {r['start']:7.2f}-{r['end']:7.2f} с  y {r['y']:.4f}, высота {r['height']:.4f}")
    if not apply:
        return len(ranges)

    ts.call("project.select_clip", clip_id=clip["id"])
    for mask_id in own_masks(ts):
        ts.call("annotations.delete", kind="mask", segment_id=mask_id)
    added = 0
    for r in ranges:
        for start, end in to_timeline(ts, r["start"], r["end"]):
            res = ts.call("annotations.add", kind="mask", mask_style="blur", start_time=round(start, 2),
                          end_time=round(end, 2),
                          area={"x": 0, "y": r["y"], "width": 1, "height": r["height"]})
            mask_id = (res.get("data") or {}).get("id")
            if mask_id:
                ts.call("annotations.update", kind="mask", segment_id=mask_id, intensity=INTENSITY)
            added += 1
    print(f"   поставлено масок: {added}")
    return added


def main():
    p = argparse.ArgumentParser(description="Блюр адреса и закладок браузера в открытом проекте Tight Studio")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    import tight_publish as tp
    ts = tp.TightStudio()
    try:
        info = ts.call("project.inspect")["data"]
        path = tp.PROJECTS_DIR / f"{info['name']}.tightstudio"
        for clip in info["clips"]:
            if clip["type"] == "recording" and not clip["hidden"]:
                blur(ts, clip, path, args.apply)
    finally:
        ts.close()


if __name__ == "__main__":
    main()
