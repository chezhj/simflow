#!/bin/bash
# simflow_config.sh - per-app deploy configuration, owned by this repo.
#
# Secret-free by design, so it is safe to commit here. The release workflow
# (.github/workflows/release-deploy.yaml) rsyncs it to ~/domains/simflow_config.sh
# on every deploy, where activate.sh / rollback.sh source it. Real secrets live in
# shared/simflow/.env on the server (see www_installer's docs/BOOTSTRAP.md).
#
# There is no GITHUB_URL / FILES_TO_COPY here anymore: CI rsyncs the whole
# built tree straight into releases/<app>_<tag>/, so nothing needs to be
# fetched or itemized on the server side.

DOMAIN="simflow.vdwaal.net"
DOMAIN_BASE_DIR="/home/vdwanet/domains/"      # must have trailing slash
VERSION_FILE="smart_training_checklist/__init__.py"   # file containing __version__ = "x.y.z"
PYTHON_ENV="/home/vdwanet/virtualenv/domains/simflow.vdwaal.net/3.11/bin/activate"
# activate.sh defaults DJANGO_SETTINGS_MODULE to config.settings.prod; this app's
# settings package differs, so it MUST be set explicitly here.
DJANGO_SETTINGS_MODULE="smart_training_checklist.settings.prod"

DATABASE_ENGINE="sqlite"                    # sqlite | mysql
DATABASE_SOURCE="production"                # production | repository

# No media/uploads in this app - db.sqlite3 and .env are symlinked from
# shared/<app>/ automatically, so no extra shared paths are needed.
SHARED_PATHS=()

# Checklist content is deployed together with the app: after migrate, wipe and
# reload the content tables (SOP, Attribute, Procedure, CheckItem) from the
# fixture shipped inside this release (checklist/fixtures/checklist_content.json).
# User and session data are never touched by this command. activate.sh guards
# each command with a `manage.py help --commands` check, so it is safely skipped
# (not an error) when re-activating an older tag that lacks the command.
POST_MIGRATE_COMMANDS=("checklist_content import --replace --noinput")

# Used by the GitHub Actions smoke test after activation.
SMOKE_URL="https://simflow.vdwaal.net/"
