#!/usr/bin/env bash
# Exercise rookery on aarch64: architecture detection, gating of x86_64-only programs, installs of
# every per-architecture build, and a run of each installed command. Exits 1 when any check fails.
set -u

failures=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() {
    printf 'FAIL  %s\n' "$1"
    failures=$((failures + 1))
}
section() { printf '\n== %s\n' "$1"; }

# check NAME COMMAND...: pass when COMMAND exits 0, printing the head of its output either way
check() {
    local name=$1 output
    shift
    if output=$("$@" 2>&1); then pass "$name"; else fail "$name"; fi
    printf '%s\n' "$output" | head -n 3 | sed 's/^/      /'
}

# expect_text NAME TEXT OUTPUT: pass when OUTPUT contains TEXT
expect_text() {
    if grep -qF -- "$2" <<<"$3"; then pass "$1"; else fail "$1 (missing: $2)"; fi
}

# expect_arm NAME PATH: pass when PATH, after following links, is an aarch64 ELF binary
expect_arm() {
    local kind
    kind=$(file -bL "$2" 2>&1)
    if grep -qF 'ARM aarch64' <<<"$kind"; then pass "$1 is aarch64"; else fail "$1 is aarch64 (file: $kind)"; fi
}

section "Host"
if [ "$(uname -m)" = aarch64 ]; then pass "uname -m reports aarch64"; else fail "uname -m reports $(uname -m)"; fi
rookery --version

section "Detection"
output=$(rookery status 2>&1)
printf '%s\n' "$output"
expect_text "status detects aarch64" "Architecture: aarch64 (detected)" "$output"
expect_text "info detects aarch64" "Architecture: aarch64" "$(rookery info 2>&1)"
expect_text "ROOKERY_ARCH=amd64 overrides detection" "Architecture: x86_64 (ROOKERY_ARCH)" \
    "$(ROOKERY_ARCH=amd64 rookery status 2>&1)"
expect_text "invalid ROOKERY_ARCH is a configuration error" "ROOKERY_ARCH must be one of" \
    "$(ROOKERY_ARCH=armv7 rookery list 2>&1)"

section "Catalog"
output=$(rookery list --all 2>&1)
printf '%s\n' "$output"
for program in blender netron storageexplorer; do
    expect_text "list explains $program" "Unavailable: $program has no build for aarch64" "$output"
done

section "x86_64-only programs are refused"
for program in blender netron storageexplorer; do
    output=$(rookery install "$program" 2>&1)
    status=$?
    if [ "$status" -eq 1 ] && grep -qF "has no build for aarch64" <<<"$output"; then
        pass "install $program refused"
    else
        fail "install $program refused (exit $status: $output)"
    fi
done

section "Per-architecture builds"
# program and the command that proves its binary runs
runs=(
    "bat|bat --version"
    "dust|dust --version"
    "dysk|dysk --version"
    "eza|eza --version"
    "fastfetch|fastfetch --version"
    "gdu|gdu --version"
    "gping|gping --version"
    "helm|helm version --short"
    "hyperfine|hyperfine --version"
    "just|just --version"
    "mc|mc --version"
    "nvim|nvim --version"
    "yazi|yazi --version"
)
for entry in "${runs[@]}"; do
    program=${entry%%|*}
    read -ra command <<<"${entry#*|}"
    check "install $program" rookery install "$program"
    expect_arm "$program" "$(command -v "${command[0]}")"
    check "run ${command[*]}" "${command[@]}"
done
# The AppImage needs FUSE and a display to run, so its build is checked by file type alone
check "install drawio" rookery install drawio
expect_arm "drawio AppImage" "$(find ~/.local/share/rookery-programs/drawio -name '*.AppImage' | head -n 1)"

section "Any-architecture programs"
for program in clip cuda-run fasttarutils imcat kpod meetrec tarssh; do
    check "install $program" rookery install "$program"
done
expect_arm "imcat (built from source)" "$(command -v imcat)"
for command in clip ftar tarssh cuda-run; do
    check "run $command --help" "$command" --help
done

section "Bulk commands"
output=$(rookery install --default 2>&1)
printf '%s\n' "$output"
for program in blender netron storageexplorer; do
    expect_text "install --default skips $program" "Skipped $program: has no build for aarch64" "$output"
done
check "update every installed program" rookery update

section "Summary"
if [ "$failures" -eq 0 ]; then
    echo "All checks passed"
else
    echo "$failures check(s) failed"
    exit 1
fi
