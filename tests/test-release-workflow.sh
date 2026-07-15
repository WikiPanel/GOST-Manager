#!/usr/bin/env bash
# shellcheck disable=SC2016
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=integration-test-lib.sh
source "${ROOT_DIR}/tests/integration-test-lib.sh"

WORKFLOW="${ROOT_DIR}/.github/workflows/release.yml"
INTEGRATION_WORKFLOW="${ROOT_DIR}/.github/workflows/monitoring-integration.yml"
CHECKOUT_SHA="11bd71901bbe5b1630ceea73d27597364c9af683"
TEST_HOME="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/gost-release-workflow-tests.XXXXXX")" && pwd -P)"
cleanup_test_home() {
  local status=$?
  rm -rf "${TEST_HOME}"
  exit "${status}"
}
trap cleanup_test_home EXIT

assert_order() {
  local name="$1"
  local first="$2"
  local second="$3"
  local first_line second_line
  first_line="$(grep -nF -- "${first}" "${WORKFLOW}" | head -n 1 | cut -d: -f1)"
  second_line="$(grep -nF -- "${second}" "${WORKFLOW}" | head -n 1 | cut -d: -f1)"
  if [[ -n "${first_line}" && -n "${second_line}" && "${first_line}" -lt "${second_line}" ]]; then
    pass "${name}"
  else
    fail "${name}"
  fi
}

assert_checkout_pin() {
  local name="$1"
  local workflow="$2"
  local checkout_ref checkout_line comment_line
  checkout_ref="$(sed -n 's/^[[:space:]-]*uses: actions\/checkout@//p' "${workflow}")"
  assert_eq "${name} uses one full checkout commit SHA" "${CHECKOUT_SHA}" "${checkout_ref}"
  assert_eq "${name} checkout reference is 40 characters" "40" "${#checkout_ref}"
  assert_not_contains "${name} has no floating checkout v4 tag" "actions/checkout@v4" "${workflow}"
  assert_contains "${name} names the reviewed checkout release" \
    "# actions/checkout v4.2.2" "${workflow}"
  checkout_line="$(grep -nF "actions/checkout@${CHECKOUT_SHA}" "${workflow}" | cut -d: -f1)"
  comment_line="$(grep -nF '# actions/checkout v4.2.2' "${workflow}" | cut -d: -f1)"
  assert_eq "${name} keeps the release comment adjacent to checkout" \
    "$((checkout_line - 1))" "${comment_line}"
}

assert_file "release workflow exists" "${WORKFLOW}"
assert_file "integration workflow exists" "${INTEGRATION_WORKFLOW}"
assert_checkout_pin "release workflow" "${WORKFLOW}"
assert_checkout_pin "integration workflow" "${INTEGRATION_WORKFLOW}"
assert_contains "integration records trusted OS metadata directories" \
  "ls -ld /etc /usr /usr/lib" "${INTEGRATION_WORKFLOW}"
assert_contains "integration records /etc/os-release symlink layout" \
  "ls -l /etc/os-release" "${INTEGRATION_WORKFLOW}"
assert_contains "integration records canonical OS metadata target" \
  "readlink -f /etc/os-release" "${INTEGRATION_WORKFLOW}"
assert_contains "integration records candidate OS metadata stat" \
  "stat /etc/os-release" "${INTEGRATION_WORKFLOW}"
assert_contains "integration records resolved OS metadata stat" \
  'stat "$(readlink -f /etc/os-release)"' "${INTEGRATION_WORKFLOW}"
assert_contains "integration runs only focused OS metadata preflight" \
  "source ./setup.sh; read_os_release" "${INTEGRATION_WORKFLOW}"
assert_contains "tag trigger exists" "- 'v*.*.*'" "${WORKFLOW}"
assert_contains "manual dispatch exists" "workflow_dispatch:" "${WORKFLOW}"
assert_contains "manual version input exists" "version:" "${WORKFLOW}"
assert_contains "manual publish input is boolean" "type: boolean" "${WORKFLOW}"
assert_contains "manual publication defaults off" "default: false" "${WORKFLOW}"
assert_contains "release has minimal write permission" "contents: write" "${WORKFLOW}"
assert_contains "checkout follows the exact event ref" "github.ref" "${WORKFLOW}"
assert_contains "manual bare version is normalized" "format('v{0}', inputs.version)" "${WORKFLOW}"
assert_contains "checkout fetches tags" "fetch-depth: 0" "${WORKFLOW}"
assert_contains "semantic tag is validated" '^v[0-9]+\.[0-9]+\.[0-9]+$' "${WORKFLOW}"
assert_contains "VERSION file safety is validated" "[[ -f VERSION && ! -L VERSION ]]" "${WORKFLOW}"
assert_contains "VERSION syntax is validated" '[[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]' "${WORKFLOW}"
assert_contains "tag and VERSION equality is checked" '[[ "${tag}" == "v${version}" ]]' "${WORKFLOW}"
assert_contains "release notes path is versioned" 'docs/releases/${tag}.md' "${WORKFLOW}"
assert_contains "release notes must be nonempty" '-s "${notes}"' "${WORKFLOW}"
assert_contains "tag commit is resolved" 'git rev-list -n 1 "refs/tags/${tag}"' "${WORKFLOW}"
assert_contains "tag commit must equal HEAD" '[[ "${tag_commit}" == "${head_commit}" ]]' "${WORKFLOW}"
assert_contains "complete validation uses make check" "make check" "${WORKFLOW}"
assert_contains "validation runs diff check" "git diff --check" "${WORKFLOW}"
assert_contains "validation requires clean source" "git status --porcelain --untracked-files=all" "${WORKFLOW}"
assert_contains "Python cache stays outside source" 'PYTHONPYCACHEPREFIX: ${{ runner.temp }}/pycache' "${WORKFLOW}"
assert_order "tests complete before packaging" "Run complete validation suite" "Build deterministic release assets"
assert_contains "packaging uses tracked files only" "git ls-files -z" "${WORKFLOW}"
assert_contains "tracked files have stable ordering" "LC_ALL=C sort -z" "${WORKFLOW}"
assert_contains "archive uses GNU deterministic format" "--format=gnu" "${WORKFLOW}"
assert_contains "archive owner is normalized" "--owner=0" "${WORKFLOW}"
assert_contains "archive group is normalized" "--group=0" "${WORKFLOW}"
assert_contains "archive uses numeric ownership" "--numeric-owner" "${WORKFLOW}"
assert_contains "archive timestamp is normalized" '--mtime="@${source_date_epoch}"' "${WORKFLOW}"
assert_contains "archive has exactly one release prefix" '--transform="s,^,GOST-Manager-${VERSION}/,"' "${WORKFLOW}"
assert_contains "gzip timestamp is suppressed" "gzip -n -9" "${WORKFLOW}"
assert_contains "archive uses expected name" "gost-manager.tar.gz" "${WORKFLOW}"
assert_contains "checksum uses expected name" "gost-manager.tar.gz.sha256" "${WORKFLOW}"
assert_contains "checksum is generated with SHA256" 'sha256sum "$(basename "${archive}")"' "${WORKFLOW}"
assert_contains "generated checksum is verified locally" 'sha256sum -c "$(basename "${checksum}")"' "${WORKFLOW}"
assert_contains "asset directory must contain two files" '-eq 2' "${WORKFLOW}"
assert_contains "archive safety listing is generated" 'tar -tzf "${ARCHIVE}"' "${WORKFLOW}"
assert_contains "archive types are validated" 'tar -tvzf "${ARCHIVE}"' "${WORKFLOW}"
assert_contains "archive must include setup" "setup.sh install.sh gost-manager.sh" "${WORKFLOW}"
assert_contains "archive must include VERSION" "for required in VERSION" "${WORKFLOW}"
assert_contains "archive must include monitoring init" "monitoring/__init__.py" "${WORKFLOW}"
assert_contains "archive must include Watchdog init" "gost_watchdog/__init__.py" "${WORKFLOW}"
assert_contains "archive must include Watchdog manifest" "packaging/watchdog-runtime-manifest.txt" "${WORKFLOW}"
assert_contains "existing release blocks publication" 'gh release view "${TAG}"' "${WORKFLOW}"
assert_contains "workflow uses GitHub CLI" 'gh release create "${TAG}"' "${WORKFLOW}"
assert_contains "release uses exact existing tag" "--verify-tag" "${WORKFLOW}"
assert_contains "release notes file is supplied" '--notes-file "${NOTES}"' "${WORKFLOW}"
assert_contains "release is staged as a draft until assets verify" "--draft" "${WORKFLOW}"
assert_contains "final release is non-draft" "--draft=false" "${WORKFLOW}"
assert_contains "final release is non-prerelease" "--prerelease=false" "${WORKFLOW}"
assert_contains "release uses built-in token" 'GH_TOKEN: ${{ github.token }}' "${WORKFLOW}"
assert_contains "published assets are checked exactly" "expected_assets=" "${WORKFLOW}"
assert_contains "release publication passes the verified archive" '"${ARCHIVE}"' "${WORKFLOW}"
assert_contains "release publication passes the verified checksum" '"${CHECKSUM}"' "${WORKFLOW}"
assert_contains "incomplete release is deleted" 'gh release delete "${TAG}" --yes' "${WORKFLOW}"
assert_contains "asset upload failure is explicit" "asset upload failed" "${WORKFLOW}"
assert_contains "manual validation-only condition exists" "!inputs.publish" "${WORKFLOW}"
assert_contains "manual validation-only result is explicit" "Release validated without publication." "${WORKFLOW}"
assert_order "release existence check follows archive validation" "Validate generated archive" "Refuse an unexpected existing release"
assert_order "publication follows release existence check" "Refuse an unexpected existing release" "Publish verified GitHub Release"
assert_not_contains "workflow has no third-party release action" "softprops/action-gh-release" "${WORKFLOW}"
assert_not_contains "workflow has no alternate third-party release action" "ncipollo/release-action" "${WORKFLOW}"
assert_not_contains "workflow never creates a tag" "git tag" "${WORKFLOW}"
assert_not_contains "workflow never pushes a tag" "git push" "${WORKFLOW}"

if tar --version 2>/dev/null | grep -Fq 'GNU tar'; then
  version="$(< "${ROOT_DIR}/VERSION")"
  source_date_epoch="$(git -C "${ROOT_DIR}" show -s --format=%ct HEAD)"
  file_list="${TEST_HOME}/release-files.zlist"
  git -C "${ROOT_DIR}" ls-files -z | LC_ALL=C sort -z > "${file_list}"

  build_archive() {
    local archive="$1"
    (
      cd "${ROOT_DIR}"
      tar \
        --create \
        --format=gnu \
        --null \
        --files-from="${file_list}" \
        --sort=name \
        --owner=0 \
        --group=0 \
        --numeric-owner \
        --mtime="@${source_date_epoch}" \
        --transform="s,^,GOST-Manager-${version}/," \
        --file="${archive%.gz}"
    )
    gzip -n -9 "${archive%.gz}"
  }

  first_archive="${TEST_HOME}/first.tar.gz"
  second_archive="${TEST_HOME}/second.tar.gz"
  build_archive "${first_archive}"
  build_archive "${second_archive}"
  if cmp -s "${first_archive}" "${second_archive}"; then
    pass "GNU tar release archives are byte-reproducible"
  else
    fail "GNU tar release archives are byte-reproducible"
  fi
  checksum="${TEST_HOME}/gost-manager.tar.gz.sha256"
  cp "${first_archive}" "${TEST_HOME}/gost-manager.tar.gz"
  if (
    cd "${TEST_HOME}"
    sha256sum gost-manager.tar.gz > "${checksum}"
    sha256sum -c "${checksum}" >/dev/null
  ); then
    pass "GNU tar release checksum verifies locally"
  else
    fail "GNU tar release checksum verifies locally"
  fi
  listing="${TEST_HOME}/archive.list"
  tar -tzf "${first_archive}" > "${listing}"
  assert_eq "GNU tar archive has one top-level directory" "1" \
    "$(awk -F/ 'NF {print $1}' "${listing}" | LC_ALL=C sort -u | wc -l | tr -d ' ')"
  assert_contains "GNU tar archive includes VERSION" "GOST-Manager-${version}/VERSION" "${listing}"
  assert_contains "GNU tar archive includes setup.sh" "GOST-Manager-${version}/setup.sh" "${listing}"
  assert_eq "GNU tar archive contains every tracked release file" \
    "$(git -C "${ROOT_DIR}" ls-files | wc -l | tr -d ' ')" \
    "$(wc -l < "${listing}" | tr -d ' ')"
else
  printf 'SKIP: deterministic asset execution requires GNU tar\n'
fi

finish_suite
