# Third-Party Licenses & Software Inventory

**Project:** `ticket-master`  
**License:** [MIT License](LICENSE)  
**Audit Date:** 2026-09-18 (Prior audit: 2026-09-16)

---

## Runtime Architecture & Dependencies

`ticket-master` is engineered as a **100% local-first, zero-egress, zero-external-dependency** cross-platform task routing and AI coding agent triage engine.

### Zero-Runtime-Dependency Guarantee

| Package | Version Spec | License | Type | Purpose |
|---------|-------------|---------|------|---------|
| *None* | `n/a` | `n/a` | External | `ticket-master` has **0** mandatory external runtime dependencies (`dependencies = []` in `pyproject.toml`). |
| *Python Standard Library* | `>=3.10` | PSF License | Built-in | `argparse`, `dataclasses`, `datetime`, `hashlib`, `json`, `os`, `pathlib`, `re`, `shutil`, `subprocess`, `sys`, `typing`, `uuid` |

All network egress, remote telemetry, and external cloud services are prohibited by design.

### Zero-Copyleft Guarantee & Unprivileged Execution

- **Zero-Copyleft Guarantee:** All runtime code and development tooling are governed exclusively by permissive open-source licenses (MIT, Apache-2.0, PSF). The repository contains zero GPL, AGPL, or viral copyleft dependencies.
- **Unprivileged User-Mode Operation (`RunAsInvoker`):** All components execute strictly within standard user space without requiring administrator elevation, root privileges, or UAC elevation.

---

## Governance & Runtime Invariants

The software adheres to ten foundational governance and runtime invariants:

| Invariant | Category | Description |
|---|---|---|
| `INV-LOCAL-01` | Local-First & Zero Egress | All tickets, lifecycles, and logs remain strictly on local disk; zero cloud telemetry. |
| `INV-WORKFLOW-02` | Agent-Native Workflow | Transparent prompt-driven Position 0 triage mode; zero opaque background daemons. |
| `INV-ROUTING-03` | 5-Dimension Routing | Deterministic scoring (Clarity, Complexity, Creativity, Context, Criticality) with fallback. |
| `INV-CLAIM-04` | Multi-System Claims | Atomic filesystem renames (`.claim-<host>-<ts>`) without external databases or locks. |
| `INV-COMPANION-05` | Companion Pattern | Domain-grouped tasks reuse companion sub-agents, amortizing orientation cost. |
| `INV-INTAKE-06` | Informal Intake | Raw text notes in `INBOX/` formalized with byte-identical `ORIGINALTEXT` preservation. |
| `INV-AUDITOR-07` | Auditor Bridge | Seamless `system-auditor` bridge with fail-closed sparmodus token-budget gating. |
| `INV-UNPRIV-08` | Unprivileged Execution | Standard user-mode execution (`RunAsInvoker`); zero UAC or root privileges required. |
| `INV-PORTABLE-09` | Zero Dependencies | Pure Python standard library (`dependencies = []`); extras cleanly isolated. |
| `INV-SLA-10` | Transparent SLA | Permissive MIT license, automated multi-OS CI matrix, and committed 48h security SLA. |

---

## Optional Runtime Dependencies

`ticket-master` defines optional extensions for advanced routing protocols:

| Package | Version Spec | License | Scope | Purpose |
|---------|-------------|---------|-------|---------|
| [clutch-router](https://github.com/ellmos-ai/clutch) | `>=0.5,<0.6` | MIT | `[routing-v2]` | Optional Clutch v2 integration and intent matching |

---

## Development & Test Dependencies

The following tools and libraries are utilized exclusively during development, linting, packaging, and automated contract test execution:

| Package / Tool | Version Spec | License | Scope | Purpose |
|----------------|-------------|---------|-------|---------|
| [pytest](https://pytest.org/) | `>=8.0` | MIT | `[dev]` | Automated unit, contract, and regression test runner |
| [ruff](https://github.com/astral-sh/ruff) | `>=0.6` | MIT OR Apache-2.0 | `[dev]` | High-performance Python linter and code formatting validation |
| [setuptools](https://github.com/pypa/setuptools) | `>=77.0` | MIT | `[build-system]` | Standard Python packaging and build backend |
| [build](https://github.com/pypa/build) | `>=1.2` | MIT | `[dev]` | PEP 517 build frontend and package artifact verification |

---

## License Texts & Attribution

### MIT License (`ticket-master`, `clutch-router`, `pytest`, `setuptools`, `build`, `ruff`)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

### Apache License 2.0 (`ruff` dual-license option)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

### Python Software Foundation License (PSF) (Python Standard Library)

1. This LICENSE AGREEMENT is between the Python Software Foundation ("PSF"), and
   the Individual or Organization ("Licensee") accessing and otherwise using Python
   software in source or binary form and its associated documentation.

2. Subject to the terms and conditions of this License Agreement, PSF hereby
   grants Licensee a nonexclusive, royalty-free, world-wide license to reproduce,
   analyze, test, perform and/or display publicly, prepare derivative works, distribute,
   and otherwise use Python alone or in any derivative version, provided, however, that
   PSF's License Agreement and PSF's notice of copyright, i.e., "Copyright (c) 2001-2026
   Python Software Foundation; All Rights Reserved" are retained in Python alone or
   in any derivative version prepared by Licensee.
