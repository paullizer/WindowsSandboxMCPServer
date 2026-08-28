# Contributing

## Setup

Use Windows 11 Pro, Enterprise or Education with Python 3.10 or newer:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## Host-only checks

These checks do not launch Windows Sandbox and are the same checks run in CI:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -W error tests\check_config.py
.\.venv\Scripts\python.exe -W error tests\check_storyboard.py
.\.venv\Scripts\python.exe -W error tests\check_prompter.py
```

Parse every changed PowerShell file with
`[System.Management.Automation.Language.Parser]::ParseFile(...)` before submitting it.

## Sandbox checks

The `tests/smoke_*.py` scripts create or drive a real Windows Sandbox. Run only the smallest smoke
test that covers your change. These tests take focus and inject keyboard or mouse input, so turn on
Do Not Disturb and do not use the machine while they run. Recording tests also require the separate
[Snagit MCP server](https://github.com/paullizer/LocalSnagitMCPServer).

## Pull requests

- Keep host-to-guest mappings narrow and preserve the read-only payload boundary.
- Put reusable guest work in `payload/` and orchestration in `storyboards/`.
- Put account- or deployment-specific values in ignored `storyboards/values.json`, never in YAML.
- Pin external application source revisions and verify downloaded artifacts.
- Update tests and documentation for changed behavior.

Report vulnerabilities according to [SECURITY.md](SECURITY.md), not in a public issue.
