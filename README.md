# quse

Quota and usage checks for coding-agent CLIs.

`quse` reports normalized usage for providers used by tools such as Codex,
Claude Code, GitHub Copilot, Grok Build, Z.AI, and OpenCode Go.

```bash
quse
quse codex
quse copilot --json
```

The CLI prints one normalized line per provider by default. `--json` emits the
same normalized records as JSON keyed by provider name:

```json
{
  "claude": {
    "details": {},
    "error": null,
    "status": "ok",
    "windows": {
      "5h": {
        "percent_remaining": 55.0,
        "reset_at": "2026-05-24T14:30:00Z",
        "rolling": false
      },
      "7d": {
        "percent_remaining": 87.0,
        "reset_at": "2026-05-28T14:59:59Z"
      },
      "monthly": {
        "percent_remaining": null,
        "reset_at": null
      }
    }
  }
}
```

Supported providers:

- `codex`
- `claude`
- `zai`
- `copilot`
- `grok` (alias: `grok-build`)
- `go`

`gemini` is accepted and reports `unsupported` because it does not currently
expose a usage endpoint.

Provider mapping:

- Every provider has the same normalized `windows.5h`, `windows.7d`, and
  `windows.monthly` records. An unavailable window has `null` values. The
  `rolling` field is present on `5h`; if a provider does not identify the
  window as rolling, it is `false`.
- `codex`: API windows map to `5h` and `7d`; when the API returns one window,
  it is `7d` only.
  Codex JSON output also includes `details.banked_resets` from ChatGPT's
  rate-limit reset-credit endpoint when available.
- `claude`: the API's short and long signals map to `5h` and `7d`.
  `details.banked_resets` lists one-time limit-reset grants (the "Resets"
  shown under Settings > Usage) from the usage endpoint's `cedar_ember` read,
  with the same unified shape as other providers; quse only lists them and
  never redeems them.
- `copilot`: the monthly premium-interactions signal maps to `monthly`.
- `zai`: the five-hour quota maps to rolling `5h` and the token quota maps to
  `7d`; its `monthly` window is unavailable. `details.banked_resets` lists
  quota reset cards from `customer-package-reset/list` (personal plan,
  read-only) with the same unified shape as other providers.
- `go`: OpenCode Go's rolling and weekly API windows map to `5h` and `7d`, and
  its monthly API window maps to `monthly`.
- `grok`: the weekly SuperGrok / X Premium window and the monthly credit
  window map to `7d` and `monthly` when both are present. When the API returns
  one window, it is assigned to `7d`. Weekly remaining
  prefers the `GrokBuild` entry in `productUsage`, then `creditUsagePercent`.
  Grok's proto3 billing payload omits a 0% used figure, so a weekly period
  with no reported percent is treated as 100% remaining. Grok JSON output
  includes `details.product_usage`. When
  Codex, Claude, or Grok expose one-time usage resets, `details.banked_resets`
  lists them with one unified shape (`expires_at`, `available`, `label`) for
  all providers and both human and JSON output render them as `banked_resets`.
  The one-time reset RPC is separate from Grok's CLI billing responses. `quse`
  queries it with the stored OAuth token and uses the local `curl` command for
  the `grok.com` request because Cloudflare can challenge Python's TLS client.
  If a deployment requires the browser session, set `GROK_RESET_COOKIE` to the
  full `Cookie` header from the logged-in `grok.com` browser session and, when
  needed, set `GROK_RESET_USER_AGENT` to that browser's user agent. Alternatively,
  point `GROK_RESET_COOKIE_FILE` at a local Cookie-Editor JSON export;
  `~/.grok/cookies.json` and `~/.grok/browser-cookies.json` are checked
  automatically. A browser's Network-panel **Copy as cURL** export can be
  saved via `GROK_RESET_CURL_FILE`; `~/.grok/reset.curl` and
  `~/.grok/grok-reset.curl` are also checked automatically, including the
  matching browser user agent. If browser cookies are stored in a `cookies`
  field in `~/.grok/auth.json`, `quse` uses those too. Do not commit or share
  these values.

Z.AI credentials are resolved in this order:

1. The OpenCode auth file at `~/.local/share/opencode/auth.json`, using the
   `zai-coding-plan` entry.
2. The legacy goz config at `~/.config/goz/config.json`, using `zai_token`.

OpenCode Go uses the same OpenCode auth file and the `opencode-go` entry. Run
`/connect` in OpenCode, choose `OpenCode Go`, and paste the key from
`opencode.ai/auth`; Quse will reuse that stored key. For a separately managed
key, set `OPENCODE_GO_API_KEY`. Do not commit or share these values.

## Install

For a local checkout, install the project environment and add its `.venv/bin`
to your shell PATH:

```bash
uv sync --dev
./install.sh
```

Open a new shell after running `./install.sh`, then verify:

```bash
quse --help
```

This is the same local-checkout style used by `tmuxctl`: the script appends
the checkout's `.venv/bin` directory to `~/.bashrc`.

For one-off use from a checkout without changing PATH:

```bash
uv run quse
uv run quse codex --json
```

For the latest released package from PyPI:

```bash
uvx quse
uv tool install quse
# or
pipx install quse
```

Use `uvx quse` for a one-off run without installing a persistent tool.

## Release

Releases are published by GitHub Actions when a tag starting with `v` is pushed:

```bash
git tag v0.0.6
git push origin main --tags
```

The workflow verifies that the tag version matches `pyproject.toml`, runs the
tests, builds the wheel and sdist, then publishes to PyPI with the repository
secret `PYPI_API_TOKEN`.
