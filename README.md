# jianying-to-resolve

A read-only parser for Jianying / CapCut desktop drafts. It reads a draft, works out what is actually in it, and writes a conversion package: a segment and track inventory, a media relink map, a missing-media report, the text as CSV and SRT, honest fidelity and validation reports, and a DaVinci Resolve import script for later.

## What it does / why

Jianying and CapCut store an edit as JSON with absolute media paths on the machine that made it. Moving that edit to another editor, or even to another machine, means answering three questions: what is on the timeline, where did the media go, and what will not survive the trip. This answers all three offline, without opening either application.

It never modifies the draft. It reads `Timelines/<id>/template.json`, reads `Timelines/project.json` for timeline names, treats `timeline_layout.json` only as a hint about which timeline was active, and writes everything it produces into a new timestamped folder under `--out`.

Each segment is classified into one of four honesty buckets:

| Classification | Meaning |
| --- | --- |
| `supported` | Video or audio whose media was relinked, with no effects to lose. Timing and media carry over. |
| `supported_basic_media` | Same, but the segment also has effects, a speed change, a transition, keyframes or a mask. Timing and media carry over; the rest is exported as metadata, not rebuilt. |
| `metadata_only` | Text, or a feature-only segment. Exported to CSV, SRT and Resolve markers; not visually rebuilt. |
| `unsupported` | The media could not be found. |

The point of the split is that the fidelity report tells you what you are losing *before* you rebuild, instead of after.

## Requirements

- Python 3.9 or newer. Standard library only.
- A Jianying or CapCut desktop draft folder.
- DaVinci Resolve **Studio** — but only later, and only if you choose to run the generated import script. Nothing in the conversion step needs Resolve installed.

## Install

No install step:

```
git clone <repo-url>
cd jianying-to-resolve
python jianying_to_resolve.py --help
```

## Usage

```powershell
python .\jianying_to_resolve.py `
  --draft-root     "F:\JianyingPro Drafts\My Draft" `
  --material-root  "D:\where\the\footage\lives" `
  --out            "D:\conversion output" `
  --job-name       "my_draft"
```

| Flag | Required | Meaning |
| --- | --- | --- |
| `--draft-root` | yes | A single draft folder, or a larger root to search. The tool looks for `Timelines/<id>/template.json` anywhere beneath it. |
| `--material-root` | yes | Where to search for the media, by exact filename. |
| `--out` | yes | Parent directory. A timestamped package folder is created inside it. |
| `--timeline-id` | no | Pick a specific `Timelines/<id>` folder. Without it, an active-timeline hint wins, otherwise the first one found is used and the report says so. |
| `--job-name` | no | Names the package and its files. Sanitised for use in filenames. |

The command prints the full conversion summary as JSON on stdout and exits `0`. It exits `1` with `ERROR: ...` on stderr if the conversion could not be completed. A draft root containing no readable timeline is not an error — it produces a package whose reports say `no_valid_draft` and explain why.

## Output

One timestamped folder per run, containing:

| File | Contents |
| --- | --- |
| `jianying_<job>_segments.csv` | Every segment: track, clip index, material, in/out, timeline position, classification and why. |
| `jianying_<job>_track_inventory.csv` | One row per track: type, segment count, duration. |
| `jianying_<job>_relink_map.csv` | Each material's declared path, the path found under `--material-root`, and how it was matched. |
| `jianying_<job>_missing.csv` | Only the materials that could not be found. |
| `jianying_<job>_text_labels.csv` / `.srt` | All text segments, as a spreadsheet and as subtitles. |
| `source_template_<job>.json` | A verbatim copy of the source template, so the package is auditable on its own. |
| `conversion_summary_<job>.json` | Machine-readable summary: timeline info, all counts, the file manifest. |
| `fidelity_report_<job>.md` | What carries over, what becomes metadata, what is lost. |
| `validation_report_<job>.md` | What the tool checked and what it could not decide. |
| `resolve_build_<job>.py` | A generated Resolve importer. |
| `README_<job>.md` | How to use that particular package. |

**The generated `resolve_build_*.py` does not run itself.** Run it only after opening DaVinci Resolve Studio and enabling External scripting. It finds the Blackmagic scripting modules at the standard location for your platform; set `RESOLVE_SCRIPT_API` to point somewhere else.

## Limitations

- **This is a prototype, and it is honest about being one.** It rebuilds timing and media. It does not rebuild the look.
- **Media matching is by exact filename** under `--material-root`. Renamed files are not found; two different files with the same name resolve to whichever is found first, and the relink map records which.
- **Effects, masks, tracking, speed curves, filters, stickers, transitions and text styling are exported as metadata only.** They appear in the CSVs and as Resolve markers; they are not recreated.
- **Text becomes plain text.** Font, size, colour, position, animation and templated captions are not carried across. The SRT is timing plus content, nothing else.
- **The draft schema is undocumented and moves.** Jianying and CapCut change their JSON between versions; the parser is defensive about key names but a new version can still produce partial results. The validation report is where that shows up — read it.
- **The Resolve import script targets 1920×1080** and takes the frame rate from the draft. Change it in the generated script if you need something else.
- **No tests.**

## Note on the fixed unpack bug

`conversion_support_for()` returned a bare string instead of a 2-tuple on one branch — the branch taken by a relinked media segment with no effects, which is the commonest case in a real draft. Any draft whose media resolved cleanly crashed with `too many values to unpack (expected 2)`. It is fixed here. The path had apparently never been exercised, because the one validation run kept in the source project ended at `no_valid_draft` and never reached it.

## License

MIT. See [LICENSE](LICENSE).
