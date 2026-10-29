#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
helper=$script_dir/fetch_openpmix_pr_checkout.sh
test_dir=$(mktemp -d)
trap '/usr/bin/rm -rf -- "$test_dir"' EXIT

pass_count=0
pass()
{
    pass_count=$((pass_count + 1))
    printf 'ok - %s\n' "$1"
}
fail()
{
    printf 'not ok - %s\n' "$1" >&2
    exit 1
}

upstream=$test_dir/upstream.git
work=$test_dir/work
git init -q --bare "$upstream"
git init -q "$work"
git -C "$work" config user.name Test
git -C "$work" config user.email test@example.invalid
printf '%s\n' exact > "$work/source.txt"
git -C "$work" add source.txt
git -C "$work" commit -q -m exact
exact_sha=$(git -C "$work" rev-parse HEAD)
git -C "$work" push -q "$upstream" \
    "$exact_sha:refs/pull/17/head"

mkdir "$test_dir/run"
(
    cd "$test_dir/run"
    OPENPMIX_PR_CHECKOUT_TEST_MODE=1 \
        bash "$helper" 17 "$exact_sha" checkout \
        --test-upstream "$upstream" > evidence.txt
)
checkout=$test_dir/run/checkout
[[ $(git -C "$checkout" rev-parse HEAD) == "$exact_sha" ]] ||
    fail 'checked-out SHA changed'
if git -C "$checkout" symbolic-ref -q HEAD >/dev/null; then
    fail 'checkout is not detached'
fi
[[ -z $(git -C "$checkout" status --porcelain=v1 --untracked-files=all) ]] ||
    fail 'checkout is not clean'
grep -Fqx 'OPENPMIX_PR_PULL_REF=refs/pull/17/head' \
    "$test_dir/run/evidence.txt" || fail 'fixed PR ref is absent from evidence'
pass 'fixed upstream PR ref produces a detached clean exact-SHA checkout'

mkdir "$test_dir/changed"
set +e
(
    cd "$test_dir/changed"
    OPENPMIX_PR_CHECKOUT_TEST_MODE=1 \
        bash "$helper" 17 0000000000000000000000000000000000000000 \
        checkout --test-upstream "$upstream"
) >/dev/null 2>&1
status=$?
set -e
[[ $status == 5 ]] || fail 'changed prepared SHA did not return status 5'
pass 'a changed PR head is distinguished from a fetch failure'

mkdir "$test_dir/missing"
set +e
(
    cd "$test_dir/missing"
    OPENPMIX_PR_CHECKOUT_TEST_MODE=1 \
        bash "$helper" 18 "$exact_sha" checkout \
        --test-upstream "$upstream"
) >/dev/null 2>&1
status=$?
set -e
[[ $status == 6 ]] || fail 'missing PR ref did not return status 6'
pass 'a missing fixed PR ref fails closed'

for invalid in 0 01 -1 '17 ' abc; do
    if OPENPMIX_PR_CHECKOUT_TEST_MODE=1 bash "$helper" "$invalid" \
        "$exact_sha" unused --test-upstream "$upstream" >/dev/null 2>&1; then
        fail 'malformed PR number was accepted'
    fi
done
upper_sha=$(printf '%s' "$exact_sha" | tr '[:lower:]' '[:upper:]')
if OPENPMIX_PR_CHECKOUT_TEST_MODE=1 bash "$helper" 17 "$upper_sha" \
    unused --test-upstream "$upstream" >/dev/null 2>&1; then
    fail 'uppercase SHA was accepted'
fi
pass 'PR numbers and prepared SHAs must be canonical'

mkdir "$test_dir/no-override"
if (
    cd "$test_dir/no-override"
    bash "$helper" 17 "$exact_sha" checkout \
        --test-upstream "$upstream"
) >/dev/null 2>&1; then
    fail 'local upstream override was accepted outside test mode'
fi
pass 'production cannot select an alternate upstream repository'

printf '%s\n' submodule > "$work/submodule.txt"
git -C "$work" add submodule.txt
git -C "$work" commit -q -m submodule-target
gitlink_target=$(git -C "$work" rev-parse HEAD)
git -C "$work" update-index --add --cacheinfo \
    "160000,$gitlink_target,vendor/foreign"
git -C "$work" commit -q -m gitlink
gitlink_sha=$(git -C "$work" rev-parse HEAD)
git -C "$work" push -q --force "$upstream" \
    "$gitlink_sha:refs/pull/19/head"
mkdir "$test_dir/gitlink"
set +e
(
    cd "$test_dir/gitlink"
    OPENPMIX_PR_CHECKOUT_TEST_MODE=1 \
        bash "$helper" 19 "$gitlink_sha" checkout \
        --test-upstream "$upstream"
) >/dev/null 2>&1
status=$?
set -e
[[ $status == 7 ]] || fail 'gitlink was not classified as unauthorized'
pass 'PR-controlled gitlinks are rejected without submodule execution'

git -C "$work" rm -q --cached vendor/foreign
printf '%s\n' '[submodule "foreign"]' > "$work/.gitmodules"
git -C "$work" add .gitmodules
git -C "$work" commit -q -m gitmodules
gitmodules_sha=$(git -C "$work" rev-parse HEAD)
git -C "$work" push -q --force "$upstream" \
    "$gitmodules_sha:refs/pull/20/head"
mkdir "$test_dir/gitmodules"
set +e
(
    cd "$test_dir/gitmodules"
    OPENPMIX_PR_CHECKOUT_TEST_MODE=1 \
        bash "$helper" 20 "$gitmodules_sha" checkout \
        --test-upstream "$upstream"
) >/dev/null 2>&1
status=$?
set -e
[[ $status == 7 ]] || fail '.gitmodules was not classified as unauthorized'
pass 'PR-controlled .gitmodules metadata is rejected'

git -C "$work" rm -q .gitmodules
oac_work=$test_dir/oac-work
oac_upstream=$test_dir/oac.git
git init -q --bare "$oac_upstream"
git init -q "$oac_work"
git -C "$oac_work" config user.name Test
git -C "$oac_work" config user.email test@example.invalid
printf '%s\n' 'OAC build helper' > "$oac_work/oac.m4"
git -C "$oac_work" add oac.m4
git -C "$oac_work" commit -q -m oac
oac_sha=$(git -C "$oac_work" rev-parse HEAD)
git -C "$oac_work" push -q "$oac_upstream" HEAD:refs/heads/main
printf '%s\n' '[submodule "oac"]' ' path = config/oac' \
    ' url = ../../open-mpi/oac' > "$work/.gitmodules"
git -C "$work" add .gitmodules
git -C "$work" update-index --add --cacheinfo "160000,$oac_sha,config/oac"
git -C "$work" commit -q -m expected-oac
with_oac=$(git -C "$work" rev-parse HEAD)
git -C "$work" push -q "$upstream" "$with_oac:refs/pull/21/head"
mkdir "$test_dir/oac-run"
(
    cd "$test_dir/oac-run"
    OPENPMIX_PR_CHECKOUT_TEST_MODE=1 bash "$helper" 21 "$with_oac" checkout \
        --test-upstream "$upstream" --test-oac-upstream "$oac_upstream" > evidence.txt
)
oac_checkout=$test_dir/oac-run/checkout
[[ $(git -C "$oac_checkout/config/oac" rev-parse HEAD) == "$oac_sha" ]] ||
    fail 'exact OAC commit was not checked out'
[[ -f $oac_checkout/config/oac/oac.m4 ]] || fail 'OAC build files are absent'
grep -Fqx 'OPENPMIX_PR_GITLINK_COUNT=1' "$test_dir/oac-run/evidence.txt" ||
    fail 'OAC gitlink count was not recorded'
cp -a "$oac_checkout" "$test_dir/copied-checkout"
python3 "$script_dir/validate_openpmix_oac.py" "$test_dir/copied-checkout" --populated >/dev/null ||
    fail 'copied trusted build source did not retain a valid OAC checkout'
pass 'expected OAC is fetched at its exact revision and survives build-source copying'

printf '%s\n' changed >> "$oac_checkout/config/oac/oac.m4"
if python3 "$script_dir/validate_openpmix_oac.py" "$oac_checkout" --populated >/dev/null 2>&1; then
    fail 'dirty OAC checkout was accepted'
fi
pass 'modified OAC worktree is rejected'

git -C "$oac_checkout/config/oac" checkout -- oac.m4
git -C "$oac_checkout/config/oac" checkout -q --detach "$gitlink_target" 2>/dev/null &&
    fail 'unrelated parent commit unexpectedly exists in OAC'
git -C "$oac_checkout/config/oac" config user.name Test
git -C "$oac_checkout/config/oac" config user.email test@example.invalid
printf '%s\n' different > "$oac_checkout/config/oac/new-file"
git -C "$oac_checkout/config/oac" add new-file
git -C "$oac_checkout/config/oac" commit -q -m different
if python3 "$script_dir/validate_openpmix_oac.py" "$oac_checkout" --populated >/dev/null 2>&1; then
    fail 'wrong OAC revision was accepted'
fi
pass 'OAC revision must match the parent gitlink exactly'

for bad in url extra nested; do
    git -C "$work" reset -q --hard "$with_oac"
    if [[ $bad == url ]]; then
        git -C "$work" config -f .gitmodules submodule.oac.url https://example.invalid/oac.git
        git -C "$work" add .gitmodules
    elif [[ $bad == extra ]]; then
        git -C "$work" config -f .gitmodules submodule.oac.update '!false'
        git -C "$work" add .gitmodules
    else
        git -C "$work" update-index --add --cacheinfo "160000,$oac_sha,vendor/foreign"
    fi
    git -C "$work" commit -q -m "reject-$bad"
    if python3 "$script_dir/validate_openpmix_oac.py" "$work" >/dev/null 2>&1; then
        fail "unexpected OAC metadata accepted: $bad"
    fi
done
pass 'redirected URLs, submodule options, and additional gitlinks are rejected'

source_text=$(<"$helper")
[[ $source_text == *'https://github.com/openpmix/openpmix.git'* ]] ||
    fail 'fixed production upstream is missing'
[[ $source_text == *'--no-recurse-submodules'* ]] ||
    fail 'fetch does not disable recursive submodules'
[[ $source_text != *'clone_url'* && $source_text != *'ssh_url'* ]] ||
    fail 'checkout helper consumes a metadata URL'
pass 'checkout uses no PR-supplied URL and never recursively fetches submodules'

printf '1..%d\n' "$pass_count"
