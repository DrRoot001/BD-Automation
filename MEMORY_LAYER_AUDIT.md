# Browser Automation Memory Layer Audit

This document provides a technical audit of the memory layer implemented within the browser automation system of the BD-Automator-Agent.

## Overview

The browser automation system features a robust **Field Memory Layer** designed to remember how form fields were successfully filled in past runs. This allows the agent to bypass expensive and slow LLM inferences for fields it has already learned to answer, significantly speeding up the application process and improving consistency.

The core implementation is located in `backend/app/browser_automation/forms/memory.py`, and it is actively utilized by both the deterministic filler (`filler.py`) and the LLM fallback (`llm_filler.py`), as well as the agent loop (`agent/loop.py`).

## Architecture & Storage

The memory system uses a local JSON file-based storage mechanism, located in the `data/field_memory/` directory.

### Storage Layout
- **Per-Candidate Memory (`<candidate_id>.json`)**: Stores identity-bearing answers and candidate-specific inferences. This ensures that Candidate A's personal details never leak into Candidate B's application run.
- **Global Memory (`__global__.json`)**: Stores candidate-agnostic learnings (e.g., standard "Decline to answer" choices for generic fields, standard Yes/No defaults).
- **Failures Log (`field_failures.json`)**: Tracks fields that the system failed to resolve, aiding in manual review and future rule creation.
- **Anonymous/Legacy Fallback (`__anonymous__.json`)**: Used when a candidate ID is not provided or during legacy migration.

## Core Mechanisms

### 1. Label Normalization and Aliasing
Before a field's label is stored or looked up, it undergoes normalization (`_normalize_label`):
- Lowercasing, whitespace stripping, and removal of special characters (like asterisks).
- **Aliasing Rules**: Common variations of questions (e.g., "current job title", "what is your current title") are mapped to a canonical key (e.g., "current job title"). 
- *Security Note*: Work authorization and sponsorship questions are intentionally **not** aliased because their answers are highly country-specific (e.g., authorized in US vs. UK).

### 2. Strict Identity Isolation (Bug Fix A)
The system employs `_IDENTITY_PATTERNS`, a set of regular expressions that flag fields containing PII or candidate-specific demographics (Name, Email, Phone, LinkedIn, Address, Gender, Race, Work Authorization, etc.).
- **Recall Behavior**: If a field matches an identity pattern, the system will **only** look it up in the candidate's specific memory file. It will never fall back to the global memory.
- **Remember Behavior**: When saving a new successful fill, identity fields are saved **only** to the candidate's file. Non-identity fields are saved to both the candidate's file and the global file.

### 3. Strict Memory Isolation Mode
For testing or highly sensitive batch runs, an environment variable `STRICT_MEMORY_ISOLATION=true` can be set. This disables the global memory fallback entirely, forcing every answer to come from the candidate's own file or be re-inferred, guaranteeing zero cross-candidate bleed.

## Integration Points

### 1. Pre-fill Stage (`llm_filler.py` & `filler.py`)
Both the deterministic and LLM-driven fillers consult the memory layer (`field_memory.recall()`) as **Step 0**.
- If an answer is found in memory, the system bypasses the LLM and rule-engine checks for that field, returning the memorized value immediately.
- File inputs (e.g., resume uploads) bypass memory since file paths vary per run.

### 2. Agent Loop (`loop.py`)
The autonomous loop includes a specific `_try_memory_prefill` routine. Before asking the LLM to decide on actions, it scans the visible DOM for fields it already has memorized answers for, and emits a batch of `fill_field` actions.

### 3. Learning (Persistence)
After a field is successfully filled and verified in the DOM, `field_memory.remember()` is called. It tracks the normalized label, the field type, the value, a hit count, and the timestamp.

## Audit Findings & Assessment

### Strengths
1. **Strong Privacy Controls**: The regex-based `_IDENTITY_PATTERNS` and the split between candidate/global files is a well-designed mechanism to prevent PII leakage across different candidates' applications.
2. **Performance Optimization**: By caching successful LLM inferences, the system reduces API costs and speeds up repetitive ATS forms.
3. **Resilience to Wording Changes**: The alias engine allows the system to apply learnings across slightly different ATS form configurations.
4. **Country-Specific Nuance**: The explicit decision *not* to alias work authorization questions demonstrates a mature understanding of job application edge cases.

### Potential Vulnerabilities / Areas for Improvement
1. **Regex Brittleness**: The `_IDENTITY_PATTERNS` rely on regex matching. If an ATS introduces a highly unusual phrasing for an identity question that bypasses these regexes, it might be classified as a generic field and leaked into `__global__.json`.
   - *Recommendation*: Consider adding a fallback LLM check or a wider semantic net for identifying PII if the regexes begin failing on new ATS platforms.
2. **Concurrency Limitations**: The memory layer uses a simple `threading.Lock()` (`_lock`). While adequate for a single Python process, if the backend scales horizontally (e.g., multiple Celery workers across different containers), file-based locking will fail, leading to race conditions or lost memory updates.
   - *Recommendation*: If distributed execution is planned, migrate the memory layer from JSON files to a centralized store like Redis or Postgres.
3. **Unbounded Growth**: The JSON files (especially `__global__.json`) will grow indefinitely as the system encounters new unique field variations.
   - *Recommendation*: Implement a pruning mechanism (e.g., remove entries with a low hit count that haven't been seen in X months) to keep the JSON files performant during loading.
4. **Memory Poisoning Recovery**: If a bad inference gets committed to memory, the agent will reuse it until it's manually purged.
   - *Recommendation*: Build a UI or script to easily inspect and prune `field_memory` files for a specific candidate or the global pool.

## Conclusion
The memory layer is highly functional, cost-saving, and well-architected for privacy isolation. The primary future concern is transitioning from local file storage to a distributed database if the architecture scales horizontally.
