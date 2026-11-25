# Talk2Stock · Final deliverables

Updated 25 November 2026 from `main` (stabilized full-chain release, ADR-16).

## 2026-11-25 v3 (stabilized benchmarks & verified reports)

| File | Language | Pages | Use |
|---|---|---:|---|
| `Talk2Stock_Final_Report_EN_v3.docx` | English | editable | Submission source; six chapters + Appendices A–C (6 embedded figures) |
| `Talk2Stock_Final_Report_EN_v3.pdf` | English | 22 (15 body + 7 appendix) | Submission copy (Chrome-printed, CJK-safe, numbered pages) |
| `Talk2Stock_Final_Report_ZH_v3.docx` | 中文 | editable | 组内沟通 / 答辩口径（含 6 张实拍与架构图） |
| `Talk2Stock_Final_Report_ZH_v3.pdf` | 中文 | 19 (12 body + 7 appendix) | 预览（Chrome 渲染，中文安全，含页码） |

All UI figures come from the deployed instance (`docs/final/assets/v2/`, see `manifest.md`); Appendix A includes both `Figure A1` (390 px mobile) and `Figure A2` (conversation sidebar). Final submission should fill teammate names/student IDs in Section 6.1 and be renamed `L0X_Group10_final.pdf`.

**Measurement status (2026-11-25, post-ADR-16 stabilization).**
- **Offline regression**: **236/236** passing across **25** modules (`python3 -m unittest discover -s tests`).
- **Bill entry full chain** (`eval/run.py --full-chain --tag v8-stabilized-20261125`, OpenRouter `deepseek/deepseek-v4.1-flash-1017`, prompt `v8-20261125`): **71/71 = 100.0%** whole-bill accuracy across all 9 fields, **0 hallucinations**, **0 errors**, mean **4.92 s** (-56% vs 11.19 s pre-stabilization) / p95 **16.19 s** (`eval/results/run_20261125-143514-v8-stabilized-20261125.md`).
- **Validation agent** (`eval/run_billcheck.py`): **4/4 = 100.0%**, mean **12.5 s** (`eval/results/billcheck_20261125-142924-v2-20261125-fix.md`).
- **Grounded Q&A** (`eval/run_ask.py`, v2 contract): **33/34 = 97.1%** (`agent` 100%, `compound` **3/3 = 100%**, `boundary` 100%, `rule` 88.9%), mean **5.39 s** / p95 **9.22 s** (`eval/results/ask_20261125-143817-v2-20261125.md`).
- **Unified-agent smoke** (`eval/run_unified.py`): **5/5** (`draft` 33.71 → **10.24 s** [-70%], `clarify` 29.05 → **11.81 s** [-59%], all grounded; `eval/results/unified_agent_smoke_20261125-142617.json`).
- **Live E2E**: daily agent (7 steps, 5 points, 21.9 s), deep diagnosis (8 steps, 2 issues, 28.8 s), spreadsheet inspect/map/repair, multi-file `/api/ask/files` (2 drafts), write gate tamper block, real commit `00000000806`, nonce replay block, duplicate detection, reverse bill draft, and clean seed restoration; browser verification at 1440 px & 390 px (0 console errors, 0 horizontal overflow, verified `✓ 数字已核对` badge and one-click grounded suggestion chips).
- Note on historical 2026-11-24 pre-stabilization runs (`http_parse_20261124.json` 88.7%, `run_20261124-030929-fullchain-v7-20261124.md` 91.5%, `http_ask_20261124.json` 85.3%): kept transparently in Section 4.2/4.3 as the before-state of ADR-16.

## 2026-11-24 revision (course-template aligned)

| File | Language | Pages | Use |
|---|---|---:|---|
| `Talk2Stock_Final_Report_EN_20261124.docx` | English | 12 (≈10 body + appendices) | Course-template structure; earlier draft |
| `Talk2Stock_Final_Report_EN_20261124.pdf` | English | 12 | Review copy |
| `Talk2Stock_Final_Report_ZH_20261124.docx` | 中文 | 9 | 组内沟通 / 内容校准 |
| `Talk2Stock_Final_Report_ZH_20261124.pdf` | 中文 | 9 | 预览 |

The 2026-11-24 draft follows the six required chapters (Executive Summary; Problem and GenAI Task; System Design and Implementation; Evaluation and Improvement; Limitations and Future Work; Team Contributions and Reproducibility) plus appendices A–C. Body text is 11 pt; final submission must be renamed `L0X_GroupX_final.pdf`.

**Measurement status (2026-11-24).** 200/200 offline tests pass; unified-agent smoke 5/5 after `seed_demo --yes` (latencies 5.66/5.42/33.71/29.05/2.16 s). Deployed full runs through the public instance (OpenRouter `deepseek/deepseek-v4.1-flash-1017`): bill entry 63/71 = 88.7% whole-bill, 1 hallucinated field, mean 10.81 s / p95 14.05 s (`eval/results/http_parse_20261124.json`); Q&A 29/34 = 85.3%, zero fallbacks, mean 6.26 s / p95 16.16 s (`eval/results/http_ask_20261124.json`, deployed-data scoring: grounded + keyword + non-fallback, no gold numbers). The frozen Q&A corpus was re-scored under the v2 contract (route type not scored; zero may be expressed in natural language). A local full rerun that day hit model-gateway 429 (quota/rate limit) and is **not** used as a conclusion; the last valid local full runs remain v8b 95.0% (40 cases) and v5 94.1% (34 cases). The bill-entry runner was adapted to the multi-bill `parse` contract on 2026-11-24.

## Reports (2026-11-22 archive)

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

- 236/236 offline tests pass locally across 25 modules.
- Full-chain bill entry: 71/71 = 100.0% (0 hallucinations, 4.92 s mean); billcheck 4/4 = 100%; Q&A 33/34 = 97.1% (compound 3/3 = 100%); unified smoke 5/5 (draft 10.24 s).
- Real attachment test: CSV + text → two separate drafts; receipt image → purchase draft with attachment path.
- Browser acceptance at 1440 px and 390 px; zero console errors and zero page-level horizontal overflow.
- Acceptance writes (`00000000749` & `00000000806`) verified tamper block, nonce replay block, duplicate detection, reverse draft, then controlled seed restored.
- Pilot: 2 participants, 16 records, n=2 per cell; descriptive only, not statistically significant.
- PPTX decks (`Talk2Stock_Presentation_EN/ZH.pptx`) remain from the 2026-11-22 build; `Presentation_Outline_EN/ZH.md` have been updated with the 2026-11-25 numbers so slides 8 and 11 can be updated in PowerPoint/Keynote before the defense.

The reports and decks deliberately use the same numbers. Do not replace them with historical results without updating the evidence labels.
