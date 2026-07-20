#!/bin/bash
set -eu

artifact_dir=ci-artifacts
rm -rf -- "$artifact_dir"
mkdir -p -- "$artifact_dir"

pipeline_execution=unknown
if [[ -f .ci-state/pmix-decision.env ]]; then
    if grep -Fxq 'PMIX_RUN_SUITE=0' .ci-state/pmix-decision.env; then
        pipeline_execution='intentional skip'
    elif grep -Fxq 'PMIX_RUN_SUITE=1' .ci-state/pmix-decision.env; then
        pipeline_execution='full run'
    fi
fi
printf 'PMIx CI artifacts\nPipeline execution: %s\n' "$pipeline_execution" \
    > "$artifact_dir/artifact-summary.txt"

for path in output perflogs reports; do
    [[ -e $path || -L $path ]] || continue
    cp -a -- "$path" "$artifact_dir/"
done

for path in \
    stage/frontier/batch/pmix_test/build_pmix_*/rfm_build.sh \
    stage/frontier/batch/pmix_test/build_pmix_*/rfm_build.out \
    stage/frontier/batch/pmix_test/build_pmix_*/rfm_build.err \
    stage/frontier/batch/pmix_test/build_pmix_*/pmix-git/config.log \
    stage/frontier/batch/pmix_test/build_pmix_*/python-site-packages \
    stage/frontier/batch/pmix_test/fetch_pmix_*/pmix-commit.env
do
    [[ -e $path || -L $path ]] || continue
    mkdir -p -- "$artifact_dir/$(dirname -- "$path")"
    cp -a -- "$path" "$artifact_dir/$path"
done
