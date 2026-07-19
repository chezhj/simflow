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
deploy.sh app <version-tag>    # stop, deploy new code, run migrations, start
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

`checklist/fixtures/checklist_content.json` is the **source of truth** for checklist content. You edit it by hand and import it into the dev DB — the DB is a copy, never the origin. A SOP bump writes a new `content_version` and `release_notes` into that fixture, and production picks them up when the fixture is loaded there.

### Prerequisites
- All checklist changes are made in the fixture and imported into the **dev** database.
- Working tree is on `master`.

### Steps

**1. Edit the fixture**

Make your checklist changes directly in `checklist/fixtures/checklist_content.json`, then load them:

```
python manage.py checklist_content import
```

Do **not** run `checklist_content export` as part of this flow — it rewrites the whole fixture from the DB and will discard anything the DB does not have. It exists only to capture changes made through the Django admin, and asks for confirmation before overwriting.

**2. Run the bump script**

```
python scripts/bump_content_version.py B738 <new-version>
```

The script:
- Drafts release notes from `git log` — commits touching `checklist/migrations/` or `checklist/fixtures/` since the previous `sop-B738-v*` tag.
- Opens the draft in `$EDITOR` (if `$EDITOR` is not set, it prints the temp file path and waits for you to edit it manually).
- Strips comment lines (lines starting with `#`) from what you save.
- Writes `content_version`, `release_notes` and `updated_at` into the `checklist.sop` record in the fixture.
- Runs `checklist_content import` so the dev DB matches the fixture.
- Stages and commits the fixture as `chore(sop): bump B738 content to X.Y.Z`.
- Tags `sop-B738-vX.Y.Z`.

**3. Push**

```
git push && git push origin sop-B738-v<new-version>
```

**4. Deploy to production** (run on the prod server)

```
install.sh app <version-tag>   # use the app tag that contains this commit
deploy.sh app <version-tag>
```

> ⚠️ `deploy.sh` (in the separate deploy repo) does **not** load the fixture yet. Until it does, load it manually on prod after deploying:
> ```
> python manage.py checklist_content import --replace
> ```
> `--replace` wipes existing `Attribute`, `Procedure`, and `CheckItem` rows before loading, which avoids PK conflicts on a clean deploy. It asks for confirmation before deleting. `SOP` is not wiped — `loaddata` updates it in place by PK.
