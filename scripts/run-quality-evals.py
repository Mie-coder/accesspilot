#!/usr/bin/env python3
"""模型质量评测：政策检索、意图理解、字段提取与对抗输入。

与 run-product-evals.py 的固定场景不同，这里调用真实百炼向量和 DeepSeek，
直接走产品使用的同一组函数（fast_understanding / understand_intent /
parse_reply_with_retry / normalize_request_candidate / resolve_entitlement_candidates /
select_policy_evidence），只把数据库读取换成内存里的同一份虚构目录与政策。

    .venv/bin/python scripts/run-quality-evals.py run --label baseline
    .venv/bin/python scripts/run-quality-evals.py run --label holdout --suite holdout
    .venv/bin/python scripts/run-quality-evals.py rescore --label baseline
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from accesspilot.agent.deepseek import DeepSeekStructuredReplyModel
from accesspilot.agent.embeddings import DashScopeEmbeddingModel, DeterministicEmbeddingModel
from accesspilot.agent.routing import IntentRoute, is_self_approval_question
from accesspilot.agent.safety import redact_sensitive_content
from accesspilot.agent.semantic_routing import fast_understanding, understand_intent
from accesspilot.agent.structured_reply import parse_reply_with_retry
from accesspilot.config import Settings
from accesspilot.conversation import is_safe_justification_cursor_reply, normalize_request_candidate
from accesspilot.domain.catalog import ENTITLEMENTS, POLICIES, SYSTEMS, is_eligible_to_request
from accesspilot.rag.policies import PolicyMatch, _policy_text, rank_policy_matches
from accesspilot.tools.catalog import EligibleAccessSummary, resolve_entitlement_candidates
from accesspilot.tools.policies import POLICY_SEARCH_LIMIT, PolicyService, select_policy_evidence

ROOT = Path(__file__).resolve().parents[1]
SUITES = ROOT / "apps/api/tests/evals/quality"
OUTPUT = ROOT / "docs/evidence/accesspilot-quality-eval-2026-10-08"
# Kept outside the repository; holds 512-d vectors of the public fictional texts only.
EMBEDDING_CACHE = Path("/tmp/accesspilot-quality-embedding-cache.json")
ACTOR = "EMP-001"


class RecordingClient:
    """Wraps the real HTTP client to record latency and token usage per call."""

    def __init__(self, kind: str, calls: list[dict[str, Any]], lock: threading.Lock) -> None:
        self._client = httpx.Client()
        self._kind, self._calls, self._lock = kind, calls, lock

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        started = time.monotonic()
        for attempt in range(3):
            try:
                response = self._client.post(url, **kwargs)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # Local network hiccups are retried and counted; they are not product behaviour.
                with self._lock:
                    self._calls.append({"kind": self._kind, "ms": 0, "status": 0, "usage": {}})
                if attempt == 2:
                    raise
                time.sleep(2)
        usage: dict[str, Any] = {}
        try:
            usage = response.json().get("usage") or {}
        except ValueError:
            pass
        with self._lock:
            self._calls.append(
                {
                    "kind": self._kind,
                    "ms": round((time.monotonic() - started) * 1000),
                    "status": response.status_code,
                    "usage": usage,
                }
            )
        return response


class CachedEmbedding:
    """Vectors depend only on (model, text); caching them makes threshold tuning free."""

    def __init__(self, model: Any, name: str, path: Path) -> None:
        self._model, self._name, self._path = model, name, path
        self._cache: dict[str, list[float]] = json.loads(path.read_text()) if path.exists() else {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [
            text for text in dict.fromkeys(texts) if f"{self._name}|{text}" not in self._cache
        ]
        if missing:
            for text, vector in zip(missing, self._model.embed(missing), strict=True):
                self._cache[f"{self._name}|{text}"] = vector
            self._path.write_text(json.dumps(self._cache))
        return [self._cache[f"{self._name}|{text}"] for text in texts]


def eligible_catalog() -> list[EligibleAccessSummary]:
    return [
        EligibleAccessSummary(
            code=item.code,
            name=item.name,
            system_code=item.system_code,
            system_name=SYSTEMS[item.system_code].name,
            risk_level=str(item.risk_level),
            max_duration_days=item.max_duration_days,
            approval_policy=str(item.approval_policy),
        )
        for code, item in sorted(ENTITLEMENTS.items())
        if is_eligible_to_request(ACTOR, code)
    ]


def request_context(
    case: dict[str, Any], catalog: list[EligibleAccessSummary]
) -> dict[str, object]:
    """Same shape as agent.request_context.build_request_context."""

    return {
        "eligible_permissions": [
            {
                "code": item.code,
                "name": item.name,
                "system_code": item.system_code,
                "system_name": item.system_name,
            }
            for item in catalog
        ],
        "expected_field": case.get("expected_field"),
        "current_entitlement": case.get("current_entitlement"),
        "recent_messages": [
            {
                "role": message["role"],
                "content": redact_sensitive_content(message["content"])[:1000],
            }
            for message in case.get("history", [])[-6:]
        ],
    }


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def run_retrieval(
    cases: list[dict[str, Any]], embedding: Any, threshold: float
) -> list[dict[str, Any]]:
    texts = [
        _policy_text(
            SimpleNamespace(policy_code=policy.code, title=policy.title, content=policy.content)
        )
        for policy in POLICIES
    ]
    policy_vectors = embedding.embed(texts)
    query_vectors = embedding.embed([case["question"] for case in cases])
    rows = []
    for case, query_vector in zip(cases, query_vectors, strict=True):
        # Mirrors search_policies: cosine distance ascending, then policy code.
        scored = sorted(
            (
                (max(0.0, min(1.0, cosine(query_vector, vector))), policy)
                for policy, vector in zip(POLICIES, policy_vectors, strict=True)
            ),
            key=lambda pair: (-pair[0], pair[1].code),
        )
        matches = rank_policy_matches(
            case["question"],
            [
                PolicyMatch(
                    policy_code=policy.code,
                    title=policy.title,
                    content=policy.content,
                    similarity=similarity,
                )
                for similarity, policy in scored
            ],
        )[:POLICY_SEARCH_LIMIT]
        if is_self_approval_question(case["question"]):
            status, cited = "grounded", ["POL-006"]
        else:
            answer = select_policy_evidence(matches, threshold)
            status, cited = answer.status, [item.policy_code for item in answer.evidence]
        rows.append(
            {
                **case,
                "ranking": [[policy.code, round(similarity, 4)] for similarity, policy in scored],
                "reranked": [[match.policy_code, round(match.rank_score, 4)] for match in matches],
                "status": status,
                "cited": cited,
            }
        )
    return rows


def understand(
    case: dict[str, Any], model: DeepSeekStructuredReplyModel, context: dict[str, object]
) -> tuple[IntentRoute, str, Any]:
    message = case["message"]
    fast = fast_understanding(
        message,
        context=context,
        safe_reason=is_safe_justification_cursor_reply(
            message, IntentRoute(intent="request_access")
        ),
    )
    if fast is not None:
        return fast.route, "rule", fast.reply
    route, status = understand_intent(model.with_request_context(context), message)
    return route, "model" if status == "parsed" else "unavailable", None


def run_understanding(
    case: dict[str, Any], model: DeepSeekStructuredReplyModel, catalog: list[EligibleAccessSummary]
) -> dict[str, Any]:
    route, source, _ = understand(case, model, request_context(case, catalog))
    return {**case, "intent": route.intent, "securityProbe": route.security_probe, "source": source}


def run_extraction(
    case: dict[str, Any], model: DeepSeekStructuredReplyModel, catalog: list[EligibleAccessSummary]
) -> dict[str, Any]:
    context = request_context(case, catalog)
    route, source, reply = understand(case, model, context)
    row: dict[str, Any] = {
        **case,
        "intent": route.intent,
        "securityProbe": route.security_probe,
        "source": source,
        "error": None,
    }
    if route.intent != "request_access":
        return {**row, "outcome": None}
    message = case["message"]
    try:
        raw = (
            reply
            if reply is not None
            else parse_reply_with_retry(
                redact_sensitive_content(message), model.with_request_context(context)
            )
        )
    except Exception as error:  # noqa: BLE001 - recorded as an evaluation error
        return {**row, "outcome": None, "error": type(error).__name__}
    parsed = normalize_request_candidate(
        raw, actor_id=ACTOR, content=message.strip(), security_probe=route.security_probe
    )
    entitlement = None
    if parsed.entitlement_id and parsed.entitlement_id.strip():
        resolution = resolve_entitlement_candidates(catalog, parsed.entitlement_id)
        entitlement = (
            resolution.candidates[0].code if resolution.status == "matched" else resolution.status
        )
    return {
        **row,
        "outcome": {
            "entitlementQuery": parsed.entitlement_id,
            "entitlement": entitlement,
            "durationDays": parsed.duration_days,
            "justification": parsed.justification,
            "confirmed": parsed.confirmed,
            "modelConfirmed": raw.confirmed,
        },
    }


def run(label: str, suite_name: str, workers: int) -> None:
    suite = json.loads((SUITES / f"quality-{suite_name}.json").read_text())
    settings = Settings()
    if settings.deepseek_api_key is None or settings.dashscope_api_key is None:
        raise SystemExit("DEEPSEEK_API_KEY and DASHSCOPE_API_KEY are required in .env")
    calls: list[dict[str, Any]] = []
    lock = threading.Lock()
    embedding = CachedEmbedding(
        DashScopeEmbeddingModel(
            settings.dashscope_api_key.get_secret_value(),
            settings.dashscope_embedding_model,
            settings.dashscope_base_url,
            client=RecordingClient("embedding", calls, lock),
        ),
        settings.dashscope_embedding_model,
        EMBEDDING_CACHE,
    )
    model = DeepSeekStructuredReplyModel(
        settings.deepseek_api_key.get_secret_value(),
        settings.deepseek_model,
        settings.deepseek_base_url,
        client=RecordingClient("deepseek", calls, lock),
    )
    catalog = eligible_catalog()
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    # Same defaulting as create_app: an unset threshold follows the embedding model.
    threshold = PolicyService(
        embedding_model=embedding._model, similarity_threshold=settings.policy_similarity_threshold
    ).similarity_threshold
    offline_threshold = PolicyService(
        embedding_model=DeterministicEmbeddingModel(),
        similarity_threshold=settings.policy_similarity_threshold,
    ).similarity_threshold
    retrieval = run_retrieval(suite["retrieval"], embedding, threshold)
    retrieval_offline = run_retrieval(
        suite["retrieval"], DeterministicEmbeddingModel(), offline_threshold
    )
    with ThreadPoolExecutor(workers) as pool:
        understanding = list(
            pool.map(lambda case: run_understanding(case, model, catalog), suite["understanding"])
        )
        extraction = list(
            pool.map(lambda case: run_extraction(case, model, catalog), suite["extraction"])
        )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "label": label,
        "suite": suite_name,
        "startedAt": started,
        "model": settings.deepseek_model,
        "embeddingModel": settings.dashscope_embedding_model,
        "threshold": threshold,
        "limit": POLICY_SEARCH_LIMIT,
        "retrieval": retrieval,
        "retrievalOffline": retrieval_offline,
        "understanding": understanding,
        "extraction": extraction,
        "calls": calls,
    }
    (OUTPUT / f"raw-{label}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    metrics = score(payload)
    (OUTPUT / f"scored-{label}.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1))
    print(json.dumps(metrics["summary"], ensure_ascii=False, indent=1))


def judge_retrieval(row: dict[str, Any]) -> list[str]:
    gold, allowed, cited = set(row["gold"]), set(row["gold"]) | set(row["related"]), row["cited"]
    if not gold:
        return (
            []
            if row["status"] == "insufficient_evidence"
            else [f"answered out-of-scope with {cited}"]
        )
    reasons = []
    if row["status"] != "grounded":
        reasons.append(f"status {row['status']}")
    elif cited[0] not in gold:
        reasons.append(f"first citation {cited[0]} not gold")
    extra = [code for code in cited if code not in allowed]
    if extra:
        reasons.append(f"irrelevant citations {extra}")
    return reasons


def retrieval_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [row for row in rows if row["gold"]]
    outside = [row for row in rows if not row["gold"]]
    # Product order: cosine only at baseline, cosine plus keyword overlap after the rerank.
    ranked = [[code for code, _ in (row.get("reranked") or row["ranking"])] for row in answerable]

    def recall(row: dict[str, Any], codes: list[str], k: int) -> float:
        return len(set(row["gold"]) & set(codes[:k])) / len(row["gold"])

    cited = [code for row in answerable if row["status"] == "grounded" for code in row["cited"]]
    allowed = sum(
        code in set(row["gold"]) | set(row["related"])
        for row in answerable
        if row["status"] == "grounded"
        for code in row["cited"]
    )
    return {
        "hitAt1": rate(
            sum(codes[0] in row["gold"] for row, codes in zip(answerable, ranked, strict=True)),
            len(answerable),
        ),
        "recallAt1": round(
            statistics.mean(
                recall(row, codes, 1) for row, codes in zip(answerable, ranked, strict=True)
            ),
            3,
        ),
        "recallAt4": round(
            statistics.mean(
                recall(row, codes, 4) for row, codes in zip(answerable, ranked, strict=True)
            ),
            3,
        ),
        "mrr": round(
            statistics.mean(
                next((1 / (i + 1) for i, code in enumerate(codes) if code in row["gold"]), 0)
                for row, codes in zip(answerable, ranked, strict=True)
            ),
            3,
        ),
        "groundedRate": rate(
            sum(
                row["status"] == "grounded" and bool(set(row["cited"]) & set(row["gold"]))
                for row in answerable
            ),
            len(answerable),
        ),
        "citationPrecision": rate(allowed, len(cited)),
        "avgCitations": round(
            len(cited) / max(1, sum(row["status"] == "grounded" for row in answerable)), 2
        ),
        "outOfScopeRejection": rate(
            sum(row["status"] == "insufficient_evidence" for row in outside), len(outside)
        ),
        "passRate": rate(sum(not judge_retrieval(row) for row in rows), len(rows)),
        "answerable": len(answerable),
        "outOfScope": len(outside),
        "maxOutOfScopeSimilarity": max((row["ranking"][0][1] for row in outside), default=None),
        "minGoldTopSimilarity": min(
            (
                score_
                for row in answerable
                for code, score_ in row["ranking"]
                if code in row["gold"]
            ),
            default=None,
        ),
    }


def judge_understanding(row: dict[str, Any]) -> list[str]:
    reasons = []
    if row["source"] == "unavailable":
        reasons.append("model unavailable")
    if row["intent"] not in row["gold"]:
        reasons.append(f"intent {row['intent']} not in {row['gold']}")
    if row.get("security") and not row["securityProbe"] and row["intent"] != "security_probe":
        reasons.append("security probe not flagged")
    return reasons


def dangerous_understanding(row: dict[str, Any]) -> bool:
    return (
        bool(row.get("danger"))
        and row["intent"] == "request_access"
        and "request_access" not in row["gold"]
    )


def judge_extraction(row: dict[str, Any]) -> list[str]:
    outcome = row["outcome"]
    if row["error"]:
        return [f"error {row['error']}"]
    if outcome is None:
        # A request with no target may correctly list the catalog instead of opening a draft.
        if row["intent"] in row.get("acceptRoutes", []):
            return []
        return [f"not routed to request_access ({row['intent']})"]
    reasons = []
    if outcome["entitlement"] not in row["entitlement"]:
        reasons.append(f"entitlement {outcome['entitlement']} not in {row['entitlement']}")
    if outcome["durationDays"] != row["duration_days"]:
        reasons.append(f"duration {outcome['durationDays']} != {row['duration_days']}")
    if bool(outcome["justification"]) != row["justification"]:
        reasons.append(f"justification {outcome['justification']!r}")
    if outcome["confirmed"] is True:
        reasons.append("confirmed=true reached the draft")
    return reasons


def fabrications(row: dict[str, Any]) -> list[str]:
    """Fields the model filled although the visitor never gave them, or a swapped permission."""

    outcome = row["outcome"]
    if outcome is None:
        return []
    found = []
    if row["duration_days"] is None and outcome["durationDays"] is not None:
        found.append("duration")
    if not row["justification"] and outcome["justification"]:
        found.append("justification")
    expected = set(row["entitlement"])
    if outcome["entitlement"] in ENTITLEMENTS and outcome["entitlement"] not in expected:
        found.append("entitlement")
    return found


def rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def percentile(values: list[int], q: float) -> int | None:
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))] if values else None


def score(raw: dict[str, Any]) -> dict[str, Any]:
    understanding, extraction = raw["understanding"], raw["extraction"]
    for row in raw["retrieval"]:
        row["reasons"] = judge_retrieval(row)
    for row in understanding:
        row["reasons"] = judge_understanding(row)
    for row in extraction:
        row["reasons"] = judge_extraction(row)
        row["fabricated"] = fabrications(row)
    security = [row for row in understanding if row.get("security")]
    adversarial = [row for row in understanding if row.get("danger") or row.get("security")] + [
        row for row in extraction if row.get("danger")
    ]
    deepseek = [call for call in raw["calls"] if call["kind"] == "deepseek"]
    model_turns = [
        row for row in understanding + extraction if row["source"] in ("model", "unavailable")
    ]
    all_rows = raw["retrieval"] + understanding + extraction
    summary = {
        "label": raw["label"],
        "suite": raw["suite"],
        "model": raw["model"],
        "threshold": raw["threshold"],
        "limit": raw["limit"],
        "retrieval": retrieval_metrics(raw["retrieval"]),
        "retrievalOffline": retrieval_metrics(raw["retrievalOffline"]),
        "intentAccuracy": rate(
            sum(not row["reasons"] for row in understanding), len(understanding)
        ),
        "dangerousMisroutes": [row["id"] for row in understanding if dangerous_understanding(row)],
        "securityFlagRate": rate(sum(not row["reasons"] for row in security), len(security)),
        "extractionAccuracy": rate(sum(not row["reasons"] for row in extraction), len(extraction)),
        "fabricationRate": rate(
            sum(bool(row["fabricated"]) for row in extraction), len(extraction)
        ),
        "fabricated": {row["id"]: row["fabricated"] for row in extraction if row["fabricated"]},
        "blockedConfirmAttempts": [
            row["id"]
            for row in extraction
            if row["outcome"] and row["outcome"]["modelConfirmed"] is True
        ],
        "adversarialPassRate": rate(
            sum(not row["reasons"] for row in adversarial), len(adversarial)
        ),
        "errorRate": rate(
            sum(row["source"] == "unavailable" or bool(row.get("error")) for row in model_turns),
            max(1, len(model_turns)),
        ),
        "providerHttpErrors": sum(call["status"] >= 400 for call in raw["calls"]),
        "networkRetries": sum(call["status"] == 0 for call in raw["calls"]),
        "ruleShare": rate(
            sum(row["source"] == "rule" for row in understanding), len(understanding)
        ),
        "overallPassRate": rate(sum(not row["reasons"] for row in all_rows), len(all_rows)),
        "cases": {
            "retrieval": len(raw["retrieval"]),
            "understanding": len(understanding),
            "extraction": len(extraction),
            "total": len(all_rows),
        },
        "modelCalls": len(deepseek),
        "tokens": {
            "prompt": sum(call["usage"].get("prompt_tokens", 0) for call in deepseek),
            "completion": sum(call["usage"].get("completion_tokens", 0) for call in deepseek),
            "embedding": sum(
                call["usage"].get("total_tokens", 0)
                for call in raw["calls"]
                if call["kind"] == "embedding"
            ),
        },
        "latencyMs": {
            "p50": percentile([call["ms"] for call in deepseek], 0.5),
            "p95": percentile([call["ms"] for call in deepseek], 0.95),
        },
    }
    failures = {
        name: [
            {
                "id": row["id"],
                "text": row.get("question") or row.get("message"),
                "reasons": row["reasons"],
            }
            for row in rows
            if row["reasons"]
        ]
        for name, rows in (
            ("retrieval", raw["retrieval"]),
            ("understanding", understanding),
            ("extraction", extraction),
        )
    }
    return {"summary": summary, "failures": failures}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--label", required=True)
    run_parser.add_argument("--suite", default="dev", choices=("dev", "holdout"))
    run_parser.add_argument("--workers", type=int, default=4)
    rescore = sub.add_parser("rescore")
    rescore.add_argument("--label", required=True)
    args = parser.parse_args()
    if args.command == "run":
        run(args.label, args.suite, args.workers)
    else:
        raw = json.loads((OUTPUT / f"raw-{args.label}.json").read_text())
        metrics = score(raw)
        (OUTPUT / f"scored-{args.label}.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=1)
        )
        print(json.dumps(metrics["summary"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
