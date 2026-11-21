# Talk2Stock · Final deliverables

Generated 22 November 2026 from the `feature/unified-agent` working tree.

## Reports

| File | Language | Pages | Use |
|---|---|---:|---|
| `Talk2Stock_Final_Report_EN.docx` | English | 24 | Editable final report |
| `Talk2Stock_Final_Report_EN.pdf` | English | 24 | Submission / review copy |
| `Talk2Stock_Final_Report_ZH.docx` | 中文 | 19 | 可编辑最终报告 |
| `Talk2Stock_Final_Report_ZH.pdf` | 中文 | 19 | 提交 / 评审副本 |

The reports use the same chapters, evidence and figures. The Chinese PDF was rendered with an explicit CJK font configuration; the source DOCX uses system CJK fonts and remains editable.

## Presentations

| File | Language | Slides | Use |
|---|---|---:|---|
| `Talk2Stock_Presentation_EN.pptx` | English | 12 | 10-minute live presentation |
| `Talk2Stock_Presentation_EN.pdf` | English | 12 | Preview / fallback |
| `Talk2Stock_Presentation_ZH.pptx` | 中文 | 12 | 10 分钟现场演示 |
| `Talk2Stock_Presentation_ZH.pdf` | 中文 | 12 | 预览 / 回退 |

Each presentation includes speaker notes. The suggested 10-minute allocation is:

- 1 min: target users, needs, pain points and scenario
- 1 min: product positioning and business value
- 4 min: prototype demo
- 4 min: technical solution
- Title and close are inside the first and last minute.

## Per-slide outlines

- `Presentation_Outline_EN.md`
- `Presentation_Outline_ZH.md`

Each outline gives the page purpose, the content on the slide, the live action, the speaker script, demo fallback and Q&A anchors.

## Shared visual assets

`assets/` contains the source images used by both reports and decks:

- `architecture.png` / `architecture_diagram.png` — four-plane architecture.
- `agent-loop.png` / `agent_loop_diagram.png` — controlled Agent loop.
- `attachments.png` / `attachments_diagram.png` — multi-file ownership flow.
- `benchmark.png` / `benchmark_chart.png` — pilot timing chart.
- `ui-home.png`, `ui-mobile.png`, `ui-drafts.png`, `ui-confirm-60.png`, `ui-import.png`, `ui_duo.png` — actual prototype screenshots.
- `cover_en.png`, `cover_zh.png` — report covers.

## Evidence boundaries used in the documents

- 183/183 offline tests pass locally.
- Unified-agent smoke: 5/5; latest result in `eval/results/`.
- Real attachment test: CSV + text → two separate drafts; receipt image → purchase draft with attachment path.
- Browser acceptance at 1440 px and 390 px; no page-level horizontal overflow.
- Acceptance write `00000000749`; stock 0→1, then controlled seed restored.
- Pilot: 2 participants, 16 records, n=2 per cell; descriptive only, not statistically significant.
- The unified smoke is five core behaviours, not a full rerun of the historical 71-case corpus.

The reports and decks deliberately use the same numbers. Do not replace them with historical results without updating the evidence labels.
