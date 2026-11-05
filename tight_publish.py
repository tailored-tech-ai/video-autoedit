#!/usr/bin/env python3
"""
Демо-видео из Tight Studio до готового mp4 одной командой.

Шаги:
  1. Открывает проект в Tight Studio в фоне (open -g): фокус остаётся у тебя.
  2. Через локальный мост Tight Studio (тот же сервер, что у Claude Desktop, но без
     Claude): стиль субтитров clean-modern, расшифровка записи, субтитры включены,
     вычитка субтитров (caption_check.py), блюр адреса и закладок (blur_browser.py).
  3. Экспорт MP4 в 4K без музыки. Tight Studio экспортирует только из окна в фокусе
     и всегда спрашивает папку: скрипт на пару секунд выводит его вперёд, Export here
     жмёт TightClick (см. tightclick/) или человек, потом фокус возвращается.
  4. smart_edit.py: паузы, метки «кат-кат», ускорение в том же рендере.
  5. Звук: голосовая цепочка, музыка на всю длину, громкость -16 LUFS в два прохода.

Музыка только в постобработке, в проект Tight Studio скрипт её не кладёт: положенная
туда, она рвалась бы на каждой вырезанной паузе.

Два окна Tight Studio жмёт TightClick, мини-приложение с Accessibility только для себя:
  - Allow при первом подключении после запуска Tight Studio (ответ живёт в памяти до
    выхода из приложения, «Not now» блокирует мост до перезапуска);
  - Export here с именем файла по дате, чтобы не упереться в «Replace».
Без доступа у TightClick оба клика остаются за человеком.

Примеры:
    .venv/bin/python tight_publish.py "my demo"
    # с готового экспорта, без Tight Studio
    .venv/bin/python tight_publish.py "my demo" --export ~/Downloads/x.mp4

Свои значения флагов по умолчанию лежат в tight_publish.local.json рядом со скриптом,
ключи как у флагов: {"language": "uk", "music": "lofi", "speed": 1.1}.
"""

import argparse
import datetime as dt
import glob
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import blur_browser
import caption_check
import jump_cut_vad_patched as jc

HOME = Path.home()
PROJECTS_DIR = HOME / "Tight Studio Videos"
MEDIA_DIR = HOME / "Library/Application Support/Tight Studio/Media"
APP_PATH = Path("/Applications/Tight Studio.app")
EXT_GLOB = str(HOME / "Library/Application Support/Claude/Claude Extensions/*tight-studio*/server/index.js")
EXPORT_DIRS = [HOME / "Downloads", HOME / "Desktop", HOME / "Movies", HOME / "Documents", PROJECTS_DIR]


class TightError(RuntimeError):
    pass


# ---------------------------------------------------------------- мост Tight Studio

class TightStudio:
    """MCP-клиент к серверу расширения Tight Studio по stdio."""

    def __init__(self):
        servers = sorted(glob.glob(EXT_GLOB))
        if not servers:
            raise SystemExit("❌ Не нашёл расширение Tight Studio для Claude Desktop: подключи Tight Studio "
                             "к Claude Desktop, см. README")
        node = shutil.which("node") or max(
            glob.glob(str(HOME / ".nvm/versions/node/*/bin/node")), default=None)
        if not node:
            raise SystemExit("❌ Нужен node (brew install node)")
        self.log = tempfile.NamedTemporaryFile("w", prefix="tight-bridge-", suffix=".log", delete=False)
        self.p = subprocess.Popen([node, servers[-1]], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=self.log, text=True, bufsize=1)
        self.n = 0
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "tight-publish", "version": "1.0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def _rpc(self, method, params):
        self.n += 1
        self._send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise TightError(f"мост Tight Studio закрылся, лог: {self.log.name}")
            msg = json.loads(line)
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise TightError(msg["error"].get("message", str(msg["error"])))
                return msg["result"]

    def call(self, tool, **args):
        res = self._rpc("tools/call", {"name": tool, "arguments": args})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {"ok": not res.get("isError"), "summary": text}
        if res.get("isError") or payload.get("ok") is False:
            raise TightError(payload.get("summary") or text)
        return payload

    def close(self):
        self.p.terminate()


# ---------------------------------------------------------------- фокус и клавиатура

def app_bundle_id(app: Path) -> str:
    with open(app / "Contents/Info.plist", "rb") as f:
        return plistlib.load(f)["CFBundleIdentifier"]


def frontmost_bundle() -> str | None:
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    out = subprocess.run(["lsappinfo", "info", "-only", "bundleid", asn],
                         capture_output=True, text=True).stdout
    m = re.search(r'bundleID="([^"]+)"', out)
    return m.group(1) if m else None


def wait_front(bundle: str, timeout=5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if frontmost_bundle() == bundle:
            return True
        time.sleep(0.2)
    return False


CLICKER = HOME / "Applications/TightClick.app"


def clicker(*args: str, wait: bool = True) -> str:
    """TightClick: мини-приложение с Accessibility, жмёт Allow и Export here в Tight Studio.
    Запуск через open, чтобы macOS проверяла права TightClick, а не терминала."""
    if not CLICKER.exists():
        return "missing"
    fd, out = tempfile.mkstemp(prefix="tightclick-", suffix=".txt")
    os.close(fd)
    subprocess.run(["open", "-n", "-g", "--stdout", out, "-a", str(CLICKER), "--args", *args],
                   check=False)
    if not wait:
        return out
    for _ in range(100):  # open -W не успевает за коротким запуском, ждём ответ в файле
        answer = Path(out).read_text().strip()
        if answer:
            return answer
        time.sleep(0.1)
    return "fail:no-answer"


def notify(text):
    subprocess.run(["osascript", "-e", f'display notification "{text}" with title "tight_publish"'],
                   capture_output=True)


# ---------------------------------------------------------------- шаги в Tight Studio

def open_project(path: Path, name: str, timeout=120) -> tuple[TightStudio, dict]:
    back_to, tight = frontmost_bundle(), app_bundle_id(APP_PATH)
    subprocess.run(["open", "-g", "-a", str(APP_PATH), str(path)], check=True)
    if clicker("check") == "trusted":
        clicker("allow", "45", wait=False)
        print(f"📂 Открываю «{name}» в фоне, Allow в Tight Studio нажмёт TightClick")
    else:
        print(f"📂 Открываю «{name}» в фоне. Если Tight Studio спросит доступ, нажми Allow.")
    deadline = time.time() + timeout
    ts, last = None, ""
    while time.time() < deadline:
        try:
            ts = ts or TightStudio()
            info = ts.call("project.inspect")["data"]
            if info.get("isOpen") and info.get("name") == name:
                # свежий запуск Tight Studio выходит вперёд сам, возвращаем фокус
                if back_to and back_to != tight and frontmost_bundle() == tight:
                    subprocess.run(["open", "-b", back_to])
                return ts, info
            last = f"в фокусе другой проект: «{info.get('name')}»"
        except TightError as e:
            last = str(e)
            if "закрылся" in last and ts:
                ts.close()
                ts = None
        time.sleep(2)
    raise SystemExit(f"❌ Проект не открылся за {timeout} с: {last}")


def transcribe(ts: TightStudio, clip: dict, style: str, timeout=1800):
    ts.call("project.select_clip", clip_id=clip["id"])
    ts.call("captions.set_style", style=style)
    print(f"📝 {clip['name']}: субтитры {style}, {ts.call('captions.generate', force=False)['summary']}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = ts.call("captions.inspect", limit=1)["data"]
        st, done = d["generation"]["status"], d["total"] > 0
        if st in ("failed", "no_original_audio"):
            raise SystemExit(f"❌ Расшифровка не прошла: {st}")
        if st == "completed" or (done and st not in ("generating", "queued")):
            ts.call("captions.set_enabled", enabled=True)
            print(f"   готово: {d['total']} сегментов субтитров")
            return
        print(f"   расшифровка: {st} {d['generation'].get('progress', '')}", flush=True)
        time.sleep(5)
    raise SystemExit("❌ Расшифровка не уложилась в таймаут")


def find_export(name: str, since: float, exact: str | None) -> Path | None:
    hits = []
    for d in EXPORT_DIRS:
        for f in d.glob("*.mp4") if d.exists() else []:
            if f.stat().st_mtime < since:
                continue
            if (exact and f.stem == exact) or (not exact and f.stem.lower().startswith(name.lower())):
                hits.append(f)
    return max(hits, key=lambda f: f.stat().st_mtime) if hits else None


def export(ts: TightStudio, name: str, resolution: str) -> Path:
    fps = int(ts.call("delivery.inspect")["data"]["settings"].get("fps") or 30)
    ts.call("delivery.configure_export", format="MP4", quality="High", fps=fps, resolution=resolution)

    auto = clicker("check") == "trusted"
    tight = app_bundle_id(APP_PATH)
    back_to = frontmost_bundle()
    subprocess.run(["open", "-a", str(APP_PATH)], check=True)
    if not wait_front(tight):
        raise SystemExit("❌ Tight Studio не вышел вперёд, а экспорт работает только из окна в фокусе")

    started = time.time()
    exact = None
    if auto:
        exact = f"{name} {dt.datetime.now():%Y-%m-%d %H-%M}"
        clicker("export", exact, "30", wait=False)
        print(f"🎬 Экспорт {resolution}, {fps} fps: «{exact}.mp4», Export here нажмёт TightClick")
    else:
        notify("Tight Studio ждёт: нажми Export here")
        print(f"🎬 Экспорт {resolution}, {fps} fps: нажми Export here в Tight Studio "
              "(чтобы жал сам, выдай доступ TightClick, см. README)")
    try:
        r = ts.call("delivery.start_export", resolution=resolution)
    finally:
        if back_to and back_to != tight:
            subprocess.run(["open", "-b", back_to])
    if not r.get("changed"):
        raise SystemExit(f"❌ {r.get('summary')}")

    last = None
    while True:
        e = ts.call("delivery.inspect")["data"]["export"]
        if e.get("error"):
            raise SystemExit(f"❌ Экспорт упал: {e['error']}")
        if e.get("complete") and not e.get("isExporting"):
            break
        p = (e.get("progress") or {}).get("progress")
        if isinstance(p, (int, float)) and int(p) // 10 != last:
            last = int(p) // 10
            print(f"   экспорт: {p:.0f}%", flush=True)
        time.sleep(5)

    for _ in range(20):
        out = find_export(name, started - 5, exact)
        if out:
            print(f"   ✅ {out}")
            return out
        time.sleep(1)
    raise SystemExit("❌ Экспорт завершён, но файл не найден. Передай путь через --export")


# ---------------------------------------------------------------- после экспорта

def resolve_music(query: str) -> dict:
    lib = json.loads((MEDIA_DIR / "media-library.json").read_text())
    for m in lib:
        if query.lower() in m.get("name", "").lower() and Path(m.get("filePath", "")).exists():
            return m
    raise SystemExit(f"❌ В медиатеке Tight Studio нет трека «{query}»")


def venv_python() -> str:
    local = Path(__file__).resolve().parent / ".venv/bin/python"
    return str(local) if local.exists() else sys.executable


def loudnorm_measure(cmd_in: list[str], graph: str) -> dict:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *cmd_in, "-filter_complex",
                        graph + ",loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json[a]",
                        "-map", "[a]", "-f", "null", "-"], capture_output=True, text=True)
    tail = r.stderr[r.stderr.rfind("{"):r.stderr.rfind("}") + 1]
    if r.returncode or not tail:
        raise SystemExit(f"❌ Замер громкости не прошёл:\n{r.stderr[-1500:]}")
    return json.loads(tail)


def finish_audio(cut: Path, out: Path, music: dict | None, volume: float):
    """Голосовая цепочка на весь файл, музыка без разрывов, громкость в два прохода."""
    duration = jc.get_duration(str(cut))
    voice = ",".join(jc.AUDIO_FILTERS[k] for k in ("highpass", "lowpass", "warmth", "presence", "compression"))
    cmd_in = ["-i", str(cut)]
    if music:
        cmd_in += ["-stream_loop", "-1", "-i", music["filePath"]]
        fade = max(0.0, duration - 3)
        graph = (f"[0:a]{voice}[v];"
                 f"[1:a]volume={volume},afade=t=in:d=1,afade=t=out:st={fade:.2f}:d=3[m];"
                 f"[v][m]amix=inputs=2:duration=first:normalize=0")
    else:
        graph = f"[0:a]{voice}"

    print("🎧 Замеряю громкость...")
    m = loudnorm_measure(cmd_in, graph)
    norm = (f"loudnorm=I=-16:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
            f"offset={m['target_offset']}:linear=true")
    print(f"   было {m['input_i']} LUFS, свожу к -16")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *cmd_in,
                    "-filter_complex", f"{graph},{norm}[a]", "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                    "-movflags", "+faststart", str(out)], check=True)


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="Tight Studio, монтаж, звук: от проекта до готового mp4")
    p.add_argument("project", help="имя проекта в ~/Tight Studio Videos или путь к .tightstudio")
    p.add_argument("--caption-style", default="clean-modern")
    p.add_argument("--music", help="часть имени трека в медиатеке Tight Studio; без флага ролик без музыки")
    p.add_argument("--no-music", action="store_true", help="без музыки, даже если трек задан в tight_publish.local.json")
    p.add_argument("--music-volume", type=float, default=0.1, help="0.1 = 10%% как в Tight Studio")
    p.add_argument("--resolution", default="4k", choices=["1080p", "4k"])
    p.add_argument("--speed", type=float, default=1.1)
    p.add_argument("--whisper-model", default="small", help="модель для поиска меток «кат-кат»")
    p.add_argument("--marker", default="кат кат")
    p.add_argument("--language", default="ru", help="язык речи для меток и вычитки: ru, uk, en")
    p.add_argument("--export", help="готовый экспорт: пропустить Tight Studio")
    p.add_argument("--save", action="store_true", help="сохранить проект Tight Studio после экспорта")
    p.add_argument("--keep-cut", action="store_true", help="не удалять промежуточный файл после монтажа")
    p.add_argument("--no-caption-check", action="store_true", help="не вычитывать субтитры")
    p.add_argument("--caption-model", default="sonnet", help="модель Claude для вычитки субтитров")
    p.add_argument("--no-blur", action="store_true", help="не блюрить адрес и закладки браузера")
    local = Path(__file__).with_name("tight_publish.local.json")
    if local.exists():
        defaults = json.loads(local.read_text())
        unknown = set(defaults) - {a.dest for a in p._actions}
        if unknown:
            raise SystemExit(f"❌ В {local.name} неизвестные ключи: {', '.join(sorted(unknown))}")
        p.set_defaults(**defaults)
    args = p.parse_args()

    path = Path(args.project).expanduser()
    if path.suffix != ".tightstudio":
        path = PROJECTS_DIR / f"{args.project}.tightstudio"
    name = path.stem
    music = resolve_music(args.music) if args.music and not args.no_music else None
    t0 = time.time()

    if args.export:
        exported = Path(args.export).expanduser()
    else:
        if not path.exists():
            raise SystemExit(f"❌ Нет проекта {path}")
        ts, info = open_project(path, name)
        try:
            for clip in info["clips"]:
                if clip["type"] != "recording" or clip["hidden"]:
                    continue
                if clip["hasAudio"]:
                    transcribe(ts, clip, args.caption_style)
                    if not args.no_caption_check:
                        caption_check.review(ts, apply=True, model=args.caption_model,
                                            language=args.language)
                if not args.no_blur:
                    blur_browser.blur(ts, clip, path, apply=True)
            # Музыка только в постобработке. Если в проекте она есть, на экспорт громкость 0,
            # иначе в итоге два трека. Выключать нельзя: выключение стирает трек из проекта.
            audio = ts.call("audio.inspect", limit=1)["data"]
            own_volume = (audio.get("music") or {}).get("volume") if audio.get("musicMode") != "none" else None
            if own_volume:
                ts.call("audio.set_music", enabled=True, volume=0)
                print("🔇 В проекте есть своя музыка: на экспорт громкость 0")
            exported = export(ts, name, args.resolution)
            if own_volume:
                try:
                    ts.call("audio.set_music", enabled=True, volume=own_volume)
                except TightError as e:
                    print(f"   ⚠️ Громкость музыки в проекте не вернул: {e}")
            if args.save:
                ts.call("project.save")
                print("💾 Проект сохранён")
        finally:
            ts.close()

    cut = exported.with_name(f"{exported.stem} cut.mp4")
    final = exported.with_name(f"{exported.stem} final.mp4")
    print(f"\n✂️  Монтаж: паузы, «{args.marker}», x{args.speed}")
    subprocess.run([venv_python(), str(Path(__file__).parent / "smart_edit.py"), str(exported), str(cut),
                    "--speed", str(args.speed), "--whisper-model", args.whisper_model,
                    "--marker", args.marker, "--language", args.language, "--apply"], check=True)
    finish_audio(cut, final, music, args.music_volume)
    if not args.keep_cut:
        cut.unlink(missing_ok=True)

    print(f"\n✅ {final}")
    print(f"   {jc.get_duration(str(exported)) / 60:.1f} мин в экспорте, "
          f"{jc.get_duration(str(final)) / 60:.1f} мин в итоге, конвейер {(time.time() - t0) / 60:.1f} мин")
    return 0


if __name__ == "__main__":
    sys.exit(main())
