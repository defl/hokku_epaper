---
description: Full release workflow — tests, CI check, doc/changelog updates, version bump, tag, push, and GitHub release with approval gates.
---

# /release — hokku_epaper release workflow

Run the full release workflow for hokku_epaper, step by step. Stop and report clearly on any failure. Require explicit approval before any push, tag, or GitHub release action (per AGENTS.md).

---

## Step 1 — Pre-flight: workspace must be clean

Run:
```
git status --porcelain
```

If the output is non-empty, **abort immediately**. List the dirty files and tell the user to commit or stash all changes before starting a release.

Also check that the current commit has been pushed to GitHub and has a passing CI run:

```
git rev-parse HEAD
gh run list --commit <SHA> --workflow CI --json databaseId,status,conclusion --limit 10
```

Find a run where `status == "completed"` and `conclusion == "success"`. If no such run exists (commit not pushed, CI still running, or CI failed), **abort immediately** — tell the user to push the commit and wait for CI to pass, then re-run `/release`.

---

## Step 2 — Run webserver tests

Run from the repo root:
```
.venv\Scripts\python -m pytest -m "not time_intensive"
```

The `pytest.ini` at repo root sets `testpaths = webserver/tests`, so no extra path argument is needed.

If any test fails, **abort**. Show the failing test names and output.

---

## Step 3 — Run firmware host tests

Run via Docker (same pattern as the .deb build, to avoid Windows NTFS/compiler issues):

```bash
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "/c/Users/defl/workspace/hokku_epaper":/src \
  ubuntu bash -c '
    apt-get update -qq
    apt-get install -y -qq cmake build-essential >/dev/null 2>&1
    cmake -B /tmp/fw-test-build /src/firmware/test/host -DCMAKE_BUILD_TYPE=Release
    cmake --build /tmp/fw-test-build
    ctest --test-dir /tmp/fw-test-build --output-on-failure
  '
```

If any test fails, **abort**. Show the CTest output.

---

## Step 4 — Update README.md and docs/

Read `README.md` and every file in `docs/`. Compare their content against the current state of the codebase (code, tests, config). Identify anything that is stale, inaccurate, or missing.

Key areas to audit:
- Feature lists and capability descriptions in README.md
- Installation steps in `docs/install.md` and `docs/manual.md`
- Any version-specific version numbers embedded in docs
- Pipeline or algorithm descriptions in `docs/dithering.md` — compare against `webserver/hokku_server/` code
- Metric definitions in `docs/image_quality.md` — compare against `webserver/hokku_server/image_quality.py`

For each proposed change, show the user the before/after diff and wait for confirmation before applying it. If no changes are needed, say so and continue.

---

## Step 5 — Show existing tags; ask user for the new version

Run:
```
git tag --sort=-version:refname | head -15
```

Display the list, then ask the user:

> What version tag should this release use? (e.g. `v3.0.0` or `v3.0.0-beta6`)

From the user's answer, derive all format variants and display them for confirmation:

| Format | Example |
|--------|---------|
| Git tag (as entered) | `v3.0.0-beta6` |
| PEP 440 — `webserver/pyproject.toml` | `3.0.0b6` |
| Debian — `webserver/debian/changelog` | `3.0.0~beta6-1` |
| CHANGELOG.md heading | `3.0 beta6` |

Conversion rules:
- `v3.0.0` → PEP 440 `3.0.0`, Debian `3.0.0-1`, heading `3.0.0`
- `v3.0.0-beta6` → PEP 440 `3.0.0b6`, Debian `3.0.0~beta6-1`, heading `3.0 beta6`
- `v3.0.0-alpha2` → PEP 440 `3.0.0a2`, Debian `3.0.0~alpha2-1`, heading `3.0 alpha2`
- `v3.0.0-rc1` → PEP 440 `3.0.0rc1`, Debian `3.0.0~rc1-1`, heading `3.0 rc1`

Wait for the user to confirm the derived versions before continuing.

---

## Step 6 — Update CHANGELOG.md

Get the git log since the last tag:
```
git tag --sort=-version:refname | head -1   # last tag
git log <last-tag>..HEAD --oneline
```

Read the existing `CHANGELOG.md`. Prepend a new section for the new version using the format already established in the file (e.g. `## 3.0 beta6`). Write this section in user-friendly "what, not how" language: user-visible features and fixes, not implementation or refactoring details. Group related items under descriptive sub-headings if there are more than a few changes.

Present the draft CHANGELOG section to the user for review and editing before saving. Apply their edits.

**Note:** This section covers only changes since the previous tag. The GitHub release text (Step 12) may span a wider range.

---

## Step 7 — Update version files

Edit **`webserver/pyproject.toml`**: change the `version = "..."` line to the PEP 440 form.

Edit **`webserver/debian/changelog`**: prepend a new entry at the top in Debian RFC 822 format:

```
hokku-server (<debian-version>) unstable; urgency=medium

  * Release <git-tag>

 -- Dennis Fleurbaaij <mail@dennisfleurbaaij.com>  <current date in RFC 2822 format>
```

The build number starts at `-1` for any new upstream version.

RFC 2822 date format example: `Sun, 17 May 2026 00:00:00 +0000` — use the actual current date and time UTC.

---

## Step 8 — Commit the release changes

Stage exactly these files (and only these):
- `webserver/pyproject.toml`
- `webserver/debian/changelog`
- `CHANGELOG.md`
- Any `README.md` or `docs/*.md` files that were modified in Step 4

Run:
```
git commit -m "chore: release <git-tag>"
```

No approval is required for a commit (per AGENTS.md). After the commit, capture the new commit SHA:
```
git rev-parse HEAD
```

---

## Step 9 — Tag and push (REQUIRES explicit user approval)

State the exact three commands that will run:

```
git tag <git-tag>
git push
git push origin <git-tag>
```

**Do not execute them yet.** Wait for the user to give explicit approval — phrases like "go ahead", "yes", "push it", "do it". Per AGENTS.md, "looks good", "I see", "that's right", and similar are NOT authorisation.

Once approved, run those three commands in sequence.

---

## Step 10 — Wait for GitHub Actions CI to complete

After push, get the new commit SHA and poll until the CI run completes. Check every ~60 seconds:

```
gh run list --commit <new-SHA> --workflow CI --json databaseId,status,conclusion,name --limit 10
```

Report job-level progress at each poll (e.g. "test-firmware: success, build-firmware: in_progress, test-webserver: success, build-webserver-deb: queued").

Wait until **all four jobs** (test-firmware, test-webserver, build-firmware, build-webserver-deb) show `conclusion == "success"`.

If any job fails → **stop**. Report which job failed and include the run URL:
```
https://github.com/defl/hokku_epaper/actions/runs/<run-id>
```

Note the successful run's `databaseId` for the next step.

To get per-job status, use:
```
gh run view <run-id> --json jobs --jq '.jobs[] | {name, status, conclusion}'
```

---

## Step 11 — Download CI artifacts

Download both build artifacts from the successful CI run:

```powershell
New-Item -ItemType Directory -Force "$env:TEMP\hokku-release"
gh run download <run-id> --name hokku-firmware --dir "$env:TEMP\hokku-release"
gh run download <run-id> --name hokku-server-deb --dir "$env:TEMP\hokku-release"
```

List the downloaded files and their sizes. Verify that exactly one `.bin` file (firmware) and one `.deb` file (webserver) are present. If anything is missing, report and stop.

---

## Step 12 — Create GitHub release (REQUIRES explicit user approval)

**Determine the release notes scope:**

Find the last published GitHub release:
```
gh release list --limit 5
```

Then get all commits since that release's tag up to the current HEAD:
```
git log <last-gh-release-tag>..<new-git-tag> --oneline
```

Read the CHANGELOG.md sections that span this same range (may cover multiple intermediate tags if they were never released on GitHub). Synthesise human-readable release notes that:
- Tell the story of **what changed for the user**, not how it was implemented
- Are organised by feature area or theme, not by commit
- Include notable fixes and improvements
- Omit internal refactors, test changes, and build-system tweaks unless user-visible

Present the draft release notes to the user for review and editing. Apply any edits.

**Then state the exact commands that will run:**

Write the release notes to a temp file to avoid quoting issues:
```powershell
Set-Content -Path "$env:TEMP\hokku-release-notes.md" -Value @'
<release notes>
'@
```

Then create the release:
```powershell
gh release create <git-tag> `
  "$env:TEMP\hokku-release\<firmware-filename>.bin" `
  "$env:TEMP\hokku-release\<webserver-filename>.deb" `
  --title "Hokku e-paper server <git-tag>" `
  --notes-file "$env:TEMP\hokku-release-notes.md"
```

Use the actual filenames discovered in Step 11.

**Do not execute yet.** Per AGENTS.md, ask explicitly: "Shall I create the GitHub release with the above assets and notes?" Wait for explicit approval before running `gh release create`.
