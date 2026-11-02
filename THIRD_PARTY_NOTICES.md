# Third-party notices

EMNETS-RA builds on [Claw-AI-Lab](https://github.com/Claw-AI-Lab/Claw-AI-Lab). Upstream notices and individual source licenses remain applicable. This publication does not replace third-party licenses.

## Vendored research skills

| Source | Pinned revision | Declared license | Skills |
| --- | --- | --- | ---: |
| [scientific](https://github.com/K-Dense-AI/scientific-agent-skills) | `5f2bc0341354fd5590aef76a533ada1e1e5b4e62` | MIT | 177 |
| [autoscirub](https://github.com/zjunlp/AutoSciRub) | `2c6c58392ee6399e08f2526e35176f0e2c716e16` | MIT | 8 |
| [anthropic](https://github.com/anthropics/skills) | `dbd4588f9e1033efb41dad4bef2f7947c8993d44` | Apache-2.0 | 8 |
| [superpowers](https://github.com/obra/superpowers) | `2ce8aa15d05bd57213e6f746f953aafb7ded7a8b` | MIT | 7 |
| [trailofbits](https://github.com/trailofbits/skills) | `bf23bfb21f10a2a4b0406086d690dc8cb1b3ccf3` | CC-BY-SA-4.0 | 10 |

Exact source metadata and file hashes are in `backend/agent/researchclaw/skills/sources.lock.json`. License files and any skill-specific notices remain alongside the vendored resources; those individual notices are authoritative.

## Web dependencies and design references

The Web app uses React, React Router, Vite, Lucide, html2canvas, jsPDF and Playwright; dependency versions are pinned by the npm lockfile and their licenses remain with the packages. Ant Design Pro, Shadcn Admin and TailAdmin React informed the interface organization; their complete frameworks were not copied into this project. See `docs/web-workspace.md` for source links.

## Deployment tools

The optional production setup uses [Caddy](https://github.com/caddyserver/caddy), [cloudflared](https://github.com/cloudflare/cloudflared), and [Supervisor](https://github.com/Supervisor/supervisor). They are installed separately; their respective upstream licenses apply. The gateway and tunnel installer pins release versions and validates the published SHA-256 digests.
