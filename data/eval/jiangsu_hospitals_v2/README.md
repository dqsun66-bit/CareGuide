# 江苏医院知识库 BM25 评测集 v2

本评测集验证 15 家江苏三甲医院核心知识记录的离线、范围约束 BM25 检索，不调用大模型、Embedding 或 Rerank。

- 案例数：45（每家医院 3 条）
- 正例：每家医院 2 条，覆盖医院身份及院区、预约、联系方式或就医准备信息
- 硬负例：每家医院 1 条，覆盖实时停车、实时路线或医生排班等未被静态知识库支持的问题
- 评估规则：先按声明的 `hospital_id` 与 `category` 过滤，再进行 BM25 排序
- `retrieval_cases.jsonl` SHA-256：`cca24d6a65adb01381e617e18ab705b71d59c7ff08f623c717405072115b3876`

当前基线见 `bm25_baseline.json`。由于评测先进行医院和类别过滤，且每家医院仅发布三条核心记录，本基线明显容易于未来的全国医院、大规模语料和跨类别排序基准；它主要证明范围控制、来源可追溯和无证据类别拒答，不应被解释为通用检索能力上限。

## 华为云混合检索评测

医院 V2 集合使用华为云 MaaS `bge-m3` Embedding、BM25、RRF 和
`bge-reranker-v2-m3` 重排。配置文件必须显式传入，脚本不会打印密钥。

```powershell
.\.venv\Scripts\python.exe scripts\index_jiangsu_hospitals.py --apply --config-file .env --corpus-dir data\jiangsu_hospitals_v2 --today 2026-11-13
.\.venv\Scripts\python.exe scripts\evaluate_jiangsu_hospital_hybrid.py --config-file .env --corpus-dir data\jiangsu_hospitals_v2 --cases data\eval\jiangsu_hospitals_v2\retrieval_cases.jsonl --today 2026-11-13 --output data\eval\jiangsu_hospitals_v2\hybrid_huawei_result.json
```

如需让隔离工作树复用主工作区的同一个已验证索引，可在两个命令上显式添加
`--chroma-dir D:\codex\CareGuide\data\chroma`，避免重复产生 Embedding 费用。

通过门槛：

- Recall@5 不低于 0.90
- MRR 不低于 0.75
- 硬负例准确率等于 1.0
- 医院、院区、类别或官方审核状态范围违规为 0

只有全部门槛通过时，评测脚本才写入 `hybrid_huawei_result.json`。
