from law_agent.review.rules import RULE_VERSION, ComplianceFacts, evaluate_national_path


def _complete_facts(**updates: object) -> ComplianceFacts:
    values: dict[str, object] = {
        "cross_border_transfer": True,
        "is_ciio": False,
        "important_data": False,
        "contains_personal_information": True,
        "contains_sensitive_personal_information": False,
        "cumulative_personal_information_subjects": 10,
        "cumulative_sensitive_personal_information_subjects": 0,
    }
    values.update(updates)
    return ComplianceFacts.model_validate(values)


def test_important_data_requires_security_assessment() -> None:
    result = evaluate_national_path(
        ComplianceFacts(cross_border_transfer=True, important_data=True)
    )

    assert result.status == "determined"
    assert [path.code for path in result.candidate_paths] == ["security_assessment"]
    assert result.rule_hits[0].rule_id == "security_assessment.important_data"
    assert result.official_bases[0].article == "第七条"


def test_ciio_personal_information_requires_security_assessment() -> None:
    result = evaluate_national_path(_complete_facts(is_ciio=True))

    assert [path.code for path in result.candidate_paths] == ["security_assessment"]
    assert result.rule_hits[-1].rule_id == "security_assessment.ciio_personal_information"


def test_volume_threshold_requires_security_assessment() -> None:
    result = evaluate_national_path(
        _complete_facts(
            contains_sensitive_personal_information=True,
            cumulative_sensitive_personal_information_subjects=10_000,
        )
    )

    assert [path.code for path in result.candidate_paths] == ["security_assessment"]
    assert result.rule_hits[-1].rule_id == "security_assessment.volume_threshold"


def test_standard_contract_or_certification_interval() -> None:
    result = evaluate_national_path(
        _complete_facts(cumulative_personal_information_subjects=100_000)
    )

    assert result.status == "determined"
    assert [path.code for path in result.candidate_paths] == ["standard_contract_or_certification"]
    assert result.official_bases[-1].article == "第八条"


def test_confirmed_statutory_exemption_wins_before_threshold_checks() -> None:
    result = evaluate_national_path(
        ComplianceFacts(
            cross_border_transfer=True,
            important_data=False,
            claimed_exemption="individual_contract",
            exemption_facts_confirmed=True,
        )
    )

    assert result.status == "determined"
    assert [path.code for path in result.candidate_paths] == ["statutory_exemption"]
    assert result.rule_hits[-1].rule_id == "statutory_exemption.confirmed"


def test_missing_counts_never_produce_a_determined_path() -> None:
    result = evaluate_national_path(_complete_facts(cumulative_personal_information_subjects=None))

    assert result.status == "needs_info"
    assert "cumulative_personal_information_subjects" in {item.key for item in result.needs_info}
    assert all(path.confidence == "possible" for path in result.candidate_paths)


def test_unknown_important_data_blocks_the_decision() -> None:
    result = evaluate_national_path(_complete_facts(important_data=None))

    assert result.status == "needs_info"
    assert "important_data" in {item.key for item in result.needs_info}
    assert result.candidate_paths[0].code == "security_assessment"


def test_unknown_ciio_blocks_personal_information_decision() -> None:
    result = evaluate_national_path(_complete_facts(is_ciio=None))

    assert result.status == "needs_info"
    assert "is_ciio" in {item.key for item in result.needs_info}


def test_special_regimes_only_raise_rag_and_human_confirmation_marker() -> None:
    result = evaluate_national_path(
        _complete_facts(special_regimes=["industry_specific", "free_trade_zone"])
    )

    assert result.status == "determined"
    assert result.candidate_paths[0].code == "no_filing_below_threshold"
    assert result.requires_rag_human_confirmation is True
    assert len(result.manual_confirmation_reasons) == 2


def test_same_facts_and_rule_version_produce_identical_output() -> None:
    facts = _complete_facts(cumulative_personal_information_subjects=999_999)

    first = evaluate_national_path(facts).model_dump(mode="json")
    second = evaluate_national_path(facts).model_dump(mode="json")

    assert first == second
    assert first["rule_version"] == RULE_VERSION
