# Third-party notices

The root MIT license covers the project's original code. Bundled upstream
materials retain their own licenses, copyright notices, and conditions.

| Component | Source | License | Included license / provenance |
| --- | --- | --- | --- |
| Data-analysis skill references | [anthropics/knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins) | Apache-2.0 | `vendor/skills/anthropic-data/LICENSE`, `skills/manifest.json` |
| frontend-design skill | [anthropics/skills](https://github.com/anthropics/skills) | Apache-2.0 | `skills/design/frontend-design/LICENSE.txt`, `skills/design-manifest.json` |
| UI/UX Pro Max skill | [nextlevelbuilder/ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill) | MIT | `skills/design/ui-ux-pro-max/LICENSE`, `skills/design-manifest.json` |
| Token Optimization skill | [bm629/agent-skills](https://github.com/bm629/agent-skills) | MIT | `vendor/skills/bm629/token-optimization/LICENSE`, `skills/token-optimization-manifest.json` |

Project-specific role adapters live in `skills/main`, `skills/analyst`, and
`skills/report_designer`. Upstream originals and their fixed revisions/hashes
are recorded separately; local adapters describe changes to tool names,
source authorization, evidence binding, and execution constraints.

The CSV files under the UI/UX Pro Max skill are upstream design reference
catalogs and upstream test fixtures. They are not uploaded business datasets
or exported analysis results. User datasets, knowledge-base files, reports,
and runtime history are excluded from this repository.

Python dependencies are installed separately; consult each package's license.
The Docker image installs Debian Noto CJK fonts, whose license notices remain
in the installed system package. The repository does not bundle model weights
or grant any rights to external datasets or model services.
