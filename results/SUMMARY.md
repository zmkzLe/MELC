## Runs

| Run | Folder | Search | Proposer models | C++ writer | Hours* | Candidates | Refused | Distinct designs scored | Best on training |
|---|---|---|---|---|---|---|---|---|---|
| Ada-3.x | `arm_adaevolve_noadapt_g3` | AdaEvolve | 3.1 Pro / 3.8 Flash | gemini-3.1-pro-preview | 17.1 | 246 | 197 | 39 | +0.47% `429a3b77e242` |

*From the first candidate's start to the last one's.

## Refusals by reason

| Reason | Ada-3.x |
|---|---|
| new words: not written as a list | 158 |
| reply missing a section | 18 |
| new word: redefines a taken name | 8 |
| design section is not YAML (e.g. a copied placeholder) | 7 |
| new word: other declaration error | 2 |
| over 48 KB of state | 2 |
| design name too long or malformed | 1 |
| other structure error | 1 |
| **total** | 197 |

## Model calls and tokens

| Run | Role | Model | Calls | Input tokens | Output tokens (incl. reasoning) |
|---|---|---|---|---|---|
| Ada-3.x | proposer | gemini-3.1-pro-preview | 194 | 4,298,322 | 2,907,801 |
| Ada-3.x | proposer | gemini-3.8-flash | 72 | 1,581,385 | 736,235 |
| Ada-3.x | proposer | (rate-limited, retried) | 37 | | |
| Ada-3.x | C++ writer | gemini-3.1-pro-preview | 71 | 1,410,909 | 792,768 |
