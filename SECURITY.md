# Security Policy

## Supported versions

Until the first tagged release, security fixes are made on `main`. After releases begin, only the
latest release line and `main` are supported.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/paullizer/WindowsSandboxMCPServer/security/advisories/new).
Do not open a public issue for an unpatched vulnerability. If private reporting is unavailable,
open an issue containing only a request for private contact and no vulnerability details.

Include the affected Windows and Python versions, the smallest reproduction you can provide, the
security impact, and whether the issue crosses the host/guest boundary.

Security-sensitive areas include:

- input reaching any window other than Windows Sandbox;
- access outside the per-session host control folder;
- unsafe command or storyboard value handling;
- credentials appearing in tool output or recordings;
- downloaded payloads executing without integrity or publisher verification.

## Trust model

Windows Sandbox is the isolation boundary. The read-only payload folder and the per-session control
folder are deliberately mapped from the host. Code run in the guest can write to that control
folder, and networking exposes the guest to the same network available to the host. Do not map
broad host directories or place credentials in storyboard values.
