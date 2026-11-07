"""Build the reviewed 15-hospital registry and three-hospital Taizhou pilot."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from careguide.hospital_corpus.normalize import normalize_text
from careguide.hospital_corpus.review import apply_review_manifest
from careguide.hospital_corpus.store import write_corpus


CN_TZ = timezone(timedelta(hours=8))
REVIEWED_AT = datetime(2026, 11, 5, 14, 0, tzinfo=CN_TZ)
IDENTITY_VALID_UNTIL = date(2027, 11, 5)
RULE_VALID_UNTIL = date(2027, 2, 3)


HOSPITALS = [
    ("js_taizhou_001", "南京医科大学附属泰州人民医院", [], "泰州", "general", "www.jstzhospital.com", [("main", "总院区", "江苏省泰州市医药高新区太湖路366号")]),
    ("js_taizhou_002", "泰州市中医院", [], "泰州", "tcm_general", "www.tzszyy.com", [("jichuan", "济川路院区", "江苏省泰州市海陵区济川东路86号")]),
    ("js_taizhou_003", "泰州市第二人民医院", ["泰州二院", "姜堰市人民医院"], "泰州", "general", "www.jyry.com", [("main", "主院区", "江苏省泰州市姜堰区健康路27号")]),
    ("js_nanjing_001", "江苏省人民医院", ["南京医科大学第一附属医院"], "南京", "general", "", [("main", "本部", "江苏省南京市广州路300号")]),
    ("js_nanjing_002", "南京鼓楼医院", [], "南京", "general", "www.njglyy.com", [("main", "本部", "江苏省南京市中山路321号")]),
    ("js_nanjing_003", "南京市儿童医院", ["南京医科大学附属儿童医院"], "南京", "children", "www.njch.com.cn", [("guangzhou", "广州路院区", "江苏省南京市鼓楼区广州路72号"), ("hexi", "河西院区", "江苏省南京市建邺区江东南路8号")]),
    ("js_nanjing_004", "江苏省中医院", ["南京中医药大学附属医院"], "南京", "tcm_general", "", [("main", "总院区", "江苏省南京市汉中路155号")]),
    ("js_suzhou_001", "苏州大学附属第一医院", [], "苏州", "general", "", [("main", "总院", "江苏省苏州市平海路899号")]),
    ("js_suzhou_002", "苏州大学附属第二医院", [], "苏州", "general", "", [("main", "三香路院区", "江苏省苏州市三香路1055号")]),
    ("js_suzhou_003", "苏州大学附属儿童医院", [], "苏州", "children", "www.sdfey.cn", [("main", "总院", "江苏省苏州工业园区钟南街92号")]),
    ("js_wuxi_001", "无锡市人民医院", [], "无锡", "general", "www.wuxiph.com", [("main", "主院区", "江苏省无锡市清扬路299号")]),
    ("js_wuxi_002", "无锡市中医医院", [], "无锡", "tcm_general", "", [("main", "主院区", "江苏省无锡市中南西路8号")]),
    ("js_changzhou_001", "常州市第一人民医院", [], "常州", "general", "", [("main", "本部", "江苏省常州市局前街185号")]),
    ("js_nantong_001", "南通大学附属医院", [], "南通", "general", "", [("main", "西院区", "江苏省南通市西寺路20号")]),
    ("js_yangzhou_001", "江苏省苏北人民医院", ["苏北人民医院"], "扬州", "general", "www.yzsbh.com", [("main", "主院区", "江苏省扬州市南通西路98号")]),
]


PRIMARY_SOURCES = {
    "js_taizhou_001": ("https://tzhh.taizhou.gov.cn/xxgk/ggtz/art/2024/art_afc11b41f6334450829964f941b9d4b1.html", "government"),
    "js_taizhou_002": ("https://tzhh.taizhou.gov.cn/xxgk/ggtz/art/2024/art_afc11b41f6334450829964f941b9d4b1.html", "government"),
    "js_taizhou_003": ("https://www.jyry.com/yygk/yygk.aspx", "hospital_official"),
    "js_nanjing_001": ("https://wjw.jiangsu.gov.cn/art/2024/7/25/art_49490_6012987.html", "health_authority"),
    "js_nanjing_002": ("https://www.njglyy.com/", "hospital_official"),
    "js_nanjing_003": ("https://www.njch.com.cn/", "hospital_official"),
    "js_nanjing_004": ("https://wjw.jiangsu.gov.cn/art/2024/7/25/art_49490_6012987.html", "health_authority"),
    "js_suzhou_001": ("https://wjw.jiangsu.gov.cn/art/2024/7/25/art_49490_6012987.html", "health_authority"),
    "js_suzhou_002": ("https://ggzy.suzhou.gov.cn/jyxx/003004/003004006/20260609/025d92d41eb148018bf33b9a9a6a0881.html", "government"),
    "js_suzhou_003": ("https://www.sdfey.cn/booking.html?type=link", "hospital_official"),
    "js_wuxi_001": ("https://en.wuxi.gov.cn/2025-10/13/c_1131209.htm", "government"),
    "js_wuxi_002": ("https://en.wuxi.gov.cn/2025-10/13/c_1131209.htm", "government"),
    "js_changzhou_001": ("https://www.changzhou.gov.cn/gi_news/670178419973618", "health_authority"),
    "js_nantong_001": ("https://wjw.jiangsu.gov.cn/art/2024/7/25/art_49490_6012987.html", "health_authority"),
    "js_yangzhou_001": ("https://www.yzsbh.com/Html/Hospitals/Main/ContactUs.html", "hospital_official"),
}


GRADE_SOURCES = {
    "js_nanjing_001": ("https://wjw.jiangsu.gov.cn/art/2025/7/17/art_55461_11604076.html", "health_authority", date(2028, 12, 31)),
    "js_nanjing_002": ("https://wjw.jiangsu.gov.cn/art/2022/10/10/art_49511_10625237.html", "health_authority", IDENTITY_VALID_UNTIL),
    "js_nanjing_004": ("https://wjw.jiangsu.gov.cn/art/2023/5/16/art_57228_10893867.html", "health_authority", IDENTITY_VALID_UNTIL),
    "js_suzhou_001": ("https://wjw.jiangsu.gov.cn/art/2024/9/23/art_7316_11367843.html", "health_authority", date(2027, 12, 31)),
    "js_suzhou_002": ("https://wjw.jiangsu.gov.cn/art/2022/10/10/art_49511_10625237.html", "health_authority", IDENTITY_VALID_UNTIL),
    "js_changzhou_001": ("https://wjw.jiangsu.gov.cn/art/2024/9/23/art_7316_11367843.html", "health_authority", date(2027, 12, 31)),
    "js_nantong_001": ("https://wjw.jiangsu.gov.cn/art/2024/9/23/art_7316_11367843.html", "health_authority", date(2027, 12, 31)),
    "js_yangzhou_001": ("https://wjw.jiangsu.gov.cn/art/2024/9/23/art_7316_11367843.html", "health_authority", date(2027, 12, 31)),
}


PILOT_SECONDARY = {
    "js_taizhou_001": ("https://wjw.taizhou.gov.cn/xwzx/gzdt/art/2024/art_377954e9690f4ef7b52c827fc7e8bb56.html", "health_authority", "appointment", "泰州市一号管三天惠民措施"),
    "js_taizhou_002": ("https://js.guahaoe.com/hospital/desc/BF314EAD233B3ED3E040007F01004B66000", "official_appointment", "appointment", "泰州市中医院官方预约平台信息"),
    "js_taizhou_003": ("https://www.jyry.com/jyzn/jyzn.aspx", "hospital_official", "appointment", "泰州市第二人民医院预约指南"),
}


PILOT_EXTRA_SOURCES = {
    "js_taizhou_001": ("https://tzhh.taizhou.gov.cn/xxgk/ggtz/art/2024/art_afc11b41f6334450829964f941b9d4b1.html", "government", "campus", "泰州市人民医院总院地址"),
    "js_taizhou_002": ("https://tzhh.taizhou.gov.cn/xxgk/ggtz/art/2024/art_afc11b41f6334450829964f941b9d4b1.html", "government", "campus", "泰州市中医院济川路院区地址"),
    "js_taizhou_003": ("https://www.jyry.com/yygk/yygk.aspx", "hospital_official", "contact", "泰州市第二人民医院地址与联系方式"),
}


PILOT_RECORDS = [
    ("js_taizhou_001", "identity", "医院身份与院区", "泰州市红十字会公开的医疗卫生机构名录显示，南京医科大学附属泰州人民医院为三级甲等医院，总院地址为泰州市医药高新区太湖路366号。患者就诊前仍应根据预约信息核对具体院区。"),
    ("js_taizhou_001", "campus", "总院区地址", "泰州市红十字会公开的医疗卫生机构信息显示，南京医科大学附属泰州人民医院总院地址为泰州市医药高新区太湖路366号。前往其他门诊或专病中心时，仍应以预约短信、医院当日通知所标明的院区为准。"),
    ("js_taizhou_001", "appointment", "普通门诊惠民措施", "泰州市卫生健康委员会公开信息显示，泰州市在市域所有二三级公立医院实行“一号管三天”。患者如需在泰州就诊，应向目标医院确认实际适用条件和办理方式，并以医院当日公告为准。"),
    ("js_taizhou_002", "identity", "医院身份与院区", "泰州市中医院为三级甲等中医医院。本版只登记已有政府公开页面直接证明的济川路院区，未把缺少同等级快照证据的其他院区写入正式记录。前往医院前应按预约科室再次确认院区，避免因院区不同耽误就诊。"),
    ("js_taizhou_002", "campus", "济川路院区地址", "泰州市红十字会公开的医疗卫生机构信息显示，泰州市中医院地址为泰州市海陵区济川东路86号。本记录只用于院区导航，不代表所有科室均在该院区出诊；具体科室、门诊楼和报到地点应以预约结果为准。"),
    ("js_taizhou_002", "appointment", "预约平台与院区", "江苏卫生集约式预约诊疗服务平台登记了泰州市中医院东院信息及预约入口。预约号源和停诊信息可能变化，应在出发前通过官方渠道再次确认。"),
    ("js_taizhou_003", "identity", "医院身份与地址", "泰州市第二人民医院原为姜堰市人民医院，是三级甲等综合医院，主院区位于江苏省泰州市姜堰区健康路27号。就诊前应核对科室和门诊时间。"),
    ("js_taizhou_003", "contact", "地址与咨询渠道", "泰州市第二人民医院官网医院概况页列明医院地址为泰州市姜堰区健康路27号，并公开急救及预约咨询渠道。电话号码、接听时间和服务范围可能调整，使用前应以医院官网最新展示或院内公告为准。"),
    ("js_taizhou_003", "appointment", "门诊预约方式", "泰州市第二人民医院预约指南列有网上预约、电话预约、现场预约、诊间预约、出院预约和自助预约等方式；预约咨询电话及服务时间应以医院最新页面为准。"),
]


def _source_id(hospital_id: str, suffix: str) -> str:
    return f"src_{hospital_id.removeprefix('js_')}_{suffix}"


def _definitions():
    hospitals = []
    source_defs = []
    for hospital_id, name, aliases, city, hospital_type, domain, campuses in HOSPITALS:
        primary_id = _source_id(hospital_id, "identity")
        grade_id = _source_id(hospital_id, "grade") if hospital_id in GRADE_SOURCES else primary_id
        hospitals.append({
            "hospital_id": hospital_id,
            "official_name": name,
            "aliases": aliases,
            "city": city,
            "hospital_type": hospital_type,
            "grade": "三级甲等",
            "grade_source_id": grade_id,
            "official_domains": [domain] if domain else [],
            "campuses": [{
                "campus_id": f"{hospital_id}_{key}",
                "official_name": campus_name,
                "address": address,
                "address_source_id": primary_id,
            } for key, campus_name, address in campuses],
        })
        url, tier = PRIMARY_SOURCES[hospital_id]
        source_defs.append((primary_id, hospital_id, None, f"{name}身份、等级与院区", url, tier, "identity", IDENTITY_VALID_UNTIL))
        if hospital_id in GRADE_SOURCES:
            grade_url, grade_tier, grade_valid_until = GRADE_SOURCES[hospital_id]
            source_defs.append((grade_id, hospital_id, None, f"{name}三级甲等等级依据", grade_url, grade_tier, "identity", grade_valid_until))
        if hospital_id in PILOT_SECONDARY:
            url, tier, category, title = PILOT_SECONDARY[hospital_id]
            source_defs.append((_source_id(hospital_id, category), hospital_id, None, title, url, tier, category, RULE_VALID_UNTIL))
        if hospital_id in PILOT_EXTRA_SOURCES:
            url, tier, category, title = PILOT_EXTRA_SOURCES[hospital_id]
            source_defs.append((_source_id(hospital_id, category), hospital_id, None, title, url, tier, category, IDENTITY_VALID_UNTIL))
    return hospitals, source_defs


def _records(sources):
    sources_by_scope = {
        (source["hospital_id"], source["category"]): source
        for source in sources
        if source["hospital_id"] in {"js_taizhou_001", "js_taizhou_002", "js_taizhou_003"}
    }
    hospital_map = {item[0]: item for item in HOSPITALS}
    records = []
    for index, (hospital_id, category, title, content) in enumerate(PILOT_RECORDS, 1):
        source = sources_by_scope[(hospital_id, category)]
        campus_key = hospital_map[hospital_id][6][0][0]
        records.append({
            "document_id": f"{hospital_id}_{category}_{index:04d}",
            "hospital_id": hospital_id,
            "campus_id": f"{hospital_id}_{campus_key}",
            "city": hospital_map[hospital_id][3],
            "category": category,
            "title": title,
            "content": content,
            "source_id": source["source_id"],
            "source_url": source["url"],
            "source_tier": source["source_tier"],
            "published_at": source["published_at"],
            "collected_at": source["collected_at"],
            "reviewed_at": None,
            "review_status": "needs_review",
            "content_hash": hashlib.sha256(normalize_text(content).encode("utf-8")).hexdigest(),
            "language": "zh-CN",
        })
    return records


def _load_frozen_sources(source_dir: Path, source_defs) -> list[dict]:
    """Load and authenticate the reviewed input bundle without making network calls."""
    raw = json.loads((source_dir / "sources.json").read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Frozen sources.json must contain a list")
    expected = {
        source_id: {
            "hospital_id": hospital_id,
            "campus_id": campus_id,
            "title": title,
            "url": url,
            "source_tier": tier,
            "category": category,
            "valid_until": valid_until.isoformat(),
        }
        for source_id, hospital_id, campus_id, title, url, tier, category, valid_until
        in source_defs
    }
    actual = {item.get("source_id"): item for item in raw if isinstance(item, dict)}
    if None in actual or set(actual) != set(expected) or len(actual) != len(raw):
        raise ValueError("Frozen source identities do not match the reviewed plan")

    sources: list[dict] = []
    for source_id, definition in expected.items():
        item = dict(actual[source_id])
        for field, value in definition.items():
            if item.get(field) != value:
                raise ValueError(f"Frozen source {source_id} has unexpected {field}")
        if item.get("reviewed_at") is not None or item.get("review_status") != "needs_review":
            raise ValueError(
                f"Frozen source {source_id} must remain unreviewed until the offline review build"
            )
        snapshot_path = item.get("snapshot_path")
        if not isinstance(snapshot_path, str) or not snapshot_path.startswith("snapshots/"):
            raise ValueError(f"Frozen source {source_id} has an invalid snapshot path")
        snapshot = source_dir.joinpath(*Path(snapshot_path).parts)
        if not snapshot.is_file():
            raise ValueError(f"Frozen source {source_id} snapshot is missing")
        digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        if digest != item.get("retrieved_sha256"):
            raise ValueError(f"Frozen source {source_id} snapshot hash mismatch")
        collected_at = datetime.fromisoformat(item["collected_at"])
        if collected_at.tzinfo is None or collected_at > REVIEWED_AT:
            raise ValueError(f"Frozen source {source_id} was not collected before review")
        sources.append(item)
    return sources


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("data/jiangsu_hospitals_v1"))
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("data/jiangsu_hospital_sources_v1"),
        help="Reviewed, immutable source snapshots used as offline build inputs.",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    hospitals, source_defs = _definitions()
    if not args.apply:
        print(json.dumps({"status": "dry_run", "hospital_count": len(hospitals), "source_count": len(source_defs), "record_count": len(PILOT_RECORDS), "external_calls": 0}, ensure_ascii=False, sort_keys=True))
        return 0
    if args.output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite {args.output_dir}")
    sources = _load_frozen_sources(args.source_dir, source_defs)
    records = _records(sources)
    reviewed = apply_review_manifest(args.source_dir, sources, records)
    built_at = max(datetime.now(CN_TZ), REVIEWED_AT)
    corpus = write_corpus(
        args.output_dir,
        hospitals=hospitals,
        sources=reviewed.sources,
        records=reviewed.records,
        verification=reviewed.verification,
        built_at=built_at,
        snapshot_root=args.source_dir / "snapshots",
    )
    (args.output_dir / "README.md").write_text(
        "# 江苏医院官方知识库 v1\n\n"
        "当前版本登记15家三级甲等医院；用户可见内容仅覆盖泰州3家试点医院。\n"
        "所有发布记录均来自已冻结并人工复核的医院官网、卫生健康主管部门或江苏官方预约平台快照。\n"
        "交通、停车和无障碍信息尚无同等级冻结证据，因此本版明确留空并标记待核验。\n"
        "本库只提供就医导航，不提供诊断、处方或用药建议。\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "built", "hospital_count": len(corpus.hospitals), "source_count": len(corpus.sources), "record_count": len(corpus.records), "external_calls": 0}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
