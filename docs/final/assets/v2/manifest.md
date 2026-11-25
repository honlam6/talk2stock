# UI evidence manifest (2026-11-24, deployed instance)

All screenshots were taken from the deployed system at http://111.91.30.23:8090/ after logging in as `admin` (ERP account). Read-only operations only: no bill was submitted.

| File | Page / action | Viewport | State shown |
|---|---|---|---|
| `01-home.png` | `/ask` → “新对话” | 1440×900 | Greeting page: two avatars, eight capability cards, input box |
| `02-answer.png` | `/ask` → “可乐还有多少库存？” | 1440×900 | Grounded answer + execution panel (2 steps, 1.6 s) |
| `03-draft.png` | `/ask` → “从美宜佳买30瓶可乐，单价3块5，入原料仓” | 1440×900 | Draft card: purchase-in, 30 bottles, ¥105.00, “核对草稿” |
| `03b-confirm.png` | Draft card → “核对草稿” | 1440×900 | Batch confirmation: source labels (“已有”), editable fields, total, submit button (not clicked) |
| `04-sidebar.png` | `/ask` → “新对话” | 1440×900 | Conversation sidebar with four sessions + greeting page |
| `05-mobile.png` | `/ask` → “新对话” | 390×844 | Mobile first screen: single-line input above the bottom tab bar |
| `06-dashboard.png` | `/dashboard` | 1440×900 | AI insight card + procurement suggestion card (663 ms, read-only) |
| `07-diagnosis.png` | `/dashboard` → “深度诊断” | 1440×900 | Diagnosis results after seven evidence steps with timings |
| `08-import.png` | `/new` → “表格导入” | 1440×900 | Spreadsheet import: file → mapping → preview/write |

Skipped:
- `09-login.png` — logging out would invalidate the active demo session; the login dialog was captured indirectly by the auth flow, not needed for the report.
- `10-retry.png` — producing a failed answer requires forcing a model error or quota failure; skipped to avoid polluting the demo session and data.
