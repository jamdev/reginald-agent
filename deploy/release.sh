#!/usr/bin/env bash
# Bump, verify, commit and tag, then push and publish a GitHub pre-release.
#
#   ./deploy/release.sh 0.2.0        # or v0.2.0 — the v is optional
#   ./deploy/release.sh 0.2.0 -n     # dry run: show every step, change nothing
#   ./deploy/release.sh 0.2.0 -y     # skip the confirmation before pushing
#   ./deploy/release.sh 1.0.2 --force # finish releasing the current version
#
# Run from your workstation, on main, with a clean tree. Deploying the result is a
# separate step on the host: sudo /opt/reginald/deploy/update.sh
#
# The version lives in pyproject.toml and is what `reginald --version` reports,
# so a tag without the matching bump makes the running system misreport which
# release it is. This keeps the two in step, which is the whole point.
set -euo pipefail

VERSION="${1:-}"
DRY_RUN=0
ASSUME_YES=0
FORCE=0
for arg in "${@:2}"; do
    case "$arg" in
        -n|--dry-run) DRY_RUN=1 ;;
        -y|--yes)     ASSUME_YES=1 ;;
        -f|--force)   FORCE=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ -z "$VERSION" ]; then
    cat >&2 <<EOF
usage: $0 <version> [-n|--dry-run] [-y|--yes] [-f|--force]

  e.g. $0 0.2.0        verify and push v0.2.0, then publish a GitHub pre-release
       $0 0.2.0 -n     show what would happen, change nothing
       $0 1.0.2 -f     finish an existing version whose tag is missing on origin

Current version: $(grep -m1 '^version = ' pyproject.toml | cut -d'"' -f2)
Latest tag:      $(git describe --tags --abbrev=0 2>/dev/null || echo '(none)')
EOF
    exit 2
fi

VERSION="${VERSION#v}"                   # accept 0.2.0 or v0.2.0
TAG="v$VERSION"
TAG_REF="refs/tags/$TAG"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

say()  { printf '>>> %s\n' "$1"; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '    would run: %s\n' "$*"; else "$@"; fi; }
die()  { echo "!!! $1" >&2; exit 1; }

# -- preconditions ---------------------------------------------------------
# All of them up front: a release that fails halfway leaves a bumped version
# or a local-only tag to clean up by hand.

say "checking preconditions"

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] \
    || die "not a semver version: $VERSION (expected N.N.N)"

CURRENT="$(grep -m1 '^version = ' pyproject.toml | cut -d'"' -f2)"
if [ "$VERSION" = "$CURRENT" ] && [ "$FORCE" != 1 ]; then
    die "pyproject.toml is already at $VERSION. To finish an unpublished tag, run: $0 $VERSION --force"
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = main ] || die "on branch '$BRANCH' — release from main"

[ -z "$(git status --porcelain)" ] || die "working tree is dirty — commit or stash first"

PUSH_URL="$(git remote get-url --push origin)"
remote_tag="$(git ls-remote --refs "$PUSH_URL" "$TAG_REF")"
[ -z "$remote_tag" ] || die "tag $TAG already exists on origin"

# An out-of-date main means tagging a commit that is not what origin has.
git fetch --no-tags --quiet "$PUSH_URL" refs/heads/main
LOCAL="$(git rev-parse main)"
REMOTE="$(git rev-parse FETCH_HEAD)"
if [ "$LOCAL" != "$REMOTE" ]; then
    if git merge-base --is-ancestor "$REMOTE" "$LOCAL"; then
        say "    main is ahead of origin/main"
    else
        die "main is behind or diverged from origin/main — pull first"
    fi
fi

push_main="refs/heads/main:refs/heads/main"
tag_commit=""
if git show-ref --verify --quiet "$TAG_REF"; then
    [ "$VERSION" = "$CURRENT" ] || die "tag $TAG already exists locally"
    tag_commit="$(git rev-parse --verify "$TAG_REF^{commit}")"
    git merge-base --is-ancestor "$tag_commit" main || die "local tag $TAG is outside main"
    tagged_version="$(git show "$tag_commit:pyproject.toml" | grep -m1 '^version = ' | cut -d'"' -f2)"
    [ "$tagged_version" = "$VERSION" ] || die "local tag $TAG has project version $tagged_version"
    say "reusing local tag $TAG at $tag_commit"
    if git merge-base --is-ancestor "$tag_commit" "$REMOTE"; then
        push_main=""
    else
        git merge-base --is-ancestor "$REMOTE" "$tag_commit" || die "local tag $TAG diverges from origin/main"
        push_main="$tag_commit:refs/heads/main"
    fi
fi

if [ "$DRY_RUN" != 1 ]; then
    command -v gh >/dev/null || die "GitHub CLI (gh) is required to publish a pre-release"
    # Use the same repository as the tag push even if gh defaults to a fork's upstream.
    RELEASE_REPO="$(gh repo view "$PUSH_URL" --json url --jq .url)" ||
        die "gh could not access origin; check your GitHub CLI credentials"
fi

# -- bump ------------------------------------------------------------------

bump_version() {
    say "bumping pyproject.toml from $CURRENT to $VERSION"
    if [ "$DRY_RUN" = 1 ]; then
        printf '    would set version = "%s"\n' "$VERSION"
    else
        python3 - "$VERSION" <<'PY'
import re, sys
from pathlib import Path

version = sys.argv[1]
path = Path("pyproject.toml")
text = path.read_text()
new, n = re.subn(
    r'(?ms)(^\[project\].*?^version = ")[^"]+(")',
    lambda m: m.group(1) + version + m.group(2),
    text,
    count=1,
)
if n != 1:
    sys.exit("could not find the [project] version field in pyproject.toml")
path.write_text(new)
PY
    fi
    say "refreshing uv.lock"
    run uv lock --quiet
}

# Verify the code that will ship, even if main advanced after a local tag was made.
verify_release() (
    if [ -n "$tag_commit" ] && [ "$tag_commit" != "$LOCAL" ]; then
        say "checking the tagged commit $tag_commit in a separate worktree"
        if [ "$DRY_RUN" != 1 ]; then
            verification_dir="$(mktemp -d)"
            git worktree add --quiet --detach "$verification_dir" "$tag_commit"
            trap 'git -C "$REPO_ROOT" worktree remove --force "$verification_dir"' EXIT
            cd "$verification_dir"
        fi
    fi
    if [ "$VERSION" = "$CURRENT" ]; then
        run uv lock --check
    fi
    say "running lint and tests"
    run make lint || die "lint failed — not releasing"
    run make test || die "tests failed — not releasing"
)

if [ "$VERSION" != "$CURRENT" ]; then bump_version; fi
verify_release
if [ "$VERSION" != "$CURRENT" ]; then
    run git add pyproject.toml uv.lock
    run git commit -q -m "Release $TAG"
fi
if [ -z "$tag_commit" ]; then
    run git tag -a "$TAG" -m "reginald $TAG"
fi

if [ "$DRY_RUN" = 1 ]; then
    if [ -n "$push_main" ]; then say "would push $push_main to origin"; fi
    say "would push $TAG to origin and publish a GitHub pre-release with empty notes"
    echo
    echo "dry run: nothing changed. Re-run without -n to release."
    exit 0
fi

if [ "$ASSUME_YES" != 1 ]; then
    echo
    read -rp "push $TAG to origin and publish a GitHub pre-release? [y/N] " reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
        say "push cancelled. To publish the local tag later, run: $0 $VERSION --force"
        exit 0
    fi
fi

say "pushing"
if [ -n "$push_main" ]; then
    git push --atomic origin "$push_main" "$TAG_REF"
else
    git push origin "$TAG_REF"
fi

say "publishing $TAG as a GitHub pre-release"
publish=(gh release create "$TAG" --repo "$RELEASE_REPO" --verify-tag --prerelease --title "$TAG" --notes "")
if ! "${publish[@]}"; then
    echo "The tag is on origin. To retry GitHub publication, run:" >&2
    printf '%q ' "${publish[@]}" >&2
    printf '\n' >&2
    exit 1
fi

say "released $TAG. To deploy it, run on the host: sudo /opt/reginald/deploy/update.sh $TAG"
if previous_tag="$(git describe --tags --abbrev=0 --match 'v[0-9]*' "$TAG_REF^" 2>/dev/null)"; then
    say "to return to $previous_tag on the host: sudo /opt/reginald/deploy/update.sh $previous_tag"
    echo "This restores the code only. Crossing a schema change also requires the pre-upgrade database backup."
fi
