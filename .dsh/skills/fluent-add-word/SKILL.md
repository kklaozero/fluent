---
name: fluent-add-word
description: Add custom vocabulary words the learner wants to learn. Collects words one at a time (target-language word, meaning, optional category/difficulty/example), checks for duplicates, persists them to the spaced-repetition system, and confirms the review schedule. Triggered only when the learner types /fluent-add-word.
disable-model-invocation: true
---
## Running in DeepSeek Harness (DSH)

Migrated from Claude Code (`.claude/skills/`). DSH runs PowerShell on Windows:

- Use `python` (not `python3`) — Python 3.14.6 is installed; `python3` is not on PATH.
- `${CLAUDE_PLUGIN_ROOT:-${CLAUDE_PROJECT_DIR:-.}}` was a Claude Code idiom for the repo root. Run commands from the fluent repo root (your DSH session workspace) so `.claude/hooks/...` resolves, or substitute `$(git rev-parse --show-toplevel)`.
- Bash heredocs (`<<'EOF'`) are not PowerShell: write the JSON payload to a temp file, then `Get-Content payload.json | python .claude/hooks/update-db.py`.

# Add Custom Vocabulary Words

## Overview

A lightweight, standalone flow for the learner to add words they encounter in daily life — from a book, a conversation, a song, or anywhere else. Each word is persisted to the spaced-repetition system and will appear in tomorrow's review queue. No full learning session required.

## When to Use

Trigger this skill only when the learner types `/fluent-add-word`. The skill is gated with `disable-model-invocation: true` — a false-positive auto-trigger would write to the databases without the learner's intent.

Skip this skill if no learner profile exists — direct the learner to `/fluent-setup` first.

## Instructions

### 1. Load learner context

```bash
python ".claude/hooks/read-db.py"
```

If any databases are missing, direct the learner to `/fluent-setup` and stop.

From the output, extract:
- `databases.learner_profile.learner.name`
- `databases.learner_profile.learner.target_language`
- `databases.learner_profile.learner.native_language` (if set; otherwise infer from context or ask)
- `databases.spaced_repetition.items` — all existing items, used for duplicate detection
- `computed.next_session_id`

### 2. Opening

```markdown
# 📝 Add Custom Words

{greeting in target language}, {name}!

Found some new words you want to learn? Tell me what they are and I will add them to your spaced-repetition review queue. They will appear in tomorrow's review session.

**Already tracking:** {total_items_count} items in your review system.
```

### 3. Collect words — one at a time

Present a clear prompt for the first word:

```markdown
## Word 1

**What is the word or phrase in {target_language}?**

Type your answer (or type **done** to finish):
```

After the learner provides the word, collect the remaining details. Ask for the meaning first (required), then offer optional fields:

```markdown
**What does "{word}" mean in {native_language}?**

Type your answer:
```

Once the meaning is provided, ask for optional details in a single follow-up. Present all three optional fields together so the learner can fill none, some, or all:

```markdown
**Optional details for "{word}" ({meaning}):**

- **Category** (e.g., food, travel, business, emotion, technology) — helps organize your vocabulary
- **Difficulty** (A1 / A2 / B1 / B2 / C1 / C2) — if you are unsure, I can estimate based on your level
- **Example sentence** in {target_language} — seeing the word in context improves retention

Type any or all of these, separated by newlines. Type **skip** to leave them all blank.
```

Parse the learner's response loosely — accept free-form input like "category: food, difficulty: B1, example: ..." or just "food / B1" or a bare category name. For any field the learner does not provide, use sensible defaults:
- `category`: `"custom"` (or infer from the word if obvious — e.g. "apple" → "food")
- `difficulty`: match the learner's `current_level` from `learner-profile.json`
- `example`: leave blank (the spaced-repetition system handles items without examples)

#### Duplicate detection

Before accepting a word, check whether it already exists in `spaced_repetition.items`:

- Match by `content` (case-insensitive) or by `item_id` (derived as `vocab_<lowercase_word_with_underscores>`).
- If a match is found:

```markdown
⚠️ **"{word}" is already in your review system.**

- Added on: {created_date}
- Current mastery: {mastery_level}/5 stars
- Next review: {due_date}

It is already scheduled for review. No need to add it again.

Want to add a different word instead? Type your next word (or **done** to finish).
```

Do NOT block the learner from re-adding — they might want to update the translation or add a different meaning. If they insist, proceed but note it will create a separate entry.

#### Continue or finish

After each word is staged, ask:

```markdown
✅ **"{word}" ({meaning})** staged for review — it will appear tomorrow.

**Add another word?** Type your next word in {target_language} (or type **done** to finish).
```

### 4. Confirm and persist

When the learner types **done** (or after collecting 20 words — a reasonable single-session cap), show a summary and ask for final confirmation:

```markdown
## 📋 Review Before Saving

**Words to add ({count}):**

| # | Word | Meaning | Category | Difficulty |
|---|------|---------|----------|------------|
| 1 | {word} | {meaning} | {category} | {difficulty} |
| 2 | ... | ... | ... | ... |

These will appear in your review queue starting **tomorrow** and will follow the standard spaced-repetition schedule.

**Save these words?** Type **yes** to confirm, **no** to discard, or **edit** to modify a word.
```

If the learner types **no**, discard and exit:

```markdown
No words saved. Come back anytime with `/fluent-add-word` when you find new words to learn!
```

If the learner types **edit**, ask which word number and what to change, then re-show the summary.

### 5. Write to databases

On confirmation, call `update-db.py`:

```bash
python ".claude/hooks/update-db.py" <<'EOF'
{
  "session_id": "{next_session_id}",
  "date": "{today}",
  "duration_minutes": {elapsed},
  "command_used": "/fluent-add-word",
  "skills_practiced": ["vocabulary"],
  "new_vocabulary": [
    {
      "item_id": "vocab_{lowercase_underscored_word}",
      "item_type": "vocabulary",
      "content": "{word}",
      "answer": "{meaning}",
      "category": "{category}",
      "difficulty": "{difficulty}",
      "initial_quality": 4,
      "priority": "medium"
    }
  ],
  "session_notes": "Added {count} custom word(s): {word_list}"
}
EOF
```

For `item_id`, follow the existing convention: `vocab_` + the word in lowercase with spaces and special characters replaced by underscores. Examples:
- "serendipity" → `vocab_serendipity`
- "de keuken" → `vocab_de_keuken`
- "être" → `vocab_etre` (strip diacritics for the ID)
- "hacer la maleta" → `vocab_hacer_la_maleta`

If `item_id` collides with an existing item (unlikely if the duplicate check passed), append a numeric suffix: `vocab_serendipity_2`.

Check the exit code:
- `0` → success, proceed to confirmation.
- `1` → validation error. Read the stderr message, fix the payload, retry once.
- `2` → I/O error. Report to the learner and suggest they try again.

### 6. Confirmation

```markdown
## ✅ Words Saved!

**{count} word(s) added to your review system:**

{list each word with its meaning}

**What happens next:**
- 🕐 **Tomorrow:** these words appear in `/fluent-review`
- 📅 **Day 3:** words you recall correctly move to a 3-day interval
- 📅 **Day 7:** words you keep recalling spread to weekly review
- ⭐ **Mastery:** after several correct recalls, they reach long-term memory

**Tip:** Use these words in a sentence today — active use before the first review dramatically improves retention.

Want to practice right now? Try `/fluent-vocab` or `/fluent-learn`!

{encouragement in target language}! 🌟
```

## Examples

### Example 1 — adding a single word

```
Learner: /fluent-add-word
```

> # 📝 Add Custom Words
>
> Hallo, Mohammad!
>
> Found some new words you want to learn? Tell me what they are and I will add them to your spaced-repetition review queue. They will appear in tomorrow's review session.
>
> **Already tracking:** 47 items in your review system.
>
> ---
>
> ## Word 1
>
> **What is the word or phrase in Dutch?**
>
> Type your answer (or type **done** to finish):

```
Learner: de vergadering
```

> **What does "de vergadering" mean in English?**
>
> Type your answer:

```
Learner: the meeting
```

> **Optional details for "de vergadering" (the meeting):**
>
> - **Category** (e.g., food, travel, business, emotion, technology)
> - **Difficulty** (A1 / A2 / B1 / B2 / C1 / C2)
> - **Example sentence** in Dutch
>
> Type any or all of these, separated by newlines. Type **skip** to leave them all blank.

```
Learner: category: business
difficulty: A2
example: De vergadering begint om negen uur.
```

> ✅ **"de vergadering" (the meeting)** staged for review — it will appear tomorrow.
>
> **Add another word?** Type your next word in Dutch (or type **done** to finish).

```
Learner: done
```

> ## 📋 Review Before Saving
>
> **Words to add (1):**
>
> | # | Word | Meaning | Category | Difficulty |
> |---|------|---------|----------|------------|
> | 1 | de vergadering | the meeting | business | A2 |
>
> These will appear in your review queue starting **tomorrow**.
>
> **Save these words?** Type **yes** to confirm, **no** to discard, or **edit** to modify a word.

```
Learner: yes
```

> ## ✅ Words Saved!
>
> **1 word added to your review system:**
>
> - **de vergadering** — the meeting (business, A2)
>
> **What happens next:**
> - 🕐 **Tomorrow:** this word appears in `/fluent-review`
> - 📅 **Day 3:** recall correctly → 3-day interval
> - 📅 **Day 7:** keep recalling → weekly review
> - ⭐ **Mastery:** several correct recalls → long-term memory
>
> Goed gedaan! 🌟

### Example 2 — duplicate word

```
Learner: de keuken
```

> ⚠️ **"de keuken" is already in your review system.**
>
> - Added on: 2026-07-10
> - Current mastery: 4/5 stars
> - Next review: 2026-08-02
>
> It is already scheduled for review. No need to add it again.
>
> Want to add a different word instead? Type your next word in Dutch (or type **done** to finish).

### Example 3 — empty session (no words added)

```
Learner: /fluent-add-word
```

> [... opening ...]
>
> ## Word 1
>
> **What is the word or phrase in Dutch?**
>
> Type your answer (or type **done** to finish):

```
Learner: done
```

> No words to add. Come back anytime with `/fluent-add-word` when you find new words to learn!

## Critical Rules

- **Never auto-invoke.** Gated; writes to databases.
- **One word at a time.** Collect fully before moving to the next.
- **Check for duplicates.** Search `spaced_repetition.items` before accepting.
- **Sanitize `item_id`.** Lowercase, underscores, ASCII-only — the spaced-repetition system uses this as a key.
- **Batch the write.** Call `update-db.py` once with all words, not once per word.
- **Confirm before writing.** Show the summary and wait for explicit **yes**.
- **Cap at 20 words per session.** Beyond that the learner should use `/fluent-vocab` for a full session.
- **Handle missing profile.** Direct to `/fluent-setup`; do not create a partial profile.
