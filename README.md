# Elements of Style Checker (one agent per rule)

English | [简体中文](README.zh-CN.md)

Give it an English PDF and it checks the text against William Strunk Jr.'s *The Elements of Style* (the 1918 original, in the public domain).
**Every rule has its own agent. Each agent looks only for its own rule and reads the document from the first page to the last.** The result shows which passage breaks which rule.

It writes three files:

| File | Contents |
|---|---|
| `report.html` | A filterable report: filter by rule, severity or keyword. The problem span is highlighted in its sentence, with the reason and a suggested fix. Switch between Chinese and English, and between light and dark mode, in the top-right corner |
| `<file name>.annotated.pdf` | A copy of the original PDF with pale highlight annotations on each problem (colored by chapter of the book). Each note gives the rule number, the reason and the suggestion, in the `--lang` language |
| `report.json` | Every finding plus per-agent run statistics, for use by other programs |

## Quick start

```bash
pip install -r requirements.txt
```

You need an OpenAI-compatible model endpoint (vLLM, Ollama, DeepSeek, ...). It was developed and tested with Qwen3-VL-8B-Instruct served by vLLM.

1. If the model runs on a remote server, open an SSH tunnel first (in its own terminal, left open):

```bash
ssh -N -L 8000:127.0.0.1:8000 <user>@<server>
```

2. Copy `.env.example` to `.env` and fill in the endpoint URL, API key and model name.
3. Test the connection:

```bash
python -m eos_checker ping
```

4. Check a PDF:

```bash
python -m eos_checker check your-file.pdf --lang en
```

Results go to a `<file name>_style_check/` folder next to the PDF.
Explanations are written in Chinese by default; `--lang en` makes the agents and the annotated PDF use English. The HTML report can switch between both languages either way.

## Commands and options

| Command / option | What it does |
|---|---|
| `python -m eos_checker rules` | List the rules (add `--all` to include outdated and not-checkable ones) |
| `python -m eos_checker extract x.pdf` | Show how the PDF was split into paragraphs (for debugging text extraction) |
| `python -m eos_checker check x.pdf --dry-run` | No model calls; estimate how many calls each agent would make |
| `python -m eos_checker check x.pdf --no-llm` | Run only the pure-regex rules (a few seconds, no model needed) |
| `--pages 1-20,25` | Check only these pages |
| `--rules II-05,III-10,V-etc` / `--sections II,III` | Run only these rules / chapters |
| `--include-outdated` | Also run rules that modern usage no longer accepts (see below) |
| `--lang en` | The agents write reasons in English and the annotated PDF uses English (default: Chinese). After the run, every reason is translated into the other language (a few model calls), so the HTML report can show either |
| `--chunk-chars 2000` | Characters per chunk. Smaller chunks give small models better recall but need more calls |
| `--concurrency 16` / `--window 8` | Requests in flight overall / chunks one agent reads in parallel (`--window 1` = strictly in order) |
| `--no-double-check` | Skip the second review (more findings, more false alarms) |
| `--no-cache` | Don't reuse cached answers (by default model answers are cached in `.cache/`, so re-running the same file costs almost nothing) |

## How it works

```
PDF ──► document.py   extract the text with the position of every character; drop headers, footers
         │            and page numbers; rejoin paragraphs split across lines, columns and pages;
         │            rejoin words divided at line ends and record the divisions; detect headings;
         │            split sentences
         ▼
       chunker.py    split into chunks by paragraph / sentence (2000 characters by default); each
         │            chunk carries the text before it as read-only context
         ▼
       agents.py     80 RuleAgents run concurrently, each responsible for one rule, each reading the
         │            whole document:
         │             1. regex hits reported directly (misspellings, "different than", "as to whether" ...)
         │             2. regex finds candidate sentences -> the model confirms each one
         │                (passive voice, less/fewer ...)
         │             3. the model reads the whole document looking for problems -> the same agent
         │                reviews each one again, and only confirmed findings are kept
         ▼
       translate.py  translate reasons and violation types into the other language (quoted English
         │            stays as written; anything that can't be translated cleanly keeps its original text)
         ▼
       report.py     report.json / report.html (Chinese/English) / annotated PDF
```

All rules live in [rules/elements_of_style_1918.yaml](rules/elements_of_style_1918.yaml); no rule is hard-coded. To change or add a rule, edit that file.

## How the book was turned into rules

The book (Project Gutenberg #37134) has six chapters, which became 91 rules:

| Chapter | Rules | Contents |
|---|---|---|
| II Elementary Rules of Usage | 7 | Possessive 's, serial comma, commas around parenthetic expressions, comma before a conjunction, comma splices, sentence fragments, dangling participles |
| III Elementary Principles of Composition | 11 | The paragraph, topic sentences, active voice, positive form, definite and concrete language, omitting needless words, loose sentences, parallel form, keeping related words together, tense in summaries, emphatic words at the end |
| IV A Few Matters of Form | 6 | Numerals, punctuation with parentheses, quotations, references, line-end word division (one rule, on heading format, can't be judged from PDF text and is not run) |
| V Words and Expressions Commonly Misused | 63 | One agent per entry: all right, different than, due to, etc., less/fewer, like/as, while, whom ... |
| VI Spelling | 4 | The book's list of commonly misspelled words (plus common misspellings), consonant doubling, to-day and others |

Each rule's YAML entry has the meaning of the rule (`description`), numbered violation types (`violations`), cases that explicitly do not count (`not_violations`, including every exception the book gives), the book's right and wrong examples (`examples`), and regex triggers (`triggers`).

**10 rules are marked `outdated`** (not run by default; `--include-outdated` runs them). The book was written in 1918, and modern English no longer accepts some of its demands: "he" rather than "they" after each/everyone, "shall" for the first-person future, no split infinitives, "to-day". These rules keep the book's wording faithfully, but running them by default would produce a flood of correct-but-dated warnings.

## Precision measures for a small 8B model

Letting Qwen3-VL-8B simply read the whole document and report problems gave many false alarms on the test sample (about half of 78 findings were wrong). What the checker does instead:

1. **Use regexes wherever they are enough.** 14 rules (misspellings, fixed phrases and the like) are pure regex, with no false alarms.
2. **Regexes find candidates, the model only judges.** For 41 rules a regex first finds suspicious sentences, and the model only answers "does this sentence really break the rule?", which is far more accurate than asking it to search.
3. **The model must name a violation type.** Each answer has to say which numbered violation of the rule it is; answers that don't match are dropped.
4. **Second review.** Every problem a read-through agent proposes is reviewed again, on its own, by the same agent; only confirmed ones are kept.
5. **Deterministic guards.** For example, "active voice, type 1 (passive with *by*)" requires the sentence to actually contain be + past participle + by; punctuation rules require the suggestion to actually change the punctuation; findings whose suggestion equals the original, or where the model itself says no change is needed, are dropped.
6. **Reasoning fields before the verdict.** For comma splices, the model first states whether the text on each side of the comma is a complete sentence; only when both are true is it a violation.
7. **Caps.** Paragraph-level rules report at most once per paragraph, and subjective rules (concrete language, emphatic endings, ...) at most 2–3 times per chunk. Paragraph problems that can be counted, such as runs of loose sentences, are counted by rule first and then confirmed by the model.

Test sample (`samples/sample.pdf`, 2 pages, about 45 planted problems): 50 findings, about 90% correct on manual review, about 4 misses (see below), and no false alarms in the clean paragraphs.

## Known limitations

- **Model ability.** The 8B model misses rules that need real semantic understanding. Misses on the sample: the dangling participle "Walking slowly down the road, the bus appeared" (it thinks the bus is walking), a relative clause far from its antecedent (III-16), and the line-end division des-cribe. A stronger model (change `LLM_MODEL` / `LLM_BASE_URL` in `.env`; any OpenAI-compatible endpoint works) directly improves recall on these rules.
- **Occasional false alarms.** Parallel form (III-15) and active voice (III-10, e.g. treating the instrumental *by* in "taught by the textbook method" as an agent). Both are medium/low severity, so the report can filter them out.
- **Serial comma (II-02)** has lower precision: telling "introductory phrase, A and B" from "A, B and C" needs real parsing, and the 8B model miscounts the items.
- **Scanned PDFs** have no text layer and need OCR first.
- **Multi-column layouts** are read in the PDF's internal order. That is usually right, but some layouts can come out of order (check with the `extract` command).
- Figure captions and reference lists in papers are checked like ordinary text and can produce some meaningless warnings.

## Speed

A 32-page test document (about 900 sentences): about 600 model calls in about 2 minutes (about 830K input tokens and 36K output tokens). Re-running the same file hits the cache and finishes almost instantly. At that rate, 300 pages take about 15–20 minutes.
Time depends mostly on server throughput; vLLM batches concurrent requests, so try raising `--concurrency` to 32.

## Test

```bash
python samples/make_sample_pdf.py
python -m eos_checker check samples/sample.pdf
```

## License

The code is released under the [MIT License](LICENSE). The rules are adapted from Strunk's 1918 *The Elements of Style* (Project Gutenberg #37134, public domain).
