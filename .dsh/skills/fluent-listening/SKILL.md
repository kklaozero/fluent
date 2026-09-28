---
name: fluent-listening
description: Run an interactive listening comprehension session — writes a level-matched target-language script, synthesizes it to an audio clip with TTS, has the learner listen (max two plays), then asks main-idea, detail, number, and inference questions one at a time with immediate feedback. Triggered only when the learner types /fluent-listening.
disable-model-invocation: true
---
## Running in DeepSeek Harness (DSH)

Migrated from Claude Code (`.claude/skills/`). DSH runs PowerShell on Windows:

- Use `python` (not `python3`) — Python 3.14.6 is installed; `python3` is not on PATH.
- `${CLAUDE_PLUGIN_ROOT:-${CLAUDE_PROJECT_DIR:-.}}` was a Claude Code idiom for the repo root. Run commands from the fluent repo root (your DSH session workspace) so `.claude/hooks/...` resolves, or substitute `$(git rev-parse --show-toplevel)`.
- Bash heredocs (`<<'EOF'`) are not PowerShell: write the JSON payload to a temp file, then `Get-Content payload.json | python .claude/hooks/update-db.py`.
- PowerShell has no `\` line continuation — keep `tts.py` calls on one line (or use a backtick).

# Listening Comprehension Session

## Overview

Generate one audio clip (monologue or two-person dialogue), have the learner listen to it, then ask 4-6 comprehension questions one at a time. No dictation, no transcript until the end — the learner must decode speech in real time, which is the skill reading practice cannot train.

The script is built **from the learner's own data**: due reviews, past mistakes, and focus areas. Their hardest words are the ones they hear. That is what makes this skill worth more than a generic podcast.

## When to Use

Trigger this skill only when the learner types `/fluent-listening`. The skill is gated with `disable-model-invocation: true` — a 15-20 min session with TTS calls and DB writes should never start from an ambiguous prompt.

Skip this skill below A1 mastery 3 — at that stage single-word audio (`/fluent-vocab` with audio) is more useful than connected speech.

## Instructions

### 1. Load context

```bash
python .claude/hooks/read-db.py
```

Need: `learner-profile` (level, target language, focus areas, preferences), `spaced-repetition.review_queue` (due items to recycle into the script), `mistakes-db.error_patterns` (weak patterns), `mastery-db.skills.listening`.

### 2. Check the audio engine before promising anything

```bash
python .claude/hooks/tts.py --check
```

If the engine is not ready, offer the learner the one-time install (one command, ~90 MB, Apache-2.0 engine + a local en_US voice) and stop until it is done:

```bash
python .claude/hooks/tts.py --install
```

### 3. Opening

```markdown
# 🎧 {target_language} Listening Practice

Hi {name}!

Today we're training your **ear**. I'll play you a short {target_language} clip, you listen, then I ask what you understood.

**Level:** {CEFR} · **Length:** ~{seconds}s · **Plays:** 2 max
**Accent:** {accent} · **Speed:** {speed}×

**How to do this well:**
- Listen once for the gist — don't try to catch every word
- If you must, ask for one replay (I can also slow it down)
- Answer in {native_language} — I'm testing comprehension, not production

**Ready? Press play.** 🎧
```

### 4. Plan the clip (do this in one pass, before writing the script)

| CEFR | Length | Words | Speed | Questions |
|------|--------|-------|-------|-----------|
| A1-A2 | 25-45 s | 55-90 | 0.85× | 4 |
| B1 | 60-75 s | 110-160 | 0.95× | 5 |
| B2+ | 90-120 s | 180-250 | 1.0× | 6 |

Pick:
- **Format** — voicemail, stand-up update, meeting excerpt, shop/airport announcement, two-colleague dialogue, podcast snippet, doctor's appointment. Match `learner-profile.focus_areas` and the learner's motivation (work, exam, travel).
- **Accent** — vary it across sessions (`en-US` / `en-GB`); switch only when the learner's level is 4+ for listening, otherwise the same accent for a few sessions builds confidence.
- **Recycled items** — deliberately weave in 3-5 items from `review_queue.today` and `mistakes-db`, plus one **number** (price, time, flight number, deadline) and one **proper name**. Numbers and names are where comprehension silently fails.
- **Trap-free but not trivial** — the answer must be inferable from the audio, never from world knowledge.

### 5. Write the script to a file — NOT into the chat

Write the transcript with the `write` tool to a scratch file **inside the repo** (`<repo>/.tmp/listening/script-{session}.txt`, git-ignored). Keep audio and scripts out of `<data_dir>` — that directory is often a cloud-synced folder, and only the finished session report belongs in it.

**Then synthesize it.** Always pass the file, never inline text — an inline `--text "..."` would put the transcript in the visible tool call and spoil the exercise:

```bash
python .claude/hooks/tts.py --text-file "<abs path>/script-{session}.txt" --out-dir "<abs path>" --out-name "clip-{session}-1.wav" --voice "${FLUENT_TTS_VOICE:-en_US-lessac-medium}" --speed {0.85|0.95|1.0}
```

The script prints one JSON line: `{"ok":true,"path":"...","engine":"...","seconds":N}`. Use `path` as-is.

**Hand the clip to the learner.** If your harness has a file-presentation tool, present that path. Otherwise run the same command with `--play` (opens the OS player) and print the absolute path. Never print the transcript.

For a two-person dialogue, synthesize each speaker separately (`--out-name clip-{session}-1a.wav` / `-1b.wav`) and present them in order; keep voice-contrast voices (`--list-voices` shows installed ones).

### 6. Question sequence (one at a time)

Ask in {target_language} from A2 up. Rotate the types; never ask about a detail you would not expect a native listener to keep.

**Main idea:**
```markdown
## Vraag 1: Hoofdidee (main idea)

{question in target language}

a) {option}
b) {option}
c) {option}

**Type a, b, or c:**
```

**Key detail:**
```markdown
## Question 2: Detail

{specific question}

**Type your answer:**
```

**Number / time / name:**
```markdown
## Question 3: Numbers

{what time / how much / which number?}

**Type your answer:**
```

**Inference / intent:**
```markdown
## Question 4: What does he mean?

{question about intent, tone, or an unstated conclusion}

**Answer in {target_language}:**
```

### 7. Feedback per question

Use the `fluent-feedback-formatter` skill. Listening-specific addition: **quote the audio line** in the explanation so the learner can map sound to meaning.

```markdown
{✅ or ❌}

**Answer:** {correct_answer}

**Explanation:** {why}

**You heard:** "{the exact line from the script}"
{If they misheard a word: **Minimal pair:** {heard} vs {actual} — {one-line pronunciation note}}

**Score: {X}/10**

---
```

If the learner gets 3+ questions wrong on one clip, the clip was too hard, not the learner: replay it at `--speed 0.8` before continuing, and note it in `focus_next_session`.

### 8. Transcript reveal + vocabulary

Only now show the transcript.

```markdown
## 📝 Transcript

{full script, with the recycled items in **bold**}

## 📚 Worth Keeping

| {target_language} | {native_language} | Heard as |
|-------|---------|----------|
| {word} | {meaning} | {what it sounded like} |

**Save these for review?** Type "yes" to add, "no" to skip.
```

Then optionally offer one **shadowing** pass: learner reads the transcript aloud while the clip plays — cheap, and it converts this into speaking practice.

### 9. Session summary

```markdown
## 📊 Listening Session Complete!

**Clip:** {topic} · {seconds}s · {accent}
**Questions:** {N} · **Accuracy:** {percent}%
**Replays used:** {n}

### Breakdown
- Main idea: {✅/❌}
- Details: {score}
- Numbers/names: {score}
- Inference: {score}

### Ear notes
- {what was hard: connected speech, numbers, accent, speed}

### Next time
- {one concrete suggestion}

**Nice ears!** 🎧
```

### 10. Update all databases

Use the `fluent-db-updater` skill:

- `command_used: "/fluent-listening"`, `skills_practiced: ["listening"]`
- `skill_scores.listening: {exercises: N, correct: count_right, time_minutes}`
- `review_results[]` — one entry per recycled queue item the clip tested, `quality = floor(score / 2)` (see `fluent-sm2-calculator`)
- `errors[]` — `category: "comprehension"`, `subcategory: "main_idea" | "detail" | "numbers" | "inference"`; if the learner *misheard a specific word*, that is a vocabulary/perception error — log it under the real pattern instead
- `new_vocabulary[]` — words the learner chose to keep
- `focus_next_session[]` — e.g. "connected speech: 'gonna/wanna'", "numbers over 100", "British /ɑː/ vs /æ/"

Save the full transcript + Q&A to `/results/fluent-listening-session-{NNN}.md` — it is the only record of what was said, since the chat never showed it.

## Examples

### Example 1 — stand-up update, A2, 35 s

Script (written to file, never printed):

> Hi, this is the release manager's voicemail. Quick update on the release: the login bug is fixed and the build passed this morning. We still need one more review before we can ship, so the new version goes out on **Thursday**, not Monday. If anything blocks you, message me before **6 p.m.** Thanks!

Questions: main idea (what is the message about) → detail (what is still missing before shipping) → number (when does the new version ship) → inference (why isn't it shipping on Monday?).

### Example 2 — feedback on a numbers question

> ❌ Close!
>
> **Answer:** Thursday
>
> **Explanation:** He says "Thursday, not Monday" — Monday is the plan he is *correcting*, which makes it the tempting wrong answer.
>
> **You heard:** "the new version goes out on Thursday"
>
> **Minimal pair:** *Tuesday* vs *Thursday* — the /θ/ in Thursday is voiceless; Tuesday starts with a /t/ + /j/ glide.
>
> **Score: 6/10**

## Critical Rules

- **Never print the transcript before the reveal.** Not in the question, not in a tool call, not in a hint. The whole exercise dies the moment the text is on screen. Use `--text-file`, never `--text`.
- **Two plays maximum**, and tell the learner that up front. Unlimited replays turn listening into reading.
- **One question at a time.** Wait for the answer.
- **Test what was said, not what the learner knows.** World knowledge must never be enough to answer.
- **Recycle real data.** At least three items from the review queue or mistakes DB must appear in every clip — otherwise this is just content consumption.
- **Numbers and names are mandatory.** One of each per clip; they are the highest-yield listening targets.
- **Slow down before giving up.** A learner failing 3+ questions needs `--speed 0.8` and a shorter clip, not a pep talk.
- **Never auto-invoke.** Gated; must fire only on explicit `/fluent-listening`.

## TTS engine notes

`tts.py` resolves an engine in this order and reports which one it used:

| Order | Engine | Notes |
|-------|--------|-------|
| 1 | **sherpa-onnx** (standalone exe, Apache-2.0) + a Piper/VITS voice | Offline, no Python packages, `--speed` control. Installed by `--install` into `$FLUENT_TTS_HOME` or `<data_dir>/tts/`. |
| 2 | `edge-tts` (MIT client, Microsoft neural voices) | Online, best quality, `--rate` control. Used if the package is importable. |
| 3 | OS voice — Windows SAPI / macOS `say` / Linux `espeak-ng` | Zero setup, robotic. Fine for A1-A2 on machines that actually have voices installed. |

Environment overrides: `FLUENT_TTS_HOME` (engine + models), `FLUENT_TTS_VOICE` (default voice), `FLUENT_TTS_ENGINE` (force one engine). When a harness sandbox denies writes outside the workspace, the engine and clips fall back to `<repo>/.tmp/tts` and `<repo>/.tmp/listening` — `--check` reports the real locations, so trust its output instead of assuming `<data_dir>`.

If every engine fails, say so plainly and stop the session — do **not** fall back to printing the text, which would fake a listening exercise.
