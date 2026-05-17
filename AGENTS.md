# Agent rules for hokku_epaper

Sub-directory rules: [firmware/AGENTS.md](firmware/AGENTS.md) | [webserver/AGENTS.md](webserver/AGENTS.md)

## Python environment
- Venv: `.venv/` at repo root
- Windows: `.venv/Scripts/python` | Linux/macOS: `.venv/bin/python`
- Dependencies: `requirements.txt` (direct deps, unpinned; pip resolves transitive)
- Recreate: `pip install -r requirements.txt`

## Git / release rules
- CANNOT `git push` without explicit human permission
- CANNOT push a GitHub release without explicit human permission
- CANNOT `git tag` without explicit human permission
- CAN `git commit`; commit message MUST be descriptive
- "Looks good", "tests pass", "I see the fix works" are NOT authorisations to push/release/tag
- Ask explicitly before every `gh release upload`, `gh release delete-asset`, `gh release create`, `gh release edit`, or force-push of a tag
- Before running `gh` commands after authorisation, state exact filenames and release tag for user veto

## Tool scripts
- All standalone Python helper/dev scripts belong in `tools/`
- Do not create or leave `.py` files in the repo root

## Hardware
- Known facts: `docs/hardware_facts.md` (may be inaccurate — treat with caution)

## Repository
- GitHub: `https://github.com/defl/hokku_epaper`
