# Release Howto

Three independent release processes share one mono-repo. Each has its own version and git tag namespace.

| Artefact | Tag format | Tool |
|---|---|---|
| Web app | `v*` | `cz bump` |
| X-Plane plugin | `plugin-v*` | `scripts/bump_plugin.py` |
| Checklist content (SOP) | `sop-{icao}-v*` | `scripts/bump_content_version.py` |

---

## 1. Web app release

### Prerequisites
- Working tree is clean on `master`, all tests pass (`pytest`).

### Steps

**1. Bump version and push**

```
cz bump
```

Commitizen updates `smart_training_checklist/__init__.py` and `pyproject.toml`, appends an entry to `CHANGELOG.md`, creates a commit and tag (`v<new>`), then runs the `push_all` post-hook which executes `git push --follow-tags`.

**2. Deploy to production** (run on the prod server)

```
install.sh app <version-tag>   # git pull / clone at that tag
deploy.sh app <version-tag>    # stop, deploy new code, run migrations, load fixtures, start
```

---

## 2. Plugin release

Plugin changes are versioned independently of the web app. Pushing a `plugin-v*` tag triggers GitHub Actions which builds the distributable zip and publishes a GitHub Release automatically.

### Prerequisites
- Working tree is clean on `master`.
- Plugin changes in `xplane_plugin/` are complete and tested.

### Steps

**1. Phase 1 — prepare**

```
python scripts/bump_plugin.py <new-version>
```

- Updates `PLUGIN_VERSION` in `xplane_plugin/xFlow/PI_xFlow.py`.
- Prepends a stub section to `xplane_plugin/CHANGELOG.md`.
- Creates commit `chore(plugin): bump version to X.Y.Z`.
- Tags `plugin-vX.Y.Z`.

**2. Edit the release notes**

Fill in `xplane_plugin/CHANGELOG.md` under the new version heading (replace the `(fill in release notes)` placeholder).

**3. Phase 2 — amend and push**

```
python scripts/bump_plugin.py --push
```

- Stages the edited CHANGELOG.
- Amends the bump commit (preserves the commit message).
- Moves the tag to the amended commit.
- Pushes the branch and the tag.

**4. GitHub Actions runs automatically**

Pushing `plugin-v*` triggers `.github/workflows/plugin-release.yml`, which:

- Restructures files into the correct XPPython3 install layout:
  ```
  PI_xFlow.py          ← XPPython3 requires this at PythonPlugins/ root
  xFlow/
    config.ini
  README.txt
  ```
- Creates `xflow-plugin.zip`.
- Publishes a GitHub Release with the zip as the downloadable asset.

Users download from `https://github.com/chezhj/simflow/releases`.

---

## 3. SOP (checklist content) release

A SOP bump records a new `content_version` and `release_notes` in the database. The production database receives the update when the fixture is loaded by `deploy.sh`.

### Prerequisites
- All checklist changes (via Django admin or migration) are applied to the **dev** database.
- Working tree is on `master`.

### Steps

**1. Export the fixture**

```
python manage.py checklist_content export
```

Dumps `Attribute`, `Procedure`, and `CheckItem` to `checklist/fixtures/checklist_content.json` using natural keys. Prints a record count per model so you can sanity-check the output.

**2. Stage the fixture**

```
git add checklist/fixtures/checklist_content.json
```

**3. Run the bump script**

```
python scripts/bump_content_version.py B738 <new-version>
```

The script:
- Drafts release notes from `git log` — commits touching `checklist/migrations/` or `checklist/fixtures/` since the previous `sop-B738-v*` tag.
- Opens the draft in `$EDITOR` (if `$EDITOR` is not set, it prints the temp file path and waits for you to edit it manually).
- Strips comment lines (lines starting with `#`) from what you save.
- Updates `SOP.content_version` and `SOP.release_notes` in the dev DB via `manage.py shell`.
- Commits the staged fixture plus the DB-touching shell command as `chore(sop): bump B738 content to X.Y.Z`.
- Tags `sop-B738-vX.Y.Z`.

**4. Push**

```
git push && git push origin sop-B738-v<new-version>
```

**5. Deploy to production** (run on the prod server)

```
install.sh app <version-tag>   # use the app tag that contains this commit
deploy.sh app <version-tag>    # loads the fixture, updating SOP.content_version on prod
```

> `deploy.sh` loads the fixture on production. The equivalent manual command is:
> ```
> python manage.py checklist_content import --replace
> ```
> `--replace` wipes existing `Attribute`, `Procedure`, and `CheckItem` rows before loading, which avoids PK conflicts on a clean deploy. It asks for confirmation before deleting.
