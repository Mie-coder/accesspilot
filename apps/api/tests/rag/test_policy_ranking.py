"""政策检索排序、引用筛选与按向量模型校准的阈值（不依赖数据库）。"""

from types import SimpleNamespace

from accesspilot.agent.embeddings import DashScopeEmbeddingModel, DeterministicEmbeddingModel
from accesspilot.domain.catalog import POLICIES
from accesspilot.rag.policies import PolicyMatch, _policy_text, rank_policy_matches
from accesspilot.tools.policies import PolicyService, select_policy_evidence

TITLES = {policy.code: policy.title for policy in POLICIES}


def _match(code: str, similarity: float, overlap: float = 0.0) -> PolicyMatch:
    return PolicyMatch(
        policy_code=code,
        title=TITLES[code],
        content="条款正文",
        similarity=similarity,
        keyword_overlap=overlap,
    )


def test_index_text_appends_search_hints_after_the_clause() -> None:
    text = _policy_text(
        SimpleNamespace(policy_code="POL-003", title="高风险权限双审批", content="条款")
    )

    assert text.startswith("高风险权限双审批\n条款\n常见问法：")
    assert "驳回" in text


def test_keyword_overlap_breaks_a_near_tie_in_cosine_similarity() -> None:
    ranked = rank_policy_matches(
        "权限申请要先满足什么资格条件？",
        [
            _match("POL-001", 0.738),
            _match("POL-002", 0.730),
            _match("POL-006", 0.686),
        ],
    )

    assert [match.policy_code for match in ranked][0] == "POL-002"
    assert ranked[0].similarity == 0.730


def test_selection_drops_citations_far_below_the_best_match() -> None:
    answer = select_policy_evidence(
        [_match("POL-001", 0.80), _match("POL-006", 0.72), _match("POL-003", 0.69)],
        0.50,
    )

    assert [item.policy_code for item in answer.evidence] == ["POL-001"]


def test_selection_cites_at_most_two_close_policies() -> None:
    answer = select_policy_evidence(
        [_match("POL-003", 0.74), _match("POL-006", 0.73), _match("POL-005", 0.72)],
        0.50,
    )

    assert [item.policy_code for item in answer.evidence] == ["POL-003", "POL-006"]


def test_out_of_scope_question_is_insufficient_under_the_real_embedding_threshold() -> None:
    service = PolicyService(embedding_model=DashScopeEmbeddingModel(api_key="test-only"))
    answer = select_policy_evidence(
        [_match("POL-002", 0.45), _match("POL-004", 0.34)], service.similarity_threshold
    )

    assert answer.status == "insufficient_evidence"
    assert answer.evidence == []


def test_threshold_defaults_follow_the_embedding_model() -> None:
    assert PolicyService(embedding_model=DeterministicEmbeddingModel()).similarity_threshold == 0.20
    assert (
        PolicyService(
            embedding_model=DashScopeEmbeddingModel(api_key="test-only")
        ).similarity_threshold
        == 0.50
    )
    assert (
        PolicyService(
            embedding_model=DeterministicEmbeddingModel(), similarity_threshold=0.30
        ).similarity_threshold
        == 0.30
    )
