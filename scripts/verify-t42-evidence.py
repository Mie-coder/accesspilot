#!/usr/bin/env python3
"""Verify the T42 evidence package against the frozen T41 local revision."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = Path(__file__).resolve()
T41_REVISION = "9e757fd433f10fbff22fba9654c54cc3b4e9bec2"
T41_EVIDENCE = ROOT / "docs/evidence/accesspilot-v1.3-t41-acceptance-2026-08-20.md"
CLAIM_LEDGER = ROOT / "docs/evidence/accesspilot-v1.3-claim-ledger.md"
PRODUCT_MANIFEST = ROOT / "docs/evidence/accesspilot-v1.3-product-manifest.md"
INTERVIEW_PACK = ROOT / "docs/evidence/accesspilot-v1.3-interview-evidence-pack.md"
DOC_INDEX = ROOT / "docs/README.md"
T42_TEXT_ARTIFACTS = (
    CLAIM_LEDGER,
    PRODUCT_MANIFEST,
    INTERVIEW_PACK,
    VERIFIER,
)

REQUIRED_CLAIMS = {
    "CL-LG-01": "LangGraph 真实主链",
    "CL-LG-02": "official PostgreSQL checkpoint",
    "CL-LG-03": "interrupt/resume",
    "CL-LG-04": "grounded pgvector RAG",
    "CL-LG-05": "只读真实轨迹",
    "CL-LG-06": "六个故障恢复",
}

UPSTREAM_OWNED_CHANGES = {
    "docs/specs/accesspilot-langgraph-agent-loop-v1.3.md",
    "docs/tickets/accesspilot-langgraph-agent-loop-v1.3.md",
    "docs/evidence/accesspilot-v1.3-change-ledger.md",
    "docs/evidence/accesspilot-v1.3-t41-acceptance-2026-08-20.md",
}
T42_ALLOWED_CHANGES = {
    "scripts/verify-t42-evidence.py",
    "docs/README.md",
    "docs/evidence/accesspilot-v1.3-claim-ledger.md",
    "docs/evidence/accesspilot-v1.3-product-manifest.md",
    "docs/evidence/accesspilot-v1.3-interview-evidence-pack.md",
}

METRIC_SOURCE_TOKENS = {
    "API": "868 passed, 0 skipped",
    "Web": "109 passed, 0 skipped",
    "parity scenarios": "33 个语义场景",
    "parity rounds": "2/2",
    "fresh scenarios": "15/15",
    "fresh product cases": "101/101",
    "rollback wrapper": "9 passed, 0 skipped",
    "event completeness": "7/7",
    "side-effect steps": "4/4",
    "leak count": "泄漏数 `0`",
    "response-start p50": "p50 `40.0 ms`",
    "response-start p95": "p95 `51.0 ms`",
    "completion p50": "p50 `217.2 ms`",
    "completion p95": "p95 `228.7 ms`",
}

REQUIRED_BOUNDARIES = {
    "Provider at-least-once": ("Provider", "at-least-once"),
    "Mock identity": ("Mock Login",),
    "no real SSO": ("真实 SSO",),
    "no real IAM": ("真实 IAM",),
    "no ReAct": ("ReAct",),
    "no Multi-Agent": ("Multi-Agent",),
    "no production SLA": ("生产 SLA",),
    "no token cost": ("Token 成本",),
    "no observability platform": ("可观测平台",),
    "offline browser": ("deterministic offline browser",),
    "provider latency unmeasured": ("provider_token_latency",),
    "policy recall unmeasured": ("policy_recall_at_k",),
    "production SLA unmeasured": ("production_sla",),
    "strict boundary": ("strict serializer",),
    "shutdown boundary": ("shutdown",),
    "chunk boundary": ("single-chunk",),
}

CANDIDATE_OVERCLAIM_PATTERNS = (
    ("production/online claim", r"生产级|生产环境|已上线|线上部署|大规模"),
    ("real-enterprise claim", r"真实(?:企业|客户|员工|\s*SSO|\s*IAM|\s*IdP)"),
    ("autonomous-agent claim", r"自主\s*(?:ReAct|规划|选择|调用|审批|开通)"),
    ("multi-agent claim", r"Multi[- ]?Agent|多\s*Agent|Supervisor/Worker"),
    ("provider exactly-once claim", r"exactly[- ]once.{0,24}(?:Provider|外部)"),
    ("personal mastery claim", r"本人已|我已|独立完成|主导上线"),
    ("quantified SLA claim", r"SLA\s*(?:达到|达成|保证|[<=>]\s*\d)"),
)

LINK_RE = re.compile(r"!?(?:\[[^\]]*\])\(([^)]+)\)")
CLAIM_HEADING_RE = re.compile(r"^## (CL-LG-\d{2})\b.*$", re.MULTILINE)
ENTRY_RE = re.compile(
    r"^- \[[^\]]+\]\(([^)]+)\) — `([A-Za-z_][A-Za-z0-9_]*)`",
    re.MULTILINE,
)


def _run(*args: str) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.rstrip("\n")


def _read(path: Path, errors: list[str]) -> str:
    if not path.is_file():
        errors.append(f"missing artifact: {path.relative_to(ROOT)}")
        return ""
    return path.read_text(encoding="utf-8")


def _changed_paths() -> set[str]:
    output = _run("git", "status", "--porcelain=v1", "--untracked-files=all")
    changed: set[str] = set()
    for line in output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            old_path, new_path = path.split(" -> ", maxsplit=1)
            changed.update((old_path, new_path))
        else:
            changed.add(path)
    return changed


def _verify_revision(errors: list[str]) -> None:
    head = _run("git", "rev-parse", "HEAD")
    resolved = _run("git", "rev-parse", "9e757fd")
    if head != T41_REVISION:
        errors.append(f"HEAD drifted from frozen T41 revision: {head}")
    if resolved != T41_REVISION:
        errors.append(f"short T41 revision resolves unexpectedly: {resolved}")


def _verify_change_scope(errors: list[str]) -> None:
    changed = _changed_paths()
    allowed = UPSTREAM_OWNED_CHANGES | T42_ALLOWED_CHANGES
    unexpected = sorted(changed - allowed)
    if unexpected:
        errors.append(f"out-of-scope modified paths: {unexpected}")

    formal_resume_pattern = re.compile(
        r"(^|/)(?:resume|cv|简历)(?:/|[._-]|$)", re.IGNORECASE
    )
    formal_resume_paths = sorted(
        path
        for path in changed
        if formal_resume_pattern.search(path)
        or Path(path).suffix.lower() in {".doc", ".docx", ".pdf"}
    )
    if formal_resume_paths:
        errors.append(f"formal resume appears in modified paths: {formal_resume_paths}")


def _split_claim_sections(text: str) -> dict[str, str]:
    matches = list(CLAIM_HEADING_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.start():end]
    return sections


def _field(section: str, name: str) -> str:
    match = re.search(
        rf"^\*\*{re.escape(name)}：\*\*\s*(.*?)(?=^\*\*[^\n]+：\*\*|^## |\Z)",
        section,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _local_target(document: Path, href: str) -> Path | None:
    clean_href = unquote(href.split("#", maxsplit=1)[0])
    if not clean_href or clean_href.startswith(("http://", "https://", "mailto:")):
        return None
    if clean_href.startswith("/"):
        return Path(clean_href)
    return (document.parent / clean_href).resolve()


def _verify_links(document: Path, text: str, errors: list[str]) -> None:
    for raw_href in LINK_RE.findall(text):
        href = raw_href.strip().split(maxsplit=1)[0]
        target = _local_target(document, href)
        if target is None:
            continue
        try:
            target.relative_to(ROOT)
        except ValueError:
            errors.append(
                f"link escapes repository: {document.relative_to(ROOT)} -> {href}"
            )
            continue
        if not target.exists():
            errors.append(
                f"broken link: {document.relative_to(ROOT)} -> {href}"
            )
            continue
        if "#" in href and target.suffix.lower() == ".md":
            fragment = unquote(href.split("#", maxsplit=1)[1])
            target_text = target.read_text(encoding="utf-8")
            explicit_anchor = re.search(
                rf'<a\s+id=["\']{re.escape(fragment)}["\']\s*></a>',
                target_text,
            )
            if explicit_anchor is None:
                errors.append(
                    f"unresolved explicit anchor: "
                    f"{document.relative_to(ROOT)} -> {href}"
                )


def _verify_entry_symbols(
    *,
    document: Path,
    claim_id: str,
    field_name: str,
    field_text: str,
    path_marker: str,
    errors: list[str],
) -> None:
    entries = ENTRY_RE.findall(field_text)
    if not entries:
        errors.append(f"{claim_id} has no structured {field_name} entry")
        return
    if not any(path_marker in href for href, _ in entries):
        errors.append(f"{claim_id} {field_name} does not point to {path_marker}")
    for href, symbol in entries:
        target = _local_target(document, href)
        if target is None or not target.is_file():
            errors.append(f"{claim_id} {field_name} target is missing: {href}")
            continue
        source = target.read_text(encoding="utf-8")
        if not re.search(rf"\b{re.escape(symbol)}\b", source):
            errors.append(
                f"{claim_id} names absent symbol {symbol!r} in {target.relative_to(ROOT)}"
            )


def _verify_claims(text: str, errors: list[str]) -> None:
    sections = _split_claim_sections(text)
    if set(sections) != set(REQUIRED_CLAIMS):
        errors.append(
            "claim IDs mismatch: "
            f"expected {sorted(REQUIRED_CLAIMS)}, got {sorted(sections)}"
        )
    for claim_id, expected_name in REQUIRED_CLAIMS.items():
        section = sections.get(claim_id, "")
        if not section:
            continue
        if expected_name not in section:
            errors.append(f"{claim_id} capability label is missing: {expected_name}")
        if T41_REVISION not in _field(section, "证据 revision"):
            errors.append(f"{claim_id} does not bind the full T41 revision")

        implementation = _field(section, "实现入口")
        tests = _field(section, "测试")
        runtime = _field(section, "T41 运行证据")
        limitation = _field(section, "限制")
        _verify_entry_symbols(
            document=CLAIM_LEDGER,
            claim_id=claim_id,
            field_name="implementation",
            field_text=implementation,
            path_marker="/src/",
            errors=errors,
        )
        _verify_entry_symbols(
            document=CLAIM_LEDGER,
            claim_id=claim_id,
            field_name="test",
            field_text=tests,
            path_marker="test",
            errors=errors,
        )
        if "accesspilot-v1.3-t41-acceptance-2026-08-20.md" not in runtime:
            errors.append(f"{claim_id} does not link the T41 runtime evidence")
        if len(limitation) < 30:
            errors.append(f"{claim_id} limitation is missing or too vague")


def _verify_cl5_direct_ui_evidence(text: str, errors: list[str]) -> None:
    section = _split_claim_sections(text).get("CL-LG-05", "")
    implementation = _field(section, "实现入口")
    tests = _field(section, "测试")
    app_href = "../../apps/web/src/App.tsx"
    app_test_href = "../../apps/web/src/App.test.tsx"
    test_title = (
        "switches tabs with roving keyboard focus and keeps the live "
        "conversation mounted without side effects"
    )
    if f"]({app_href}) — `App`" not in implementation:
        errors.append("CL-LG-05 lacks direct App.tsx implementation evidence")
    if f"]({app_test_href})" not in tests or test_title not in tests:
        errors.append("CL-LG-05 lacks the exact App.test.tsx no-side-effect test")
    app_source = (ROOT / "apps/web/src/App.tsx").read_text(encoding="utf-8")
    app_test_source = (ROOT / "apps/web/src/App.test.tsx").read_text(
        encoding="utf-8"
    )
    if "export function App" not in app_source:
        errors.append("direct App.tsx production symbol is missing")
    if test_title not in app_test_source:
        errors.append("direct App.test.tsx test title is missing")


def _verify_at_least_once_boundaries(
    claim_ledger: str,
    manifest: str,
    interview_pack: str,
    errors: list[str],
) -> None:
    required_phrases = (
        "Provider 与只读工具执行均为 at-least-once",
        "AFTER_TOOL_COMPLETION_BEFORE_EVENT",
        "可重复工具调用",
        "只有本地 quota/草稿/step/head/轨迹/terminal 最多一次",
    )
    cl6 = _split_claim_sections(claim_ledger).get("CL-LG-06", "")
    limitation = _field(cl6, "限制")
    scoped_documents = {
        "CL-LG-06 limitation": limitation,
        "Product Manifest": manifest,
        "Interview Pack": interview_pack,
    }
    for label, text in scoped_documents.items():
        for phrase in required_phrases:
            if phrase not in text:
                errors.append(f"{label} omits tool/provider boundary: {phrase}")

    tool_exactly_once = re.compile(
        r"(?:只读)?工具(?:执行|调用)?.{0,24}"
        r"(?:exactly[- ]once|精确一次)",
        flags=re.IGNORECASE,
    )
    for label, text in scoped_documents.items():
        if tool_exactly_once.search(text):
            errors.append(f"{label} incorrectly claims tool exactly-once")


def _verify_text_hygiene(path: Path, text: str, errors: list[str]) -> None:
    trailing_lines = [
        line_number
        for line_number, line in enumerate(text.splitlines(), start=1)
        if line.endswith((" ", "\t"))
    ]
    if trailing_lines:
        errors.append(
            f"trailing whitespace in {path.relative_to(ROOT)} lines {trailing_lines}"
        )
    if not text.endswith("\n"):
        errors.append(f"missing final newline: {path.relative_to(ROOT)}")
    elif text.endswith("\n\n"):
        errors.append(f"extra blank lines at EOF: {path.relative_to(ROOT)}")


def _verify_metrics(manifest: str, source: str, errors: list[str]) -> None:
    if T41_REVISION not in manifest:
        errors.append("Product Manifest does not bind the full T41 revision")
    if "product_verified=true" not in manifest:
        errors.append("Product Manifest does not record product_verified=true")
    if "interview_ready=pending_user_verification" not in manifest:
        errors.append("Product Manifest overstates or omits interview readiness")
    if "accesspilot-v1.3-t41-acceptance-2026-08-20.md" not in manifest:
        errors.append("Product Manifest does not name its T41 metric source")
    for label, token in METRIC_SOURCE_TOKENS.items():
        if token not in source:
            errors.append(f"T41 source no longer contains {label}: {token}")
        if token not in manifest:
            errors.append(f"Product Manifest does not reproduce T41 {label}: {token}")
    if "restart + rollback `2/2 = 100%`" not in manifest:
        errors.append("Product Manifest does not record combined T41 recovery 2/2")
    if "不是历史 `425 / 87 / 101` 复用" not in manifest:
        errors.append("Product Manifest does not reject reuse of the v1.2 denominators")


def _verify_boundaries(
    claim_ledger: str,
    manifest: str,
    interview_pack: str,
    errors: list[str],
) -> None:
    combined = "\n".join((claim_ledger, manifest, interview_pack))
    for label, tokens in REQUIRED_BOUNDARIES.items():
        if not all(token in combined for token in tokens):
            errors.append(f"missing required boundary disclosure: {label}")

    heading = "## 可核验的中文简历候选表述"
    if heading not in interview_pack:
        errors.append("Interview Pack has no resume-candidate section")
        return
    candidates = interview_pack.split(heading, maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    numbered_candidates = re.findall(r"^\d+\.\s+.+$", candidates, re.MULTILINE)
    if len(numbered_candidates) != 3:
        errors.append(
            f"expected 3 bounded resume candidates, got {len(numbered_candidates)}"
        )
    for label, pattern in CANDIDATE_OVERCLAIM_PATTERNS:
        if re.search(pattern, candidates, flags=re.IGNORECASE):
            errors.append(f"resume candidate contains semantic overclaim: {label}")
    if "本地作品集原型" not in candidates:
        errors.append("resume candidates do not identify the local portfolio boundary")
    for claim_id in REQUIRED_CLAIMS:
        if f"](accesspilot-v1.3-claim-ledger.md#{claim_id.lower()})" not in candidates:
            errors.append(f"resume candidates do not trace back to {claim_id}")
    if "不得自动写入正式简历" not in interview_pack:
        errors.append("Interview Pack does not preserve the formal-resume gate")
    if "interview_ready=pending_user_verification" not in interview_pack:
        errors.append("Interview Pack does not preserve the user-verification gate")
    if "```mermaid" not in interview_pack:
        errors.append("Interview Pack has no repo-native Mermaid Agent Loop diagram")
    if "## Demo 证据索引" not in interview_pack:
        errors.append("Interview Pack has no Demo evidence index")


def _verify_index(index: str, errors: list[str]) -> None:
    for filename in (
        CLAIM_LEDGER.name,
        PRODUCT_MANIFEST.name,
        INTERVIEW_PACK.name,
    ):
        if filename not in index:
            errors.append(f"docs/README.md does not index {filename}")


def _verify_mutation_probes(
    *,
    claim_ledger: str,
    manifest: str,
    source: str,
    interview_pack: str,
    errors: list[str],
) -> None:
    probes: list[tuple[str, bool]] = []

    link_errors: list[str] = []
    _verify_links(CLAIM_LEDGER, "[missing](definitely-missing.md)", link_errors)
    probes.append(("broken link", any("broken link" in item for item in link_errors)))

    claim_errors: list[str] = []
    wrong_claim = claim_ledger.replace("## CL-LG-06", "## CL-LG-99", 1)
    _verify_claims(wrong_claim, claim_errors)
    probes.append(("wrong claim ID", any("claim IDs mismatch" in item for item in claim_errors)))

    revision_errors: list[str] = []
    wrong_revision = claim_ledger.replace(T41_REVISION, "0" * 40)
    _verify_claims(wrong_revision, revision_errors)
    probes.append(
        (
            "wrong revision",
            any("does not bind the full T41 revision" in item for item in revision_errors),
        )
    )

    metric_errors: list[str] = []
    wrong_metric = manifest.replace("868 passed, 0 skipped", "867 passed, 0 skipped")
    _verify_metrics(wrong_metric, source, metric_errors)
    probes.append(
        (
            "wrong metric",
            any("does not reproduce T41 API" in item for item in metric_errors),
        )
    )

    overclaim_errors: list[str] = []
    overclaim = interview_pack.replace(
        "1. 在本地作品集原型中",
        "1. 已上线生产环境，在本地作品集原型中",
        1,
    )
    _verify_boundaries(claim_ledger, manifest, overclaim, overclaim_errors)
    probes.append(
        (
            "candidate overclaim",
            any("semantic overclaim" in item for item in overclaim_errors),
        )
    )

    boundary_errors: list[str] = []
    _verify_boundaries(
        claim_ledger.replace("provider_token_latency", "provider-latency"),
        manifest.replace("provider_token_latency", "provider-latency"),
        interview_pack.replace("provider_token_latency", "provider-latency"),
        boundary_errors,
    )
    probes.append(
        (
            "missing boundary",
            any("provider latency unmeasured" in item for item in boundary_errors),
        )
    )

    missing_tool_errors: list[str] = []
    _verify_at_least_once_boundaries(
        claim_ledger.replace(
            "Provider 与只读工具执行均为 at-least-once",
            "Provider 为 at-least-once",
        ),
        manifest,
        interview_pack,
        missing_tool_errors,
    )
    probes.append(
        (
            "missing tool at-least-once boundary",
            any("omits tool/provider boundary" in item for item in missing_tool_errors),
        )
    )

    exactly_once_errors: list[str] = []
    _verify_at_least_once_boundaries(
        claim_ledger,
        manifest,
        interview_pack.replace(
            "Provider 与只读工具执行均为 at-least-once",
            "Provider 为 at-least-once，只读工具执行为 exactly-once",
        ),
        exactly_once_errors,
    )
    probes.append(
        (
            "tool exactly-once overclaim",
            any("tool exactly-once" in item for item in exactly_once_errors),
        )
    )

    for label, detected in probes:
        if not detected:
            errors.append(f"mutation probe was not rejected: {label}")


def main() -> int:
    errors: list[str] = []
    requested_probes = sys.argv[1:] == ["--mutation-probes"]
    if sys.argv[1:] and not requested_probes:
        errors.append("usage: verify-t42-evidence.py [--mutation-probes]")
    _verify_revision(errors)
    _verify_change_scope(errors)

    source = _read(T41_EVIDENCE, errors)
    claim_ledger = _read(CLAIM_LEDGER, errors)
    manifest = _read(PRODUCT_MANIFEST, errors)
    interview_pack = _read(INTERVIEW_PACK, errors)
    index = _read(DOC_INDEX, errors)

    if claim_ledger:
        _verify_claims(claim_ledger, errors)
        _verify_cl5_direct_ui_evidence(claim_ledger, errors)
    if manifest and source:
        _verify_metrics(manifest, source, errors)
    if claim_ledger and manifest and interview_pack:
        _verify_boundaries(claim_ledger, manifest, interview_pack, errors)
        _verify_at_least_once_boundaries(
            claim_ledger, manifest, interview_pack, errors
        )
    if index:
        _verify_index(index, errors)
    for path, text in (
        (CLAIM_LEDGER, claim_ledger),
        (PRODUCT_MANIFEST, manifest),
        (INTERVIEW_PACK, interview_pack),
        (DOC_INDEX, index),
    ):
        if text:
            _verify_links(path, text, errors)
    loaded_t42_text = {
        CLAIM_LEDGER: claim_ledger,
        PRODUCT_MANIFEST: manifest,
        INTERVIEW_PACK: interview_pack,
        VERIFIER: VERIFIER.read_text(encoding="utf-8"),
    }
    for path in T42_TEXT_ARTIFACTS:
        text = loaded_t42_text[path]
        if text:
            _verify_text_hygiene(path, text, errors)
    if requested_probes and claim_ledger and manifest and source and interview_pack:
        _verify_mutation_probes(
            claim_ledger=claim_ledger,
            manifest=manifest,
            source=source,
            interview_pack=interview_pack,
            errors=errors,
        )

    if errors:
        print("T42 evidence verification: FAIL", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("T42 evidence verification: PASS")
    print(f"- revision: {T41_REVISION}")
    print(f"- claims: {len(REQUIRED_CLAIMS)}/6")
    print(f"- metrics: {len(METRIC_SOURCE_TOKENS)} source-bound checks")
    print("- formal resume modified: no")
    if requested_probes:
        print("- mutation probes rejected: 8/8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
