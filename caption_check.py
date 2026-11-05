#!/usr/bin/env python3
"""
Вычитка субтитров Tight Studio перед экспортом.

Что правит:
  - имена и термины по словарю captions-glossary.txt (известные искажения заменяются сразу);
  - звуки-паузы («э-э-э», «эм», «а-а-а») и обрывки слов («ш--»);
  - пунктуацию и явные ошибки распознавания: это делает Claude CLI (claude -p) по всей
    расшифровке сразу, ответ строго по JSON-схеме.

Предохранитель: правка сегмента принимается, только если в ней нет новых слов, кроме
терминов из словаря и исправлений, похожих на исходное слово, и удалены только паузы
и обрывки. Иначе сегмент остаётся как был, причина пишется в лог.

Tight Studio при замене текста раскидывает время слов пропорционально длине, подсветка
слов уезжает. Поэтому после замены время каждого слова восстанавливается по исходным.

Сама по себе показывает правки, с --apply пишет их в открытый проект:
    python3 caption_check.py --language uk
    python3 caption_check.py --language uk --apply
"""

import argparse
import difflib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

GLOSSARY = Path(__file__).resolve().parent / "captions-glossary.txt"

LANGUAGES = {"ru": "русский", "uk": "украинский", "en": "английский"}
# Звуки-паузы по языку речи. «ем» в украинском пауза, в русском глагол, поэтому раздельно.
FILLERS = {"ru": r"э+|эм+|м+|ммм+|а{2,}|хм+", "uk": r"е+|ем+|э+|эм+|м+|ммм+|а{2,}|хм+", "en": r"m+|mm+"}


def filler_re(language: str) -> re.Pattern:
    return re.compile(rf"^({FILLERS.get(language, FILLERS['ru'])}|um+|uh+|ah+|eh+|er+m?|hmm+)$")


FILLER = filler_re("ru")
FRAGMENT = re.compile(r"^\w{1,4}-{1,3}[.,!?…]*$")  # обрывок «ш--», «с-»

SYSTEM_PROMPT = """Ты корректор субтитров к видео. Тебе дают сегменты автоматической расшифровки речи, язык речи: {language}, внутри бывают английские термины. Язык субтитров не меняешь и ничего не переводишь. Верни только сегменты, которые нужно поправить, с исправленным текстом. Сегменты идут подряд, предложение может переходить из одного в следующий.

Правишь:
1. Имена и термины по словарю: если распознавание исказило название, ставь написание из словаря. Названия на языке речи склоняешь по падежу, латинские оставляешь как есть.
2. Звуки-паузы и запинки: «э-э-э», «е-е-е», «эм», «м-м», «а-а-а», «um», «uh» как звук раздумья, обрывки слов вроде «ш--», «с--», повтор слова при запинке («на, на одну», «уж уже»). Удаляешь их и чинишь пунктуацию вокруг: «этапов, э-э-э, производства» становится «этапов производства», без лишних запятых.
3. Пунктуацию во всех сегментах, даже где больше нечего править: запятые по правилам языка речи, точки, вопросительные знаки, заглавная буква в начале предложения. Вводные слова выделяются запятыми.
4. Явные ошибки распознавания, когда по контексту однозначно понятно, какое слово сказано.

Не правишь:
- не перефразируешь, не меняешь порядок слов, ничего не сокращаешь;
- разговорные слова оставляешь («короче», «типа», «ну», «вот», «в общем», «типу», «от», «like»), это живая речь;
- английские слова не переводишь и не меняешь, если их нет в словаре;
- не добавляешь слов, которых нет в записи, кроме исправления искажённого термина.

Словарь (правильное написание):
{terms}"""

SCHEMA = {
    "type": "object",
    "properties": {"segments": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
        "required": ["id", "text"]}}},
    "required": ["segments"],
}


def norm(word: str) -> str:
    return "".join(c for c in word.lower().replace("ё", "е") if c.isalnum())


def load_glossary(path: Path = GLOSSARY) -> tuple[list[str], list[tuple[re.Pattern, str]]]:
    terms, fixes = [], []
    if not path.exists():
        return terms, fixes
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        term, _, wrong = line.partition(":")
        terms.append(term.strip())
        for variant in filter(None, (v.strip() for v in wrong.split(","))):
            fixes.append((re.compile(rf"(?<!\w){re.escape(variant)}(?!\w)", re.I), term.strip()))
    return terms, fixes


# ---------------------------------------------------------------- субтитры из Tight Studio

def fetch_segments(ts) -> list[dict]:
    """Все сегменты активного клипа: id, слова (с временем) и текст."""
    out, offset, limit = [], 0, 20
    while True:
        try:
            d = ts.call("captions.inspect", offset=offset, limit=limit)["data"]
        except RuntimeError as e:  # ответ моста ограничен 32 тыс. символов
            if "too much" not in str(e) or limit == 1:
                raise
            limit = max(1, limit // 2)
            continue
        for s in d["segments"]:
            words = [w for w in s["words"] if w.get("kept", True)]
            out.append({"id": s["id"], "index": len(out), "words": words,
                        "text": " ".join(w["text"] for w in words)})
        offset += len(d["segments"])
        if not d.get("hasMore") or not d["segments"]:
            return out


# ---------------------------------------------------------------- правки

def rule_pass(text: str, fixes) -> str:
    for pattern, term in fixes:
        text = pattern.sub(term, text)
    words = [w for w in text.split() if not FILLER.match(norm(w)) and not FRAGMENT.match(w)]
    return " ".join(words)


def ask_claude(segments: list[dict], terms: list[str], model: str, language: str) -> dict[str, str]:
    claude = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    payload = json.dumps({"segments": [{"id": s["id"], "text": s["draft"]} for s in segments]},
                         ensure_ascii=False)
    with tempfile.TemporaryDirectory() as cwd:  # вне репо: без CLAUDE.md проекта
        r = subprocess.run(
            [claude, "-p", "--model", model, "--tools", "", "--setting-sources", "user",
             "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
             "--system-prompt", SYSTEM_PROMPT.format(language=LANGUAGES.get(language, language),
                                                    terms="\n".join(terms)),
             "--json-schema", json.dumps(SCHEMA),
             "Вычитай сегменты субтитров из stdin."],
            input=payload, capture_output=True, text=True, cwd=cwd, timeout=600)
    if r.returncode:
        raise RuntimeError(f"claude -p: {r.stderr.strip()[-500:] or r.stdout.strip()[-500:]}")
    res = json.loads(r.stdout)
    if res.get("is_error"):
        raise RuntimeError(f"claude -p: {res.get('result')}")
    return {s["id"]: s["text"] for s in res["structured_output"]["segments"]}


def check(old: list[str], new: list[str], terms: list[str]) -> str | None:
    """Причина отказа или None, если правка безопасна."""
    if not new:
        return "пустой текст"
    allowed = {norm(w) for t in terms for w in t.split()}
    a, b = [norm(w) for w in old], [norm(w) for w in new]

    def droppable(i: int) -> bool:
        """Пауза, обрывок или запинка: слово повторяет соседнее или является его началом."""
        if FILLER.match(a[i]) or FRAGMENT.match(old[i]):
            return True
        for j in (i - 1, i + 1):
            if 0 <= j < len(a) and a[i] and a[j]:
                x, y = a[i], a[j]
                if x == y or (min(len(x), len(y)) >= 2 and (x.startswith(y) or y.startswith(x))):
                    return True
        return False

    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        real = [old[i] for i in range(i1, i2) if not droppable(i)]
        came = [w for w in b[j1:j2] if w]
        if op == "delete" and real:
            return f"удалено слово «{real[0]}»"
        def term(w: str) -> bool:  # слово из словаря, в том числе с падежным окончанием
            return w in allowed or any(len(t) >= 4 and w.startswith(t) and len(w) - len(t) <= 3 for t in allowed)

        for w in came:
            near = max((difflib.SequenceMatcher(None, w, norm(r)).ratio() for r in real), default=0)
            if not term(w) and near < 0.5:
                return f"новое слово «{w}»"
        if len(came) < len(real) and not any(term(w) for w in came):
            return f"выпало слово из «{' '.join(real)}»"
    return None


def retime(old_words: list[dict], new_texts: list[str]) -> list[tuple[float, float]]:
    """Время новых слов по исходным: совпавшие берут своё, заменённые делят отрезок."""
    a = [norm(w["text"]) for w in old_words]
    b = [norm(t) for t in new_texts]
    times: list[tuple[float, float] | None] = [None] * len(b)
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(j2 - j1):
                times[j1 + k] = (old_words[i1 + k]["startTime"], old_words[i1 + k]["endTime"])
        elif op in ("replace", "insert"):
            if op == "replace":
                lo, hi = old_words[i1]["startTime"], old_words[i2 - 1]["endTime"]
            else:
                lo = old_words[i1 - 1]["endTime"] if i1 > 0 else old_words[0]["startTime"]
                hi = old_words[i1]["startTime"] if i1 < len(old_words) else lo + 0.3
                hi = max(hi, lo + 0.1 * (j2 - j1))
            total = sum(max(len(x), 1) for x in b[j1:j2])
            t = lo
            for k in range(j1, j2):
                dur = (hi - lo) * max(len(b[k]), 1) / total
                times[k] = (t, t + dur)
                t += dur
    fixed, prev = [], 0.0
    for start, end in times:
        start = max(start, prev)
        end = max(end, start + 0.02)
        fixed.append((round(start, 3), round(end, 3)))
        prev = end
    return fixed


def apply_segment(ts, seg: dict, new_text: str):
    ts.call("captions.replace_segment_text", segment_id=seg["id"], text=new_text)
    d = ts.call("captions.inspect", offset=seg["index"], limit=1)["data"]
    fresh = d["segments"][0] if d["segments"] and d["segments"][0]["id"] == seg["id"] else None
    if not fresh:
        raise RuntimeError(f"сегмент {seg['id']} не нашёлся после замены")
    words = [w for w in fresh["words"] if w.get("kept", True)]
    for w, (start, end) in zip(words, retime(seg["words"], [w["text"] for w in words])):
        ts.call("captions.update_word_timing", word_id=w["id"], start_time=start, end_time=end)


def review(ts, apply: bool, model: str = "sonnet", use_claude: bool = True, language: str = "ru") -> int:
    """Вычитать субтитры активного клипа. Возвращает число исправленных сегментов."""
    global FILLER
    FILLER = filler_re(language)
    terms, fixes = load_glossary()
    segments = fetch_segments(ts)
    for s in segments:
        s["draft"] = rule_pass(s["text"], fixes)

    proposed = {s["id"]: s["draft"] for s in segments if s["draft"] != s["text"]}
    if use_claude:
        try:
            proposed.update(ask_claude(segments, terms, model, language))
        except (RuntimeError, subprocess.TimeoutExpired, ValueError, KeyError) as e:
            print(f"   ⚠️ Claude не ответил ({e}), остаются правки по словарю и паузам")

    changed = 0
    for s in segments:
        new_text = proposed.get(s["id"], s["text"]).strip()
        if new_text == s["text"]:
            continue
        reason = check(s["text"].split(), new_text.split(), terms)
        if reason:
            print(f"   ⛔ {s['text']}\n      отклонено ({reason}): {new_text}")
            continue
        print(f"   - {s['text']}\n   + {new_text}")
        if apply:
            apply_segment(ts, s, new_text)
        changed += 1
    verb = "исправлено" if apply else "к правке"
    print(f"   субтитры: {verb} {changed} из {len(segments)} сегментов")
    return changed


def main():
    p = argparse.ArgumentParser(description="Вычитка субтитров открытого проекта Tight Studio")
    p.add_argument("--apply", action="store_true", help="записать правки в проект")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--no-claude", action="store_true", help="только словарь и паузы")
    p.add_argument("--language", default="ru", help="язык речи: ru, uk, en")
    args = p.parse_args()

    import tight_publish as tp
    ts = tp.TightStudio()
    try:
        info = ts.call("project.inspect")["data"]
        print(f"📝 Субтитры «{info['name']}»")
        review(ts, args.apply, args.model, not args.no_claude, args.language)
    finally:
        ts.close()


if __name__ == "__main__":
    main()
