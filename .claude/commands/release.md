---
description: Full release workflow — CI check, doc/changelog updates, version bump, tag, push, and GitHub release with approval gates.
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

Evaluate the result:

- `status == "completed"` and `conclusion == "success"` → continue.
- `status == "in_progress"` or `status == "queued"` → **wait**. Poll every ~2 minutes until the run completes, reporting progress at each poll. Continue if it passes; abort if it fails.
- No run found (commit not pushed) or `conclusion != "success"` → **abort immediately**. Tell the user to push the commit and ensure CI passes, then re-run `/release`.

---

## Step 2 — Update README.md and docs/

Read `README.md` and every file in `docs/`. Compare their content against the current state of the codebase (code, tests, config). Identify anything that is stale, inaccurate, or missing.

Key areas to audit:
- Feature lists and capability descriptions in README.md
- Installation steps in `docs/install.md` and `docs/manual.md`
- Any version-specific version numbers embedded in docs
- Pipeline or algorithm descriptions in `docs/dithering.md` — compare against `python/hokku/webserver/` code
- Metric definitions in `docs/screens/huessen_epf1301/image_quality.md` — compare against `python/hokku/webserver/image_quality.py`

For each proposed change, show the user the before/after diff and wait for confirmation before applying it. If no changes are needed, say so and continue.

---

## Step 3 — Show existing tags; ask user for the new version

Run:
```
git tag --sort=-version:refname | head -15
```

Display the list, then ask the user:

> What version tag should this release use? (e.g. `v3.0.0` or `v3.0.0-beta.1`)

**Version rules (validate before continuing):**
- PATCH must be **even** (0, 2, 4, …). Odd PATCH is the development track — reject it and ask again.
- The new tag must sort strictly greater than the last tag.

From the user's answer, derive all format variants and display them for confirmation:

| Format | Example |
|--------|---------|
| Git tag (as entered) | `v3.0.2` |
| PEP 440 — `python/pyproject.toml` | `3.0.2` |
| Debian — `python/debian/changelog` | `3.0.2-1` |
| CHANGELOG.md heading | `3.0.2` |

Conversion rules:
- `v3.0.2` → PEP 440 `3.0.2`, Debian `3.0.2-1`, heading `3.0.2`
- `v3.1.0` → PEP 440 `3.1.0`, Debian `3.1.0-1`, heading `3.1.0`
- `v4.0.0-beta.1` → PEP 440 `4.0.0b1`, Debian `4.0.0~beta.1-1`, heading `4.0.0 beta 1`
- `v4.0.0-rc.1` → PEP 440 `4.0.0rc1`, Debian `4.0.0~rc.1-1`, heading `4.0.0 rc 1`

The Debian revision suffix is **always `-1`** — there is no build number concept for releases.

Wait for the user to confirm the derived versions before continuing.

Also show the current firmware version:
```
cat firmware/huessen_epf1301/VERSION
```
Note whether it has changed since the last release tag (compare `firmware/huessen_epf1301/VERSION` content at HEAD vs at the last tag using `git show <last-tag>:firmware/huessen_epf1301/VERSION`).

---

## Step 4 — Update CHANGELOG.md

Get the git log since the last tag:
```
git tag --sort=-version:refname | head -1   # last tag
git log <last-tag>..HEAD --oneline
```

Read the existing `CHANGELOG.md`. Prepend a new section for the new version using the format already established in the file. Write this section in user-friendly "what, not how" language: user-visible features and fixes, not implementation or refactoring details. Group related items under descriptive sub-headings if there are more than a few changes.

Present the draft CHANGELOG section to the user for review and editing before saving. Apply their edits.

**Note:** This section covers only changes since the previous tag. The GitHub release text (Step 10) may span a wider range.

---

## Step 5 — Update version files

Edit **`python/pyproject.toml`**: change the `version = "..."` line to the PEP 440 form.

Edit **`python/debian/changelog`**: prepend a new entry at the top in Debian RFC 822 format:

```
hokku-server (<debian-version>) unstable; urgency=medium

  * Release <git-tag>

 -- Dennis Fleurbaaij <mail@dennisfleurbaaij.com>  <current date in RFC 2822 format>
```

The Debian version is always `MAJOR.MINOR.PATCH-1` — the `-1` suffix is fixed for every release.

RFC 2822 date format: `Sun, 17 May 2026 14:32:00 +0000` — use the actual current UTC date **and time** (not midnight).

If the firmware version changed since the last release, note it in the changelog entry as well.

---

## Step 6 — Commit the release changes

Stage exactly these files (and only these):
- `python/pyproject.toml`
- `python/debian/changelog`
- `CHANGELOG.md`
- Any `README.md` or `docs/*.md` files that were modified in Step 2

Run:
```
git commit -m "chore: release <git-tag>"
```

No approval is required for a commit (per AGENTS.md). After the commit, capture the new commit SHA:
```
git rev-parse HEAD
```

---

## Step 7 — Tag and push (REQUIRES explicit user approval)

State the exact three commands that will run:

```
git tag <git-tag>
git push
git push origin <git-tag>
```

**Do not execute them yet.** Wait for the user to give explicit approval — phrases like "go ahead", "yes", "push it", "do it". Per AGENTS.md, "looks good", "I see", "that's right", and similar are NOT authorisation.

Once approved, run those three commands in sequence.

---

## Step 8 — Wait for GitHub Actions CI to complete

After push, get the new commit SHA and poll until the CI run completes. Check every ~2 minutes:

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

## Step 9 — Download CI artifacts

Download **all** firmware artifacts and the webserver `.deb` from the successful
CI run. Every screen model's firmware must be attached to the release so the
server's "download firmware from GitHub" feature works for all of them (not just
huessen):

```powershell
New-Item -ItemType Directory -Force "$env:TEMP\hokku-release"
gh run download <run-id> --name hokku-huessen_epf1301-firmware --dir "$env:TEMP\hokku-release"
gh run download <run-id> --name hokku-seeedstudio_e1004-firmware --dir "$env:TEMP\hokku-release"
gh run download <run-id> --name hokku-bigme_f7-firmware --dir "$env:TEMP\hokku-release"
gh run download <run-id> --name hokku-server-deb --dir "$env:TEMP\hokku-release"
```

List the downloaded files and their sizes. Verify that all of these are present:
- `hokku-huessen_epf1301-<version>.bin`
- `hokku-seeedstudio_e1004-<version>.bin`
- `hokku-bigme_f7-<version>.img`
- one `.deb` file (webserver)

If any firmware artifact is missing, report and stop — a release with an
incomplete set of firmware assets breaks the in-app firmware download for the
missing model(s).

The firmware filenames embed each screen's `VERSION` string (e.g.,
`hokku-huessen_epf1301-1.2.0.bin`, `hokku-bigme_f7-1.2.2.img`). Note whether any
is a new firmware version compared to the previous release.

---

## Step 10 — Create GitHub release (REQUIRES explicit user approval)

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
- Include a **Versions** section listing:
  - App: `<git-tag>` (e.g., `v3.0.2`)
  - Firmware: `<firmware/huessen_epf1301/VERSION>` (e.g., `1.2.0`) — add "(unchanged)" if firmware version matches the previous release

Present the draft release notes to the user for review and editing. Apply any edits.

**Then state the exact commands that will run:**

Write the release notes to a temp file to avoid quoting issues:
```powershell
Set-Content -Path "$env:TEMP\hokku-release-notes.md" -Value @'
<release notes>
'@
```

Then create the release, attaching **every** firmware artifact plus the `.deb`:
```powershell
gh release create <git-tag> `
  "$env:TEMP\hokku-release\<huessen-firmware>.bin" `
  "$env:TEMP\hokku-release\<seeed-firmware>.bin" `
  "$env:TEMP\hokku-release\<bigme_f7-firmware>.img" `
  "$env:TEMP\hokku-release\<webserver-filename>.deb" `
  --title "Hokku e-paper server <git-tag>" `
  --notes-file "$env:TEMP\hokku-release-notes.md"
```

Use the actual filenames discovered in Step 9. If this is a `-beta`/`-rc` tag,
also pass `--prerelease` so the server's firmware-library treats these assets as
beta (never auto-selected).

**Do not execute yet.** Per AGENTS.md, ask explicitly: "Shall I create the GitHub release with the above assets and notes?" Wait for explicit approval before running `gh release create`.

After the release is created, verify it:
```
gh release view <git-tag>
```

Confirm the release title, tag, and that all four assets (three firmware images
+ the `.deb`) are listed with non-zero sizes. Report the release URL to the user.
