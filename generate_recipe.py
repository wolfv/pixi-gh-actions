#!/usr/bin/env python3
"""
Generate rattler-build recipe.yaml files for GitHub Actions.

A GitHub Action reference like `actions/checkout@v4.2.2` is packaged as a conda
package named `gh-action-actions-checkout` version `4.2.2`.

For each action this emits:
  recipe.yaml        — rattler-build recipe (runtime dep: only nodejs)
  build.sh           — Unix build script
  bld.bat            — Windows build script
  <repo>.sh          — Bash entry point with every declared input baked in
  <repo>.ps1         — PowerShell entry point (Windows)

The bash/PowerShell entry points are generated here (from the fetched action.yml)
so no Python or PyYAML is needed inside the conda package at all.

Usage:
    python generate_recipe.py actions/checkout@v4.2.2
    python generate_recipe.py actions/setup-python@v5.3.0 --output-dir ./recipes
    python generate_recipe.py actions/checkout@v4.2.2 --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml  (or: pixi install)")


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

def package_name(owner: str, repo: str) -> str:
    """Conda-safe package name, e.g. gh-action-actions-checkout."""
    return re.sub(r"[^a-z0-9-]", "-", f"gh-action-{owner}-{repo}".lower())


def bin_name(repo: str) -> str:
    """Short name used for the installed bin entry point, e.g. 'checkout'."""
    return re.sub(r"[^a-z0-9-]", "-", repo.lower())


def normalize_version(ref: str) -> str:
    """Strip a leading 'v' and validate the result looks like a semver."""
    v = ref.lstrip("v")
    if not re.match(r"^\d+(\.\d+)*", v):
        raise ValueError(
            f"Cannot derive a semver version from ref '{ref}'. "
            "Please use a tagged release like v4.2.2."
        )
    return v


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

GITHUB_RAW     = "https://raw.githubusercontent.com"
GITHUB_ARCHIVE = "https://github.com/{owner}/{repo}/archive/refs/tags/{ref}.tar.gz"


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return resp.read().decode()


def fetch_action_meta(owner: str, repo: str, ref: str) -> dict[str, Any]:
    """Return the parsed action.yml / action.yaml for the given ref."""
    for filename in ("action.yml", "action.yaml"):
        url = f"{GITHUB_RAW}/{owner}/{repo}/{ref}/{filename}"
        try:
            return yaml.safe_load(fetch_text(url))  # type: ignore[no-any-return]
        except urllib.error.HTTPError:
            continue
    raise ValueError(f"No action.yml / action.yaml found for {owner}/{repo}@{ref}.")


def sha256_of_url(url: str) -> str:
    """Stream-download *url* and return its hex SHA-256."""
    digest = hashlib.sha256()
    print(f"  Downloading to compute sha256: {url}", flush=True)
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        while chunk := resp.read(65_536):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Action type helpers
# ---------------------------------------------------------------------------

def action_type(meta: dict[str, Any]) -> str:
    return meta.get("runs", {}).get("using", "unknown")


def node_version(using: str) -> str | None:
    m = re.match(r"node(\d+)", using)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Bash entry point generator
# ---------------------------------------------------------------------------

def _sh_quote(s: str) -> str:
    """Single-quote a string for safe embedding in shell."""
    return "'" + s.replace("'", "'\\''") + "'"


def _is_expression_default(default: Any) -> bool:
    """Return True for GitHub expression defaults like ${{ github.token }}."""
    return bool(default and ("${{" in str(default) or "}}" in str(default)))


def render_bash_entry_point(owner: str, repo: str, meta: dict[str, Any]) -> str:
    """
    Generate a self-contained bash entry point.  Inputs are supplied as
    key=value positional args (mapping directly to INPUT_KEY env vars) or
    via the caller's environment.  No --flag-per-input complexity.

    Input priority (highest first):
      1. key=value positional args  →  export INPUT_KEY=value
      2. --inputs-json JSON string  →  parsed by node inline
      3. --inputs-file JSON/YAML    →  JSON via node, YAML via yq
      4. INPUT_* already in env     →  used as-is
      5. Baked-in literal defaults  →  set via bash := if still unset

    Expression defaults like ${{ github.token }} are skipped (they only
    make sense inside the real GitHub Actions runner); users must supply
    those values explicitly when running locally.
    """
    inputs_def: dict[str, Any] = meta.get("inputs", {})
    runs:       dict[str, Any] = meta.get("runs", {})
    bname        = bin_name(repo)
    action_name  = meta.get("name", f"{owner}/{repo}")
    description  = (meta.get("description") or "").strip()
    main_script  = runs.get("main", "dist/index.js")
    post_script  = runs.get("post") or ""

    L: list[str] = []

    def w(*lines: str) -> None:
        L.extend(lines)

    # --- Header ---
    w(
        "#!/usr/bin/env bash",
        f"# CLI entry point for {owner}/{repo}",
        "# Generated by generate_recipe.py — do not edit by hand.",
        "#",
        "# Inputs can be supplied as (highest precedence first):",
        "#   key=value positional args:  checkout token=ghp_xxx fetch-depth=0",
        "#   JSON string:                checkout --inputs-json '{\"token\":\"ghp_xxx\"}'",
        "#   JSON/YAML file:             checkout --inputs-file inputs.yml",
        "#   Environment variables:      INPUT_TOKEN=ghp_xxx checkout",
        "set -euo pipefail",
        "",
        "# ---- constants baked in at generate time ----",
        f"_OWNER={_sh_quote(owner)}",
        f"_REPO={_sh_quote(repo)}",
        f"_MAIN_SCRIPT={_sh_quote(main_script)}",
        f"_POST_SCRIPT={_sh_quote(post_script)}",
        "",
        "# Locate the conda prefix: this script lives at $PREFIX/bin/<name>",
        '_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        '_PREFIX="$(dirname "$_SCRIPT_DIR")"',
        'ACTION_DIR="$_PREFIX/share/gh-actions/$_OWNER/$_REPO"',
        "",
        "_post=0",
        '_workspace="${GITHUB_WORKSPACE:-$(pwd)}"',
        "_extra_env=()",
        "_json_inputs=''",
        "_file_inputs=''",
        "",
    )

    # --- --help text ---
    # Use _sh_quote for ALL user-facing strings to safely embed backticks,
    # $-signs and other special chars that appear in action descriptions.
    def _echo(text: str) -> str:
        return f"  echo {_sh_quote(text)}"

    w("_usage() {")
    w(_echo(f"Usage: {bname} [key=value ...] [OPTIONS]"))
    w(_echo(""))
    w(_echo(f"Run GitHub Action: {action_name}"))
    if description:
        for desc_line in description.splitlines()[:3]:
            w(_echo(desc_line))
    w(_echo(""), _echo("Inputs  (key=value positional args or INPUT_KEY env vars):"))
    for inp_name, inp_def in inputs_def.items():
        default = inp_def.get("default")
        desc    = (inp_def.get("description") or "").strip().splitlines()[0][:68] if inp_def.get("description") else ""
        req     = bool(inp_def.get("required"))
        suffix  = ""
        if default:
            suffix = f"  [default: {default}]"
        elif req:
            suffix = "  (required)"
        w(_echo(f"  {inp_name}={suffix}"))
        if desc:
            w(_echo(f"      {desc}"))
    w(
        _echo(""),
        _echo("Options:"),
        _echo("  --inputs-json JSON   All inputs as a JSON object string"),
        _echo("  --inputs-file FILE   YAML or JSON file with inputs (YAML requires yq)"),
        _echo("  --post               Run the post/cleanup step instead of main"),
        _echo("  --workspace DIR      Set GITHUB_WORKSPACE (default: cwd)"),
        _echo("  --env KEY=VALUE      Extra environment variable (repeatable)"),
        "}",
        "",
    )

    # --- Argument parsing ---
    # key=value positional args: map directly to INPUT_KEY env vars.
    # bash 4+ ${var^^} and ${var//-/_} are available in conda's bash (5.x).
    w(
        "while [[ $# -gt 0 ]]; do",
        '  case "$1" in',
        "    --post)         _post=1; shift ;;",
        '    --workspace)    _workspace="$2"; shift 2 ;;',
        '    --env)          _extra_env+=("$2"); shift 2 ;;',
        '    --inputs-json)  _json_inputs="$2"; shift 2 ;;',
        '    --inputs-file)  _file_inputs="$2"; shift 2 ;;',
        "    --help|-h)      _usage; exit 0 ;;",
        "    *=*)",
        "      # key=value → INPUT_KEY=value  (bash 4+: ^^ = uppercase, //- = replace -)",
        '      _k="${1%%=*}"; _k="${_k^^}"; _k="${_k//-/_}"',
        '      export "INPUT_$_k=${1#*=}"',
        "      shift ;;",
        '    *) echo "Unknown argument: $1" >&2; _usage >&2; exit 1 ;;',
        "  esac",
        "done",
        "",
    )

    # --- JSON / file bulk inputs ---
    _json_node_snippet = """\
      const inp = JSON.parse(process.env._JSON_INPUT);
      for (const [k, v] of Object.entries(inp)) {
        const key = 'INPUT_' + k.toUpperCase().replace(/-/g, '_');
        process.stdout.write(key + '=' + String(v) + '\\0');
      }"""

    _json_file_node_snippet = """\
        const inp = JSON.parse(require('fs').readFileSync(process.env._JSON_FILE,'utf8'));
        for (const [k,v] of Object.entries(inp)) {
          const key = 'INPUT_' + k.toUpperCase().replace(/-/g,'_');
          process.stdout.write(key + '=' + String(v) + '\\0');
        }"""

    w(
        "# Apply --inputs-json using node (no jq/python needed)",
        "if [[ -n \"${_json_inputs:-}\" ]]; then",
        "  while IFS= read -r -d $'\\0' _pair; do",
        '    export "${_pair%%=*}=${_pair#*=}"',
        "  done < <(",
        '    _JSON_INPUT="$_json_inputs" node -e "',
        _json_node_snippet,
        '    "',
        "  )",
        "fi",
        "",
        "# Apply --inputs-file: try JSON via node first, then YAML via yq",
        "if [[ -n \"${_file_inputs:-}\" ]]; then",
        "  if node -e \"JSON.parse(require('fs').readFileSync(process.argv[1],'utf8'))\" \"$_file_inputs\" 2>/dev/null; then",
        "    while IFS= read -r -d $'\\0' _pair; do",
        '      export "${_pair%%=*}=${_pair#*=}"',
        "    done < <(",
        '      _JSON_FILE="$_file_inputs" node -e "',
        _json_file_node_snippet,
        '      "',
        "    )",
        "  elif command -v yq &>/dev/null; then",
        "    while IFS= read -r -d $'\\0' _pair; do",
        '      export "${_pair%%=*}=${_pair#*=}"',
        "    done < <(",
        '      _JSON_INPUT="$(yq -o json . "$_file_inputs")" node -e "',
        _json_node_snippet,
        '      "',
        "    )",
        "  else",
        '    echo "ERROR: --inputs-file with YAML requires yq (conda install yq)" >&2',
        "    exit 1",
        "  fi",
        "fi",
        "",
    )

    # --- Extra env ---
    w(
        'for _kv in "${_extra_env[@]+"${_extra_env[@]}"}"; do',
        '  export "$_kv"',
        "done",
        "",
    )

    # --- Baked-in literal defaults (skip expression defaults) ---
    literal_defaults = [
        (inp_name, str(inp_def["default"]))
        for inp_name, inp_def in inputs_def.items()
        if inp_def.get("default") is not None
        and not _is_expression_default(inp_def["default"])
    ]
    if literal_defaults:
        w("# Apply baked-in literal defaults for inputs not yet set")
        for inp_name, default in literal_defaults:
            env_key = "INPUT_" + inp_name.upper().replace("-", "_")
            w(f": \"${{{env_key}:={_sh_quote(default)}}}\"")
        w("")

    # --- Required inputs check ---
    required = [
        name for name, defn in inputs_def.items()
        if defn.get("required") and not _is_expression_default(defn.get("default"))
        and not defn.get("default")
    ]
    if required:
        w("# Check required inputs")
        for inp_name in required:
            env_key = "INPUT_" + inp_name.upper().replace("-", "_")
            w(
                f'if [[ -z "${{{env_key}:-}}" ]]; then',
                f'  echo "ERROR: required input \'{inp_name}\' is not set.  Run with --help." >&2',
                "  exit 1",
                "fi",
            )
        w("")

    # --- GITHUB_* sinks ---
    w(
        "# Set up GITHUB_* environment file sinks",
        '_gh_output="$(mktemp)"',
        '_gh_env="$(mktemp)"',
        '_gh_path="$(mktemp)"',
        "# shellcheck disable=SC2064",
        'trap \'rm -f "$_gh_output" "$_gh_env" "$_gh_path"\' EXIT',
        "",
        'export GITHUB_WORKSPACE="$_workspace"',
        'export GITHUB_ACTION="$_REPO"',
        'export GITHUB_ACTION_PATH="$ACTION_DIR"',
        'export GITHUB_OUTPUT="$_gh_output"',
        'export GITHUB_ENV="$_gh_env"',
        'export GITHUB_PATH="$_gh_path"',
        "export GITHUB_STEP_SUMMARY=/dev/null",
        # Provide defaults for runner env vars expected by @actions/toolkit.
        # The real GitHub runner sets these; outside it users can override via --env.
        'export RUNNER_TEMP="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"',
        'export RUNNER_TOOL_CACHE="${RUNNER_TOOL_CACHE:-${RUNNER_TEMP}/tool-cache}"',
        'mkdir -p "$RUNNER_TEMP" "$RUNNER_TOOL_CACHE"',
        'export PATH="$_PREFIX/bin:$PATH"',
        "",
    )

    # --- Resolve script path ---
    w(
        "# Choose main or post script",
        'if [[ "$_post" -eq 1 ]]; then',
        '  if [[ -z "$_POST_SCRIPT" ]]; then',
        '    echo "No post step defined for $_OWNER/$_REPO — nothing to do." >&2',
        "    exit 0",
        "  fi",
        '  _script="$ACTION_DIR/$_POST_SCRIPT"',
        "else",
        '  _script="$ACTION_DIR/$_MAIN_SCRIPT"',
        "fi",
        'if [[ ! -f "$_script" ]]; then',
        '  echo "ERROR: script not found: $_script" >&2',
        "  exit 1",
        "fi",
        "",
    )

    # --- Run ---
    w(
        "# Run the action",
        'node "$_script"',
        "_exit=$?",
        "",
        "# Print any outputs written to GITHUB_OUTPUT",
        'if [[ -s "$_gh_output" ]]; then',
        "  printf '\\n--- Action outputs ---\\n' >&2",
        '  cat "$_gh_output" >&2',
        "fi",
        "",
        "exit $_exit",
    )

    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# PowerShell entry point generator (Windows)
# ---------------------------------------------------------------------------

def render_powershell_entry_point(owner: str, repo: str, meta: dict[str, Any]) -> str:
    """
    Generate a PowerShell entry point for Windows with declared inputs as named params.
    Installed as $PREFIX/Scripts/<repo>.ps1; a .bat shim calls it.
    """
    inputs_def: dict[str, Any] = meta.get("inputs", {})
    runs:       dict[str, Any] = meta.get("runs", {})
    bname        = bin_name(repo)
    action_name  = meta.get("name", f"{owner}/{repo}")
    description  = (meta.get("description") or "").strip()
    main_script  = runs.get("main", "dist/index.js")
    post_script  = runs.get("post") or ""

    # Generate param() block
    params: list[str] = []
    for inp_name, inp_def in inputs_def.items():
        param_name = inp_name.replace("-", "_")
        default    = inp_def.get("default")
        default_ps = f'"{default}"' if default is not None else '""'
        params.append(f"    [string]${param_name} = {default_ps}")

    params += [
        "    [string]$InputsJson = ''",
        "    [string]$InputsFile = ''",
        "    [switch]$Post",
        "    [string]$Workspace = $(if ($env:GITHUB_WORKSPACE) { $env:GITHUB_WORKSPACE } else { (Get-Location).Path })",
        "    [string[]]$Env = @()",
    ]

    param_block = ",\n".join(params)

    # Generate INPUT_* export lines for individual params
    input_exports: list[str] = []
    for inp_name in inputs_def:
        param_name = inp_name.replace("-", "_")
        env_key    = "INPUT_" + inp_name.upper().replace("-", "_")
        input_exports.append(f'$env:{env_key} = ${param_name}')

    # Required checks
    required_checks: list[str] = []
    for inp_name, inp_def in inputs_def.items():
        if inp_def.get("required") and not inp_def.get("default"):
            env_key = "INPUT_" + inp_name.upper().replace("-", "_")
            required_checks.append(
                f'if (-not $env:{env_key}) {{ Write-Error "Required input \'{inp_name}\' is not set."; exit 1 }}'
            )

    input_exports_str = "\n".join(input_exports)
    required_checks_str = "\n".join(required_checks) if required_checks else ""

    # Help synopsis lines
    synopsis_lines = "\n".join(
        f".PARAMETER {inp_name.replace('-', '_')}\n    {(inp_def.get('description') or '').strip().splitlines()[0][:80] if inp_def.get('description') else ''}"
        for inp_name, inp_def in inputs_def.items()
    )

    return f"""\
<#
.SYNOPSIS
    CLI entry point for {owner}/{repo}
    Generated by generate_recipe.py — do not edit by hand.
.DESCRIPTION
    {description[:120] if description else action_name}
{synopsis_lines}
.PARAMETER InputsJson
    All inputs as a JSON object string, e.g. '{{"token": "ghp_xxx"}}'.
.PARAMETER InputsFile
    Path to a YAML or JSON file mapping input names to values.
.PARAMETER Post
    Run the post/cleanup step instead of the main step.
.PARAMETER Workspace
    GITHUB_WORKSPACE directory (default: current directory).
.PARAMETER Env
    Extra KEY=VALUE environment variables (array).
#>
param(
{param_block}
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---- baked-in at generate time ----
$_owner      = {repr(owner)}
$_repo       = {repr(repo)}
$_mainScript = {repr(main_script)}
$_postScript = {repr(post_script)}

$_scriptDir  = Split-Path $MyInvocation.MyCommand.Path -Parent
$_prefix     = Split-Path $_scriptDir -Parent
$actionDir   = Join-Path $_prefix "share\\gh-actions\\$_owner\\$_repo"

# ---- apply individual input flags ----
{input_exports_str}

# ---- apply --inputs-json via node ----
if ($InputsJson) {{
    $pairs = & node -e @"
const inp = JSON.parse(process.env._JSON_INPUT);
for (const [k, v] of Object.entries(inp)) {{
    const key = 'INPUT_' + k.toUpperCase().replace(/-/g, '_');
    process.stdout.write(key + '=' + String(v) + '\0');
}}
"@ --env "_JSON_INPUT=$InputsJson" 2>$null
    # Parse NUL-delimited pairs
    $pairs -split '\0' | Where-Object {{ $_ -match '=' }} | ForEach-Object {{
        $k, $v = $_ -split '=', 2
        [System.Environment]::SetEnvironmentVariable($k, $v)
    }}
}}

# ---- apply --inputs-file ----
if ($InputsFile) {{
    $raw = Get-Content $InputsFile -Raw
    try {{
        $obj = $raw | ConvertFrom-Json
    }} catch {{
        Write-Error "--inputs-file: only JSON files are natively supported on Windows (install yq for YAML)"
        exit 1
    }}
    $obj.PSObject.Properties | ForEach-Object {{
        $k = 'INPUT_' + $_.Name.ToUpper().Replace('-','_')
        [System.Environment]::SetEnvironmentVariable($k, [string]$_.Value)
    }}
}}

# ---- extra env vars ----
$Env | ForEach-Object {{
    $k, $v = $_ -split '=', 2
    [System.Environment]::SetEnvironmentVariable($k, $v)
}}

{required_checks_str}

# ---- GITHUB_* sinks ----
$ghOutput = [System.IO.Path]::GetTempFileName()
$ghEnv    = [System.IO.Path]::GetTempFileName()
$ghPath   = [System.IO.Path]::GetTempFileName()

$env:GITHUB_WORKSPACE    = $Workspace
$env:GITHUB_ACTION       = $_repo
$env:GITHUB_ACTION_PATH  = $actionDir
$env:GITHUB_OUTPUT       = $ghOutput
$env:GITHUB_ENV          = $ghEnv
$env:GITHUB_PATH         = $ghPath
$env:GITHUB_STEP_SUMMARY = [System.IO.Path]::GetTempFileName()
$env:PATH                = (Join-Path $_prefix "bin") + [System.IO.Path]::PathSeparator + $env:PATH

# ---- choose script ----
if ($Post) {{
    if (-not $_postScript) {{
        Write-Host "No post step defined for $_owner/$_repo — nothing to do." -ForegroundColor Yellow
        exit 0
    }}
    $script = Join-Path $actionDir $_postScript
}} else {{
    $script = Join-Path $actionDir $_mainScript
}}
if (-not (Test-Path $script)) {{
    Write-Error "Script not found: $script"
    exit 1
}}

# ---- run ----
try {{
    & node $script
    $exitCode = $LASTEXITCODE
}} finally {{
    if ((Test-Path $ghOutput) -and (Get-Item $ghOutput).Length -gt 0) {{
        Write-Host "`n--- Action outputs ---" -ForegroundColor Cyan
        Get-Content $ghOutput | Write-Host -ForegroundColor Cyan
    }}
    Remove-Item $ghOutput, $ghEnv, $ghPath -ErrorAction SilentlyContinue
}}

exit $exitCode
"""


def render_bat_shim(repo: str) -> str:
    """A minimal .bat shim that delegates to the .ps1 entry point."""
    bname = bin_name(repo)
    return (
        "@echo off\n"
        f'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0{bname}.ps1" %*\n'
    )


# ---------------------------------------------------------------------------
# Build scripts  (build.sh / bld.bat)
# ---------------------------------------------------------------------------

def render_build_sh(owner: str, repo: str, using: str) -> str:
    bname  = bin_name(repo)
    install_dir = f"$PREFIX/share/gh-actions/{owner}/{repo}"
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        "",
        "# Install action source files",
        f'mkdir -p "{install_dir}"',
        f'cp -r "$SRC_DIR/." "{install_dir}/"',
        "",
        "# Install bash entry point",
        'mkdir -p "$PREFIX/bin"',
        f'install -m 755 "$RECIPE_DIR/{bname}.sh" "$PREFIX/bin/{bname}"',
    ]
    node_v = node_version(using)
    if node_v is not None:
        lines += [
            "",
            "# Sanity-check the main entry point exists",
            f'if [[ ! -f "{install_dir}/dist/index.js" ]]; then',
            f'  echo "WARNING: {install_dir}/dist/index.js not found" >&2',
            "fi",
        ]
    return "\n".join(lines) + "\n"


def render_bld_bat(owner: str, repo: str) -> str:
    bname       = bin_name(repo)
    install_dir = f"%PREFIX%\\share\\gh-actions\\{owner}\\{repo}"
    return "\n".join([
        "@echo off",
        f'if not exist "{install_dir}" mkdir "{install_dir}"',
        f'xcopy /E /I /Y "%SRC_DIR%" "{install_dir}"',
        "",
        f'if not exist "%PREFIX%\\Scripts" mkdir "%PREFIX%\\Scripts"',
        f'copy "%RECIPE_DIR%\\{bname}.ps1" "%PREFIX%\\Scripts\\{bname}.ps1"',
        f'copy "%RECIPE_DIR%\\{bname}.bat" "%PREFIX%\\Scripts\\{bname}.bat"',
    ]) + "\n"


# ---------------------------------------------------------------------------
# recipe.yaml
# ---------------------------------------------------------------------------

def render_recipe(
    owner: str,
    repo: str,
    ref: str,
    version: str,
    sha256: str,
    meta: dict[str, Any],
) -> str:
    pkg         = package_name(owner, repo)
    using       = action_type(meta)
    node_v      = node_version(using)
    bname       = bin_name(repo)
    summary     = meta.get("name", f"{owner}/{repo}")
    description = meta.get("description", f"GitHub Action {owner}/{repo}")
    desc_indent = textwrap.indent(description.strip(), "    ")
    tarball_url = GITHUB_ARCHIVE.format(owner=owner, repo=repo, ref=ref)

    run_deps = []
    if node_v is not None:
        run_deps.append(f"    - nodejs >={node_v}")

    run_deps_yaml = "\n".join(run_deps) if run_deps else "    []"

    return f"""\
context:
  owner: {owner}
  repo: {repo}
  version: "{version}"

package:
  name: {pkg}
  version: ${{{{ version }}}}

source:
  url: {tarball_url}
  sha256: {sha256}

build:
  number: 0
  # build.sh / bld.bat live in the same directory as this recipe.yaml
  script: build.sh

requirements:
  # No Python or PyYAML — entry points are shell scripts that invoke node directly.
  run:
{run_deps_yaml}

tests:
  - package_contents:
      files:
        - share/gh-actions/{owner}/{repo}/action.yml
  - script:
      - {bname} --help

about:
  homepage: https://github.com/{owner}/{repo}
  license: MIT
  summary: "GitHub Action: {summary}"
  description: |
{desc_indent}
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_action_ref(ref: str) -> tuple[str, str, str]:
    """Parse 'owner/repo@ref' into (owner, repo, ref)."""
    if "@" not in ref:
        raise ValueError(f"Expected owner/repo@ref, got: {ref!r}")
    repo_part, git_ref = ref.split("@", 1)
    parts = repo_part.split("/")
    if len(parts) != 2:
        raise ValueError(f"Expected owner/repo, got: {repo_part!r}")
    return parts[0], parts[1], git_ref


def generate_recipe(
    action_ref: str,
    output_dir: Path,
    dry_run: bool = False,
) -> Path:
    owner, repo, ref = parse_action_ref(action_ref)
    version = normalize_version(ref)
    pkg     = package_name(owner, repo)
    bname   = bin_name(repo)

    print(f"Generating recipe for {owner}/{repo}@{ref} → {pkg} {version}")

    print("  Fetching action.yml from GitHub...", flush=True)
    meta  = fetch_action_meta(owner, repo, ref)
    using = action_type(meta)
    print(f"  Action type: {using}")

    if using == "docker":
        print("  WARNING: Docker actions are not fully supported for local execution.")

    tarball_url = GITHUB_ARCHIVE.format(owner=owner, repo=repo, ref=ref)
    sha256      = sha256_of_url(tarball_url)
    print(f"  sha256: {sha256}")

    files: dict[str, str] = {
        "recipe.yaml":    render_recipe(owner, repo, ref, version, sha256, meta),
        "build.sh":       render_build_sh(owner, repo, using),
        "bld.bat":        render_bld_bat(owner, repo),
        f"{bname}.sh":    render_bash_entry_point(owner, repo, meta),
        f"{bname}.ps1":   render_powershell_entry_point(owner, repo, meta),
        f"{bname}.bat":   render_bat_shim(repo),
    }

    recipe_dir  = output_dir / pkg
    recipe_path = recipe_dir / "recipe.yaml"

    if dry_run:
        for fname, content in files.items():
            print(f"\n{'='*60}")
            print(f"--- {fname} ---")
            print(content)
    else:
        recipe_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in files.items():
            (recipe_dir / fname).write_text(content)
        print(f"  Written to {recipe_dir}/")
        for fname in files:
            print(f"    {fname}")

    return recipe_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate rattler-build recipe + shell entry points for a GitHub Action.\n"
            "No Python or PyYAML is included in the generated conda package; "
            "entry points are bash (Unix) / PowerShell (Windows) scripts that\n"
            "invoke node directly."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "action_ref",
        help="Action reference, e.g. actions/checkout@v4.2.2",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("recipes"),
        help="Directory to write recipe files into (default: ./recipes)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print all generated files to stdout instead of writing them",
    )
    args = parser.parse_args()

    generate_recipe(args.action_ref, args.output_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
