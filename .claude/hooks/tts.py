#!/usr/bin/env python3
"""Fluent TTS helper — turn a listening script into a playable audio clip.

Why this exists as a script instead of letting the tutor call an engine directly:

* The transcript must never appear in the chat or in a visible tool call — that
  spoils the exercise. The tutor passes `--text-file`; the text is read here and
  fed to the engine internally, so only a JSON line with the audio path is public.
* Every harness plays audio differently, so the deliverable is a file on disk that
  the caller can present, open with the OS player, or hand over as a path.

Engine chain (first one that works wins; force with --engine or $FLUENT_TTS_ENGINE):

  1. sherpa-onnx offline-tts  — offline, no Python deps, Apache-2.0. `--install` fetches it.
  2. piper CLI                — offline, pip-installable (`pip install piper-tts`), GPL-3.0.
  3. edge-tts                 — online, near-human quality, MIT client (`pip install edge-tts`).
  4. OS voice                 — Windows SAPI / macOS `say` / Linux `espeak-ng` (robotic, zero setup).

stdout is always exactly one JSON line; all progress and diagnostics go to stderr.
Exit code 0 on success, 1 on failure (with `ok: false` and a `hint` in the JSON).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fluent_paths import data_dir, force_utf8_io, plugin_root  # noqa: E402

SHERPA_VERSION = "v1.13.8"
RELEASE_BASE = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/{SHERPA_VERSION}/"
TTS_MODELS_TAG = "https://github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models"
VOICE_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
DEFAULT_VOICE = "vits-piper-en_US-lessac-medium"

# Release bundle per platform; each ships bin/sherpa-onnx-offline-tts plus its DLLs/.so.
BUNDLES = {
    ("Windows", "AMD64"): "win-x64-shared-MT-Release",
    ("Windows", "ARM64"): "win-arm64-shared-MT-Release",
    ("Linux", "x86_64"): "linux-x64-shared",
    ("Linux", "aarch64"): "linux-aarch64-shared",
    ("Darwin", "arm64"): "osx-arm64-shared",
    ("Darwin", "x86_64"): "osx-x64-shared",
}

# Verified sherpa-onnx voice assets (HEAD-checked against the tts-models release).
# Best effort: any other upstream asset name can still be passed to --voice.
LANG_VOICES = {
    "en": "vits-piper-en_US-lessac-medium",
    "en-us": "vits-piper-en_US-lessac-medium",
    "en-gb": "vits-piper-en_GB-alba-medium",
    "ar": "vits-piper-ar_JO-kareem-medium",
    "de": "vits-piper-de_DE-thorsten-medium",
    "es": "vits-piper-es_ES-davefx-medium",
    "fr": "vits-piper-fr_FR-siwis-medium",
    "it": "vits-piper-it_IT-paola-medium",
    "nl": "vits-piper-nl_BE-nathalie-medium",
    "pl": "vits-piper-pl_PL-darkman-medium",
    "pt": "vits-piper-pt_BR-faber-medium",
    "ru": "vits-piper-ru_RU-irina-medium",
    "sv": "vits-piper-sv_SE-nst-medium",
    "tr": "vits-piper-tr_TR-dfki-medium",
    "uk": "vits-piper-uk_UA-ukrainian_tts-medium",
    "vi": "vits-piper-vi_VN-vais1000-medium",
    "zh": "vits-icefall-zh-aishell3",
}

# Fallback voice per language when the chosen engine is edge-tts (its voice ids differ).
EDGE_VOICES = {
    "en": "en-US-AriaNeural", "en-gb": "en-GB-SoniaNeural", "ar": "ar-EG-SalmaNeural",
    "de": "de-DE-KatjaNeural", "es": "es-ES-ElviraNeural", "fr": "fr-FR-DeniseNeural",
    "it": "it-IT-ElsaNeural", "ja": "ja-JP-NanamiNeural", "ko": "ko-KR-SunHiNeural",
    "nl": "nl-NL-ColetteNeural", "pl": "pl-PL-ZofiaNeural", "pt": "pt-BR-FranciscaNeural",
    "ru": "ru-RU-SvetlanaNeural", "sv": "sv-SE-SofieNeural", "tr": "tr-TR-EmelNeural",
    "uk": "uk-UA-PolinaNeural", "vi": "vi-VN-HoaiMyNeural", "zh": "zh-CN-XiaoxiaoNeural",
}

OUT_EXTS = (".wav", ".mp3", ".aiff", ".ogg")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit(payload: dict, code: int = 0) -> int:
    """Print the single public JSON line and return the exit code.

    ASCII-only (json default) on purpose: the payload is read by a parser, and a
    Windows console with a legacy code page would otherwise mojibake any non-ASCII
    diagnostic text on its way through stdout.
    """
    print(json.dumps(payload), flush=True)
    return code


# --------------------------------------------------------------------------- homes

def candidate_homes() -> list[Path]:
    """Where the engine may live, in priority order.

    Explicit env first, then next to the learner's data, then repo-local scratch
    dirs. The scratch dirs exist because sandboxed harnesses (DSH) deny writes
    outside the workspace, so an install may legitimately land inside the repo.
    """
    homes: list[Path] = []
    env = os.environ.get("FLUENT_TTS_HOME")
    if env:
        homes.append(Path(env).expanduser().resolve())
    homes.append(data_dir() / "tts")
    root = plugin_root()
    homes.extend([root / ".tmp" / "tts", root / "temp" / "tts"])
    homes.append(Path(tempfile.gettempdir()) / "fluent-tts")
    seen, out = set(), []
    for h in homes:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def find_engine(home: Path) -> Path | None:
    """Locate the sherpa-onnx offline-tts CLI inside an engine home."""
    if not home.is_dir():
        return None
    for pat in ("sherpa-onnx-offline-tts.exe", "sherpa-onnx-offline-tts"):
        for p in sorted(home.rglob(pat)):
            if p.is_file():
                return p
    return None


def writable(d: Path) -> bool:
    """Probe actual writability — sandbox ACLs deny paths that look fine on paper."""
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".fluent-write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def engine_home(for_install: bool = False) -> tuple[Path, Path | None]:
    """Return (home, engine_path): first home that already has an engine, else the
    first writable one (which is where an install would go)."""
    for h in candidate_homes():
        exe = find_engine(h)
        if exe:
            return h, exe
    for h in candidate_homes():
        if writable(h):
            return h, None
    return candidate_homes()[0], None


def installed_voices(home: Path) -> list[Path]:
    models = home / "models"
    if not models.is_dir():
        return []
    return sorted(p for p in models.iterdir() if p.is_dir() and list(p.glob("*.onnx")))


def has_voice(home: Path, name: str) -> bool:
    """Exact or suffix match — install idempotency must not be fooled by a *different*
    installed voice that resolve_voice() would happily fall back to."""
    want = voice_asset(name).lower()
    return any(v.name.lower() == want or v.name.lower().endswith(want) for v in installed_voices(home))


def resolve_voice(home: Path, wanted: str | None) -> Path | None:
    """Match a voice dir by exact name, then by substring, then fall back to the default."""
    voices = installed_voices(home)
    if not voices:
        return None
    if wanted:
        want = wanted.strip().lower()
        exact = [v for v in voices if v.name.lower() == want]
        if exact:
            return exact[0]
        near = [v for v in voices if want in v.name.lower() or want in v.name.lower().replace("vits-piper-", "")]
        if near:
            return near[0]
    for v in voices:
        if v.name.lower() == DEFAULT_VOICE.lower():
            return v
    return voices[0]


def voice_asset(name: str) -> str:
    """'en_GB-alba-medium' -> 'vits-piper-en_GB-alba-medium'; leave full asset names alone."""
    n = (name or "").strip()
    if n.startswith(("vits-", "kokoro", "matcha", "sherpa-onnx-")):
        return n
    return f"vits-piper-{n}"


def lang_of(voice: str | None, fallback: str = "en") -> str:
    """'en_US-lessac-medium' / 'vits-piper-en_GB-alba-medium' -> 'en' / 'en-gb'."""
    if not voice:
        return fallback
    m = re.search(r"([a-z]{2})[_-]([A-Za-z]{2})", voice)
    if m:
        lang = f"{m.group(1).lower()}-{m.group(2).lower()}"
        return lang if lang in EDGE_VOICES else m.group(1).lower()
    m = re.search(r"\b([a-z]{2})\b", voice.lower())
    return m.group(1) if m else fallback


# ------------------------------------------------------------------------- install

def download(url: str, dest: Path) -> None:
    name = url.rsplit("/", 1)[-1]
    log(f"  downloading {name} ...")
    t0 = time.time()
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length", 0))
        got = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            got += len(chunk)
            if got % (16 << 20) < (1 << 20):
                log(f"    {got / 1048576:.0f}/{total / 1048576:.0f} MB")
    tmp.replace(dest)
    log(f"    {dest.stat().st_size / 1048576:.1f} MB in {time.time() - t0:.1f}s")


def install(home: Path, voice: str, force: bool = False) -> dict:
    """Fetch the sherpa-onnx CLI bundle + one voice (~90 MB total). Idempotent."""
    key = (platform.system(), platform.machine())
    suffix = BUNDLES.get(key)
    if not suffix:
        return {"ok": False, "error": f"no prebuilt sherpa-onnx bundle for {key[0]}/{key[1]}",
                "hint": f"Use another engine instead: pip install edge-tts (online) — see {RELEASE_BASE}"}

    home.mkdir(parents=True, exist_ok=True)
    if force or not find_engine(home):
        url = f"{RELEASE_BASE}sherpa-onnx-{SHERPA_VERSION}-{suffix}.tar.bz2"
        tar = home / f"sherpa-onnx-{SHERPA_VERSION}-{suffix}.tar.bz2"
        try:
            download(url, tar)
        except Exception as e:  # noqa: BLE001 - surface any network/404 as JSON
            return {"ok": False, "error": f"download failed: {e}", "hint": f"Check network access to {url}"}
        unpack(tar, home, "engine")

    if force or not has_voice(home, voice):
        asset = voice_asset(voice)
        url = f"{VOICE_BASE}{asset}.tar.bz2"
        tar = home / f"{asset}.tar.bz2"
        try:
            download(url, tar)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"download failed: {e}",
                    "hint": f"'{asset}' may not exist upstream — browse {TTS_MODELS_TAG}"}
        unpack(tar, home / "models", "voice")

    return status(home)


def unpack(tar: Path, dest: Path, label: str) -> None:
    log(f"  extracting {label} ...")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar, "r:bz2") as tf:
        tf.extractall(dest)
    tar.unlink(missing_ok=True)


def scratch_dir() -> Path:
    """A directory this process can actually write to (sandboxes deny the system temp)."""
    for d in (plugin_root() / ".tmp", data_dir(), Path(tempfile.gettempdir())):
        if writable(d):
            return d
    return Path(tempfile.gettempdir())


def status(home: Path | None = None) -> dict:
    """Engine + voice report; drives --check and the tail of --install."""
    home, exe = engine_home() if home is None else (home, find_engine(home))
    voices = [v.name for v in installed_voices(home)]
    info: dict = {"ok": bool(exe), "engine": None, "engine_path": None,
                  "tts_home": str(home), "voices": voices, "voice": None}
    if exe:
        v = resolve_voice(home, os.environ.get("FLUENT_TTS_VOICE"))
        info.update(engine="sherpa-onnx", engine_path=str(exe), voice=v.name if v else None)
        return info

    # No local engine: report which fallback the synthesizer would actually pick.
    if importable("edge_tts"):
        info.update(ok=True, engine="edge-tts",
                    hint=f"No local engine installed — edge-tts will be used. Run --install for offline audio. {TTS_MODELS_TAG}")
        return info
    if shutil.which("piper"):
        info.update(ok=True, engine="piper", hint="Using the pip-installed piper CLI.")
        return info
    if platform.system() == "Windows":
        # Enumerating voices is not enough — a machine can list a voice whose engine
        # files are missing, and only a real Speak() reveals that. Probe once.
        try:
            probe = scratch_dir() / ".tts-probe.wav"
            try:
                synth_sapi("test", probe, 1.0)
            finally:
                probe.unlink(missing_ok=True)  # a failed probe can still leave a 0-byte file
            info.update(ok=True, engine="os:sapi",
                        hint="Using a system voice: robotic. Run --install for a neural voice.")
            return info
        except Exception as e:  # noqa: BLE001
            info["hint"] = (f"Windows has no usable speech voice ({e}). "
                            "Run: python3 .claude/hooks/tts.py --install — or add a voice in "
                            "Settings → Time & Language → Speech.")
            return info
    for name, probe in (("say", platform.system() == "Darwin"),
                        ("espeak-ng", bool(shutil.which("espeak-ng")))):
        if probe:
            info.update(ok=True, engine=f"os:{name}",
                        hint="Using a system voice: robotic. Run --install for a neural voice.")
            return info

    info["hint"] = "No TTS engine available. Run: python3 .claude/hooks/tts.py --install"
    if os.environ.get("FLUENT_TTS_ENGINE"):
        info["hint"] += f" (FLUENT_TTS_ENGINE={os.environ['FLUENT_TTS_ENGINE']} is set)"
    return info


def importable(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------------- engines

def synth_sherpa(exe: Path, home: Path, text: str, out: Path, voice: str | None, speed: float) -> dict:
    v = resolve_voice(home, voice)
    if not v:
        raise RuntimeError("no voice model installed — run --install")
    onnx = next((p for p in sorted(v.glob("*.onnx")) if "int8" not in p.name), None) or sorted(v.glob("*.onnx"))[0]
    tokens = v / "tokens.txt"
    if not tokens.exists():
        raise RuntimeError(f"{v.name} has no tokens.txt")
    cmd = [str(exe), f"--vits-model={onnx}", f"--vits-tokens={tokens}",
           f"--output-filename={out}", f"--speed={speed}",
           f"--num-threads={max(1, (os.cpu_count() or 2) // 2)}"]
    # Piper/VITS models phonemize via espeak-ng data; icefall models ship a lexicon instead.
    if (v / "espeak-ng-data").is_dir():
        cmd.append(f"--vits-data-dir={v / 'espeak-ng-data'}")
    elif (v / "lexicon.txt").exists():
        cmd.append(f"--vits-lexicon={v / 'lexicon.txt'}")
    cmd.append(text)
    run(cmd, cwd=v)
    ensure_audio(out)
    return {"engine": "sherpa-onnx", "voice": v.name}


def synth_piper(text: str, out: Path, home: Path, voice: str | None, speed: float) -> dict:
    piper = shutil.which("piper") or shutil.which("piper.exe")
    if not piper:
        raise RuntimeError("piper not found on PATH")
    v = resolve_voice(home, voice)
    if not v:
        raise RuntimeError("piper needs a voice model — run --install or pass --voice")
    onnx = sorted(v.glob("*.onnx"))[0]
    cmd = [piper, "-m", str(onnx), "-f", str(out), "--length-scale", f"{1.0 / speed:.2f}"]
    cfg = onnx.with_suffix(".onnx.json")
    if cfg.exists():
        cmd += ["-c", str(cfg)]
    run(cmd, cwd=v, stdin=text.encode("utf-8"))
    ensure_audio(out)
    return {"engine": "piper", "voice": v.name}


def synth_edge(text: str, out: Path, voice: str | None, speed: float) -> dict:
    import asyncio

    import edge_tts  # type: ignore

    name = voice if voice and "Neural" in voice else EDGE_VOICES.get(lang_of(voice), EDGE_VOICES["en"])
    asyncio.run(edge_tts.Communicate(text, name, rate=f"{round((speed - 1) * 100):+d}%").save(str(out)))
    ensure_audio(out)
    return {"engine": "edge-tts", "voice": name}


def powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def synth_sapi(text: str, out: Path, speed: float) -> dict:
    """Windows built-in voice. Needs the Speech feature plus an installed voice, which
    many machines lack — that failure is reported, never silently swallowed."""
    shell = powershell()
    if not shell:
        raise RuntimeError("no powershell available for SAPI")
    txt = write_temp_text(text)
    try:
        ps = (
            "$ErrorActionPreference = 'Stop'; "
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate = {max(-10, min(10, round((speed - 1) * 10)))}; "
            f"$s.SetOutputToWaveFile('{out}'); "
            f"$s.Speak([System.IO.File]::ReadAllText('{txt}', [System.Text.Encoding]::UTF8)); "
            "$s.Dispose()"
        )
        run([shell, "-NoProfile", "-NonInteractive", "-Command", ps])
    finally:
        Path(txt).unlink(missing_ok=True)
    ensure_audio(out, "no usable Windows speech voice — add one in Settings → Time & Language → Speech, or run --install")
    return {"engine": "os:sapi", "voice": "system default"}


def synth_os(text: str, out: Path, speed: float) -> dict:
    system = platform.system()
    if system == "Windows":
        return synth_sapi(text, out, speed)
    txt = write_temp_text(text)
    try:
        if system == "Darwin":
            run(["say", "-f", txt, "-o", str(out), "-r", str(round(175 * speed))])
            ensure_audio(out)
            return {"engine": "os:say", "voice": "system default"}
        espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        if not espeak:
            raise RuntimeError("no system voice available (espeak-ng missing)")
        run([espeak, "-f", txt, "-w", str(out), "-s", str(round(175 * speed))])
        ensure_audio(out)
        return {"engine": "os:espeak-ng", "voice": "system default"}
    finally:
        Path(txt).unlink(missing_ok=True)


def write_temp_text(text: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(text)
        return tf.name


def run(cmd: list[str], cwd: Path | None = None, stdin: bytes | None = None) -> None:
    """Run an engine, keeping its noisy progress output off stdout."""
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, input=stdin,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        reason = error_tail(p.stderr) or error_tail(p.stdout) or "no output"
        raise RuntimeError(f"{Path(cmd[0]).name} exited {p.returncode}: {reason}")


def error_tail(blob: bytes | None, n: int = 3) -> str:
    """Engine stderr minus the shell decoration, so the actual reason survives.

    PowerShell wraps every error in '+ ~~~' / 'At line:' / 'CategoryInfo' lines;
    a learner reading the hint needs the one line that says what went wrong.
    """
    lines = [s for s in (x.strip() for x in (blob or b"").decode("utf-8", "replace").splitlines())
             if s and not s.startswith(("+", "At line:", "CategoryInfo", "FullyQualifiedErrorId"))]
    return " | ".join(lines[-n:])


def ensure_audio(path: Path, hint: str = "") -> None:
    """An engine that exits 0 but writes an empty file is a failure, and the reason is
    what the learner needs (e.g. a Windows voice whose engine files are missing)."""
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"engine produced no audio{f' — {hint}' if hint else ''}")


# ----------------------------------------------------------------------- playback

def default_out_dir() -> Path:
    """Prefer <data_dir>/listening, else repo scratch (sandboxes), else the temp dir."""
    for d in (data_dir() / "listening", plugin_root() / ".tmp" / "listening",
              plugin_root() / "temp" / "listening", Path(tempfile.gettempdir()) / "fluent-listening"):
        if writable(d):
            return d
    return Path(tempfile.gettempdir())


def wav_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path)) as w:
            return round(w.getnframes() / w.getframerate(), 1)
    except Exception:  # noqa: BLE001 - mp3/aiff simply report no duration
        return None


def play(path: Path) -> None:
    if platform.system() == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    force_utf8_io()
    ap = argparse.ArgumentParser(description="Fluent TTS — synthesize a listening clip.")
    ap.add_argument("--text-file", help="file holding the script (preferred: keeps the text out of the chat)")
    ap.add_argument("--text", help="inline text (avoid: it leaks the transcript into the visible command)")
    ap.add_argument("--out", help="explicit output file path")
    ap.add_argument("--out-dir", help="output directory (default: <data_dir>/listening)")
    ap.add_argument("--out-name", help="output file name (default: clip-<timestamp>.wav)")
    ap.add_argument("--voice", default=os.environ.get("FLUENT_TTS_VOICE"),
                    help="voice name, e.g. en_US-lessac-medium (default: $FLUENT_TTS_VOICE or the installed default)")
    ap.add_argument("--speed", type=float, default=1.0, help="1.0 normal, 0.85 slower (graded listening)")
    ap.add_argument("--engine", default=os.environ.get("FLUENT_TTS_ENGINE", "auto"),
                    choices=["auto", "sherpa", "piper", "edge", "os"], help="force an engine")
    ap.add_argument("--play", action="store_true", help="open the clip in the OS player when done")
    ap.add_argument("--check", action="store_true", help="report engine status and exit")
    ap.add_argument("--install", action="store_true", help="download the offline engine + voice (~90 MB)")
    ap.add_argument("--lang", default="en", help="with --install: default voice for this language")
    ap.add_argument("--list-voices", action="store_true", help="list installed local voices")
    ap.add_argument("--force", action="store_true", help="with --install: re-download even if present")
    args = ap.parse_args(argv)

    if args.check:
        info = status()
        return emit(info, 0 if info.get("ok") else 1)

    if args.install:
        voice = args.voice or LANG_VOICES.get(args.lang.lower(), DEFAULT_VOICE)
        home, _ = engine_home(for_install=True)
        log(f"Installing offline TTS into {home} (voice {voice_asset(voice)})")
        info = install(home, voice, force=args.force)
        if info.get("ok"):
            info["installed_into"] = str(home)
        return emit(info, 0 if info.get("ok") else 1)

    home, exe = engine_home()

    if args.list_voices:
        return emit({"ok": True, "tts_home": str(home), "voices": [v.name for v in installed_voices(home)]})

    if args.text_file:
        src = Path(args.text_file).expanduser()
        if not src.is_file():
            return emit({"ok": False, "error": f"text file not found: {src}"}, 1)
        text = src.read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    else:
        return emit({"ok": False, "error": "nothing to synthesize: pass --text-file (preferred) or --text"}, 1)

    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return emit({"ok": False, "error": "script is empty"}, 1)

    if args.out:
        out = Path(args.out).expanduser()
    else:
        out_dir = Path(args.out_dir).expanduser() if args.out_dir else default_out_dir()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return emit({"ok": False, "error": f"output dir not writable: {out_dir} ({e})"}, 1)
        out = out_dir / (args.out_name or f"clip-{time.strftime('%Y%m%d-%H%M%S')}.wav")
    # OS voices on macOS/other engines may force a different container.
    out = out if out.suffix.lower() in OUT_EXTS else out.with_suffix(".wav")
    # Absolute: sherpa/piper are spawned with the voice dir as cwd, so a relative
    # --output-filename would be written next to the model instead of where asked.
    out = out.resolve()

    want = args.engine
    attempts: list[tuple[str, callable]] = []
    if want in ("auto", "sherpa") and exe:
        attempts.append(("sherpa-onnx", lambda: synth_sherpa(exe, home, text, out, args.voice, args.speed)))
    if want in ("auto", "piper") and (shutil.which("piper") or shutil.which("piper.exe")):
        attempts.append(("piper", lambda: synth_piper(text, out, home, args.voice, args.speed)))
    if want in ("auto", "edge") and importable("edge_tts"):
        attempts.append(("edge-tts", lambda: synth_edge(text, out.with_suffix(".mp3"), args.voice, args.speed)))
    if want == "os" or (want == "auto" and not attempts):
        ext = ".aiff" if platform.system() == "Darwin" else ".wav"
        attempts.append(("os", lambda: synth_os(text, out.with_suffix(ext), args.speed)))

    if not attempts:
        return emit({"ok": False, "error": "no usable TTS engine",
                     "hint": "Run: python3 .claude/hooks/tts.py --install"}, 1)

    errors: list[str] = []
    for name, fn in attempts:
        log(f"  engine: {name}")
        try:
            meta = fn()
        except Exception as e:  # noqa: BLE001 - try the next engine, keep the reason
            errors.append(f"{name}: {e}")
            log(f"    failed: {e}")
            continue
        produced = next((p for p in (out, *(out.with_suffix(x) for x in OUT_EXTS)) if p.exists() and p.stat().st_size > 0), None)
        if not produced:
            errors.append(f"{name}: produced no audio")
            # A failed engine can still leave a 0-byte placeholder (Windows SAPI does).
            for p in (out, *(out.with_suffix(x) for x in OUT_EXTS)):
                if p.exists() and p.stat().st_size == 0:
                    p.unlink(missing_ok=True)
            continue
        if args.play:
            play(produced)
        return emit({"ok": True, "path": str(produced), "bytes": produced.stat().st_size,
                     "seconds": wav_seconds(produced), "speed": args.speed, **meta})

    return emit({"ok": False, "error": "all engines failed", "attempts": errors,
                 "hint": "python3 .claude/hooks/tts.py --check for engine status, --install to add the offline engine"}, 1)


if __name__ == "__main__":
    sys.exit(main())
