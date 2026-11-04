"""Deterministic text processing for official hospital sources."""
import pytest

from careguide.hospital_corpus.chunking import Section, chunk_sections
from careguide.hospital_corpus.dedup import duplicate_groups
from careguide.hospital_corpus.normalize import canonical_url, normalize_text, stable_id


def test_url_normalization_removes_tracking_but_keeps_meaningful_query():
    url = "https://WWW.JYRY.COM/guide?id=12&utm_source=wx&spm=share#section"
    assert canonical_url(url) == "https://www.jyry.com/guide?id=12"


def test_url_normalization_sorts_query_and_is_idempotent():
    url = "https://www.jyry.com/guide?z=2&a=1&z=1"
    normalized = canonical_url(url)
    assert normalized == "https://www.jyry.com/guide?a=1&z=1&z=2"
    assert canonical_url(normalized) == normalized


@pytest.mark.parametrize(
    "url",
    ["http://www.jyry.com/", "https://user:secret@www.jyry.com/", "https://localhost/"],
)
def test_url_normalization_rejects_non_public_urls(url):
    with pytest.raises(ValueError, match="public HTTPS"):
        canonical_url(url)


def test_text_normalization_is_idempotent_and_removes_navigation_lines():
    text = "首页\r\n\r\n就医指南\r\n预约须实名\t办理\r\n\r\n\r\n"
    normalized = normalize_text(text)
    assert normalized == "预约须实名 办理"
    assert normalize_text(normalized) == normalized


def test_stable_id_is_normalized_but_scope_sensitive():
    first = stable_id("泰州二院", " 预约挂号 ")
    second = stable_id("泰州二院", "预约挂号")
    assert first == second
    assert first != stable_id("泰州市中医院", "预约挂号")


def test_chunker_keeps_numbered_appointment_steps_together():
    sections = [("预约流程", "1. 实名注册。\n2. 选择科室。\n3. 确认预约。")]
    chunks = chunk_sections(sections, target_min=20, target_max=120, overlap=10)
    assert len(chunks) == 1
    assert "1. 实名注册" in chunks[0].content
    assert "3. 确认预约" in chunks[0].content


def test_chunker_never_crosses_scope_or_section_boundaries():
    sections = [
        Section("hospital-a|campus-a|appointment", "预约", "预约规则内容足够完整，可以单独形成知识片段。"),
        Section("hospital-b|campus-b|contact", "电话", "联系电话内容属于另一所医院，不能与前一条合并。"),
    ]
    chunks = chunk_sections(sections, target_min=10, target_max=80, overlap=5)
    assert [chunk.scope_id for chunk in chunks] == [
        "hospital-a|campus-a|appointment", "hospital-b|campus-b|contact"
    ]


def test_chunker_rejects_mixed_scope_within_one_section():
    section = Section("hospital-a|campus-a|appointment", "预约", "预约规则。")
    object.__setattr__(section, "scope_id", "hospital-a|campus-b|appointment")
    with pytest.raises(ValueError, match="scope"):
        chunk_sections([section], target_min=5, target_max=40, overlap=5,
                       expected_scope="hospital-a|campus-a|appointment")


def test_long_paragraph_splits_at_sentence_boundaries_with_overlap():
    content = "第一句说明预约需要实名。第二句说明患者应核对院区。第三句说明取消规则以公告为准。第四句提醒不要泄露验证码。"
    chunks = chunk_sections([("预约须知", content)], target_min=15, target_max=35, overlap=8)
    assert len(chunks) >= 2
    assert all(len(chunk.content) <= 35 for chunk in chunks)
    assert chunks[0].content[-8:] in chunks[1].content


def test_near_duplicate_group_ignores_navigation_and_update_stamp():
    rows = [
        ("a", "首页 就医指南 预约须实名办理 更新时间2026-10-01"),
        ("b", "首页 就医指南 预约须实名办理 更新时间2026-11-01"),
    ]
    assert duplicate_groups(rows, threshold=0.90) == (("a", "b"),)


def test_duplicate_groups_are_sorted_transitive_and_reproducible():
    rows = [
        ("z", "门诊预约必须实名，并核对就诊院区。"),
        ("a", "门诊预约必须实名，并核对就诊院区。"),
        ("m", "门诊预约必须实名，并核对就诊院区。更新时间2026-11-01"),
        ("x", "停车余位需要查询实时地图服务。"),
    ]
    expected = (("a", "m", "z"),)
    assert duplicate_groups(rows, threshold=0.80) == expected
    assert duplicate_groups(reversed(rows), threshold=0.80) == expected


def test_unrelated_short_text_is_not_grouped():
    rows = [("a", "预约挂号"), ("b", "停车路线"), ("c", "报告查询")]
    assert duplicate_groups(rows, threshold=0.50) == ()
