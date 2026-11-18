"""Explicit medical retrieval configuration. Does not load any .env file."""
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from careguide.corpus import Corpus
from careguide.hospital_corpus.retrieval import (
    HospitalKnowledgeBase,
    hospital_collection_name,
)
from careguide.hospital_corpus.store import HospitalCorpus
from careguide.retrieval.knowledge_base import MedicalKnowledgeBase, versioned_collection_name
from careguide.tools import MedicalToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPEN_CORPUS_ROOT = PROJECT_ROOT / 'data' / 'careguide_kuake_rag_v1'
HOSPITAL_CORPUS_ROOT = PROJECT_ROOT / 'data' / 'jiangsu_hospitals_v2'


@dataclass(frozen=True)
class RetrievalRuntimeStatus:
    requested_mode: str
    active_mode: str
    embedding_provider: str | None
    rerank_provider: str | None
    index_collection: str | None
    degraded_reason: str | None = None


def _required_secret(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value or value.startswith('YOUR_'):
        raise ValueError(f'{name} is required')
    return value


def _required_text(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value or value.startswith('YOUR_'):
        raise ValueError(f'{name} is required')
    return value


def _positive_integer(name: str, default: str | None = None) -> int:
    raw = os.environ.get(name, default or '').strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a positive integer') from None
    if value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


def semantic_embedding():
    from careguide.retrieval.embedding_provider import OpenAICompatibleEmbeddingFunction
    key = _required_secret('CAREGUIDE_EMBEDDING_API_KEY')
    return OpenAICompatibleEmbeddingFunction(
        api_key=key,
        base_url=os.environ.get('CAREGUIDE_EMBEDDING_BASE_URL',
                                'https://api.modelarts-maas.com/v1'),
        model=_required_text('CAREGUIDE_EMBEDDING_MODEL'),
        dimensions=_positive_integer('CAREGUIDE_EMBEDDING_DIMENSIONS', '1024'),
        send_dimensions=False,
        encoding_format=os.environ.get('CAREGUIDE_EMBEDDING_FORMAT', 'float'),
        provider_name='huawei-modelarts', timeout=30, max_retries=2)


def intent_semantic_retriever(mode=None):
    """Separate business index and instruction, reusing the existing provider configuration."""
    from careguide.orchestration.semantic_intent import SemanticIntentRetriever, INSTRUCTION, record_response
    from careguide.retrieval.embedding_provider import OpenAICompatibleEmbeddingFunction
    mode = (mode or os.environ.get('CAREGUIDE_INTENT_SEMANTIC_MODE', 'auto')).strip().casefold()
    if mode not in {'auto', 'remote', 'local_ngram'}:
        raise ValueError('Unknown intent semantic mode')
    key = os.environ.get('CAREGUIDE_EMBEDDING_API_KEY', '')
    configured = bool(key.strip()) and not key.startswith('YOUR_')
    if mode == 'local_ngram' or mode == 'auto' and not configured:
        return None, 'local_ngram' if mode == 'local_ngram' else 'provider_unconfigured'
    if not configured:
        raise ValueError('Remote intent retrieval requires CAREGUIDE_EMBEDDING_API_KEY')
    timeout = float(os.environ.get('CAREGUIDE_INTENT_SEMANTIC_TIMEOUT', '3'))
    if not 0 < timeout <= 10:
        raise ValueError('Intent semantic timeout must be between 0 and 10 seconds')
    provider = OpenAICompatibleEmbeddingFunction(api_key=key,
        base_url=os.environ.get('CAREGUIDE_EMBEDDING_BASE_URL', 'https://api.siliconflow.cn/v1'),
        model=os.environ.get('CAREGUIDE_EMBEDDING_MODEL', 'Qwen/Qwen3-Embedding-0.6B'),
        dimensions=int(os.environ.get('CAREGUIDE_EMBEDDING_DIMENSIONS', '1024')),
        query_instruction=INSTRUCTION, timeout=timeout, max_retries=0, response_observer=record_response)
    return SemanticIntentRetriever(provider, timeout=timeout,
        index_path=os.environ.get('CAREGUIDE_INTENT_SEMANTIC_INDEX') or None), 'remote'


def semantic_reranker():
    from careguide.retrieval.reranker import OpenAICompatibleReranker
    key = os.environ.get('CAREGUIDE_RERANK_API_KEY', '').strip()
    if not key:
        key = _required_secret('CAREGUIDE_EMBEDDING_API_KEY')
    elif key.startswith('YOUR_'):
        raise ValueError('CAREGUIDE_RERANK_API_KEY is required')
    return OpenAICompatibleReranker(
        api_key=key,
        base_url=os.environ.get('CAREGUIDE_RERANK_BASE_URL') or os.environ.get(
            'CAREGUIDE_EMBEDDING_BASE_URL', 'https://api.modelarts-maas.com/v1'),
        model=_required_text('CAREGUIDE_RERANK_MODEL'),
        concurrency=int(os.environ.get('CAREGUIDE_RERANK_CONCURRENCY', '8')),
        candidate_limit=min(_positive_integer('CAREGUIDE_RERANK_CANDIDATES', '20'), 20),
        provider_name='huawei-modelarts', timeout=30, max_retries=2,
    )


def chroma_client():
    import chromadb
    return chromadb.PersistentClient(path=str(PROJECT_ROOT/'data'/'chroma'),
                                    settings=chromadb.Settings(anonymized_telemetry=False))


def load_runtime_corpus(corpus_dir=None):
    """Load the exact corpus whose fingerprint is bound to the runtime index."""
    corpus_root = Path(corpus_dir or os.environ.get('CAREGUIDE_CORPUS_DIR')
                       or OPEN_CORPUS_ROOT)
    corpus = Corpus(corpus_root)
    from careguide.business_store import BusinessStore
    from careguide.knowledge_workflow import KnowledgeWorkflow
    store = BusinessStore(os.environ.get(
        'CAREGUIDE_BUSINESS_DB', str(PROJECT_ROOT/'data'/'runtime'/'medical.sqlite3')))
    return KnowledgeWorkflow(store).extend(corpus)


def load_hospital_corpus(corpus_dir=None):
    root = Path(corpus_dir or os.environ.get('CAREGUIDE_HOSPITAL_CORPUS_DIR')
                or HOSPITAL_CORPUS_ROOT)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return HospitalCorpus.load(root)


def build_hospital_knowledge(corpus=None, *, client=None):
    mode = os.environ.get(
        'CAREGUIDE_HOSPITAL_RETRIEVAL_MODE', 'disabled').strip().casefold()
    if mode not in {'disabled', 'bm25', 'hybrid'}:
        raise ValueError('Unknown hospital retrieval mode')
    if mode == 'disabled':
        return None, RetrievalRuntimeStatus(
            mode, 'disabled', None, None, None, None)
    corpus = corpus or load_hospital_corpus()
    if mode == 'bm25':
        knowledge = HospitalKnowledgeBase(corpus, mode='bm25')
        return knowledge, RetrievalRuntimeStatus(
            mode, 'bm25', None, None, None, None)

    client = client or chroma_client()
    name = hospital_collection_name(corpus)
    names = {getattr(collection, 'name', str(collection))
             for collection in client.list_collections()}
    if name not in names:
        raise ValueError('hospital_hybrid_index_unavailable')
    collection = client.get_collection(name, embedding_function=None)
    embedding = semantic_embedding()
    reranker = None
    if os.environ.get(
        'CAREGUIDE_HOSPITAL_RERANK_ENABLED', 'false').strip().casefold() == 'true':
        reranker = semantic_reranker()
    try:
        knowledge = HospitalKnowledgeBase(
            corpus,
            mode='hybrid',
            collection=collection,
            embedding=embedding,
            reranker=reranker,
            hybrid_candidates=min(_positive_integer(
                'CAREGUIDE_HOSPITAL_HYBRID_CANDIDATES', '20'), 20),
            min_rerank_score=float(os.environ.get(
                'CAREGUIDE_HOSPITAL_MIN_RERANK_SCORE', '0.35')),
        )
    except ValueError:
        close = getattr(embedding, 'close', None)
        if close is not None:
            close()
        raise ValueError('hospital_hybrid_index_invalid') from None
    return knowledge, RetrievalRuntimeStatus(
        requested_mode=mode,
        active_mode='hybrid',
        embedding_provider=embedding.provider_name,
        rerank_provider=getattr(reranker, 'provider_name', None),
        index_collection=name,
        degraded_reason=None,
    )


def build_retrieval_knowledge(corpus: Corpus, *, client=None):
    mode = os.environ.get('CAREGUIDE_RETRIEVAL_MODE', 'bm25').strip().casefold()
    if mode not in {'bm25', 'dense', 'hybrid'}:
        raise ValueError('Unknown retrieval mode')
    if mode == 'bm25':
        knowledge = MedicalKnowledgeBase(corpus, mode='bm25')
        return knowledge, RetrievalRuntimeStatus(
            mode, 'bm25', None, None, None, None)

    embedding = semantic_embedding()
    client = client or chroma_client()
    name = versioned_collection_name(corpus)
    names = {getattr(collection, 'name', str(collection))
             for collection in client.list_collections()}
    if name not in names:
        raise ValueError('hybrid_index_unavailable')
    collection = client.get_collection(name, embedding_function=None)
    reranker = None
    if os.environ.get('CAREGUIDE_RERANK_ENABLED', 'false').strip().casefold() == 'true':
        reranker = semantic_reranker()
    try:
        knowledge = MedicalKnowledgeBase(
            corpus, mode=mode, collection=collection, embedding=embedding,
            min_dense_score=float(os.environ.get('CAREGUIDE_MIN_DENSE_SCORE', '-1.0')),
            dense_weight=float(os.environ.get('CAREGUIDE_RRF_DENSE_WEIGHT', '4')),
            lexical_weight=float(os.environ.get('CAREGUIDE_RRF_BM25_WEIGHT', '1')),
            hybrid_candidates=min(_positive_integer('CAREGUIDE_HYBRID_CANDIDATES', '20'), 20),
            reranker=reranker,
            rerank_candidates=min(_positive_integer('CAREGUIDE_RERANK_CANDIDATES', '20'), 20),
        )
    except ValueError:
        raise ValueError('hybrid_index_invalid') from None
    status = RetrievalRuntimeStatus(
        requested_mode=mode, active_mode=mode,
        embedding_provider=embedding.provider_name,
        rerank_provider=getattr(reranker, 'provider_name', None),
        index_collection=name, degraded_reason=None)
    return knowledge, status


@lru_cache(maxsize=1)
def get_registry():
    # Keep Corpus() as the frozen V1 compatibility baseline.  The product
    # runtime defaults to the versioned open-data corpus and can still select
    # any frozen corpus explicitly through CAREGUIDE_CORPUS_DIR.
    corpus = load_runtime_corpus()
    knowledge, status = build_retrieval_knowledge(corpus)
    hospital_knowledge, hospital_status = build_hospital_knowledge()
    registry = MedicalToolRegistry(
        knowledge, hospital_knowledge=hospital_knowledge)
    registry.retrieval_status = status
    registry.hospital_retrieval_status = hospital_status
    return registry


@lru_cache(maxsize=1)
def get_chat_runtime():
    from careguide.agent_orchestrator import AgentOrchestrator
    from careguide.domain import AGENTS
    from careguide.intent_recognizer import IntentRecognizer
    from careguide.intent_v2_recognizer import V2IntentRecognizer
    from careguide.llm_utils import create_llm_client
    from careguide.performance_monitor import PerformanceMonitor
    from careguide.skill_registry import SkillRegistry
    from careguide.task_context import PersistentMemory, SessionMemory
    from careguide.trace_store import TraceStore
    from careguide.business_store import BusinessStore
    from careguide.action_store import ActionStore
    from careguide.appointment_service import AppointmentService
    from careguide.hospital_workflow import (DisabledHospitalBackend,
        FHIRHospitalBackend, InMemoryHospitalBackend)
    from careguide.knowledge_backends import EmbeddedKnowledgeBackend, WeKnoraMCPBackend
    key = os.environ.get('CAREGUIDE_LLM_API_KEY', '')
    base_url = os.environ.get('CAREGUIDE_LLM_BASE_URL') or 'https://api.xiaomimimo.com/v1'
    client = create_llm_client(key, base_url) if key and not key.startswith('YOUR_') else None
    model = os.environ.get('CAREGUIDE_LLM_MODEL', 'mimo-v2.6-flash')
    orchestration = os.environ.get('CAREGUIDE_ORCHESTRATION_MODE', 'unified').strip().casefold()
    if orchestration not in {'unified', 'langgraph', 'direct'}:
        raise ValueError('Unknown orchestration mode')
    if orchestration == 'unified':
        # Keep legacy recognition offline. UnifiedRuntime owns the explicit,
        # budgeted semantic-candidate phase before its single planner decision.
        recognizer = IntentRecognizer(embedding_mode='disabled')
    else:
        embedding_mode = os.environ.get('CAREGUIDE_INTENT_EMBEDDING_MODE', 'local_ngram')
        embedding = semantic_embedding() if embedding_mode == 'remote' else None
        taxonomy = os.environ.get('CAREGUIDE_INTENT_TAXONOMY', 'v1').strip().casefold()
        if taxonomy not in {'v1', 'v2', 'v3', 'qic'}:
            raise ValueError('Unknown intent taxonomy')
        if taxonomy == 'qic':
            from careguide.qic_intent_recognizer import QICIntentRecognizer
            recognizer_class = QICIntentRecognizer
        elif taxonomy == 'v3':
            from careguide.intent_v3_recognizer import V3IntentRecognizer
            recognizer_class = V3IntentRecognizer
        else:
            recognizer_class = V2IntentRecognizer if taxonomy == 'v2' else IntentRecognizer
        recognizer_kwargs = {'embedding': embedding, 'embedding_mode': embedding_mode}
        if taxonomy == 'qic':
            recognizer_kwargs.update({
                'llm_gate_margin': float(os.environ.get('CAREGUIDE_QIC_LLM_GATE_MARGIN', '0.40')),
                'llm_override_confidence': float(os.environ.get(
                    'CAREGUIDE_QIC_LLM_OVERRIDE_CONFIDENCE', '0.95')),
            })
            qic_backend = os.environ.get('CAREGUIDE_QIC_CLASSIFIER_BACKEND', 'fusion').strip().casefold()
            if qic_backend not in {'fusion', 'transformer'}:
                raise ValueError('Unknown CareGuide QIC classifier backend')
            if qic_backend == 'transformer':
                from careguide.transformer_intent_classifier import TransformerIntentClassifier
                model_path = os.environ.get('CAREGUIDE_QIC_MODEL_PATH', '').strip()
                if not model_path:
                    raise ValueError('Transformer QIC backend requires CAREGUIDE_QIC_MODEL_PATH')
                recognizer_kwargs['semantic_classifier'] = TransformerIntentClassifier(
                    model_path,
                    device=os.environ.get('CAREGUIDE_QIC_MODEL_DEVICE', 'auto').strip(),
                )
        recognizer = recognizer_class(client, model, **recognizer_kwargs)
    skills = SkillRegistry(root_dir=PROJECT_ROOT/'data'/'skills',
                           tool_scopes={agent.value: set(spec.tools) for agent, spec in AGENTS.items()})
    skills.load()
    trace_path = PROJECT_ROOT/'data'/'traces'/'requests.jsonl' if os.environ.get('CAREGUIDE_PERSIST_TRACES') == 'true' else None
    registry = get_registry()
    # Entity extraction and retrieval must inspect the same corpus.  The
    # recognizer constructors retain Corpus() for isolated V1/V2 tests.
    recognizer.corpus = registry.corpus
    knowledge_mode = os.environ.get('CAREGUIDE_KNOWLEDGE_BACKEND', 'embedded').strip().casefold()
    if knowledge_mode == 'weknora_mcp':
        knowledge_backend = WeKnoraMCPBackend(
            os.environ.get('CAREGUIDE_WEKNORA_MCP_URL', ''),
            token=os.environ.get('CAREGUIDE_WEKNORA_MCP_TOKEN', ''),
            search_tool=os.environ.get('CAREGUIDE_WEKNORA_SEARCH_TOOL', 'hybrid_search'),
            knowledge_base_id=os.environ.get('CAREGUIDE_WEKNORA_KB_ID', ''))
    elif knowledge_mode == 'embedded':
        knowledge_backend = EmbeddedKnowledgeBackend(registry.knowledge, list(registry.corpus.records))
    else:
        raise ValueError('Unknown knowledge backend')
    hospital_mode = os.environ.get('CAREGUIDE_HOSPITAL_BACKEND', 'disabled').strip().casefold()
    if hospital_mode == 'fhir':
        hospital_backend = FHIRHospitalBackend(os.environ.get('CAREGUIDE_FHIR_BASE_URL', 'http://127.0.0.1:8080/fhir'))
    elif hospital_mode == 'memory':
        hospital_backend = InMemoryHospitalBackend()
    elif hospital_mode == 'disabled':
        hospital_backend = DisabledHospitalBackend()
    else:
        raise ValueError('Unknown hospital backend')
    business_path = os.environ.get('CAREGUIDE_BUSINESS_DB', str(PROJECT_ROOT/'data'/'runtime'/'medical.sqlite3'))
    appointment_service = AppointmentService(hospital_backend, ActionStore(business_path))
    engine = AgentOrchestrator(recognizer, registry, client=client, model=model,
        skills=skills, traces=TraceStore(trace_path),
        max_tool_rounds=int(os.environ.get('CAREGUIDE_MAX_TOOL_ROUNDS', '5')),
        business_store=BusinessStore(business_path), knowledge_backend=knowledge_backend,
        appointment_service=appointment_service)
    memory_mode = os.environ.get('CAREGUIDE_MEMORY_MODE', 'session')
    if memory_mode == 'redis_chroma':
        from careguide.conversation_memory import MemoryManager
        manager = MemoryManager(redis_url=os.environ.get('CAREGUIDE_REDIS_URL', 'redis://127.0.0.1:6380/0'),
            chroma_host=os.environ.get('CAREGUIDE_CHROMA_HOST', '127.0.0.1'),
            chroma_port=int(os.environ.get('CAREGUIDE_CHROMA_PORT', '8002')),
            chroma_path=str(PROJECT_ROOT/'data'/'chroma_memory'), api_key=key,
            base_url=base_url, model=model, client=client,
            embedding_function=semantic_embedding() if os.environ.get('CAREGUIDE_MEMORY_EMBEDDING_MODE') == 'remote' else None,
            collection_prefix='careguide_semantic' if os.environ.get('CAREGUIDE_MEMORY_EMBEDDING_MODE') == 'remote' else 'careguide',
            profile_path=business_path)
        memory = PersistentMemory(manager)
    elif memory_mode == 'session':
        memory = SessionMemory()
    else:
        raise ValueError('Unknown memory mode')
    if orchestration in {'unified', 'langgraph'}:
        from careguide.orchestration.runtime import OrchestrationRuntime
        from careguide.orchestration.budget import BudgetLimits
        runtime_class, runtime_options = OrchestrationRuntime, {}
        if orchestration == 'unified':
            from careguide.orchestration.unified import UnifiedRuntime
            from careguide.orchestration.understanding import LocalUnderstanding, LocalClassifier
            local_path = os.environ.get('CAREGUIDE_QIC_MODEL_PATH') or str(
                PROJECT_ROOT.parent/'models'/'CareGuide'/'careguide-intent-roberta-v1')
            classifier = LocalClassifier(local_path, os.environ.get('CAREGUIDE_QIC_MODEL_DEVICE', 'auto'))
            runtime_class = UnifiedRuntime
            semantic, semantic_mode = intent_semantic_retriever()
            runtime_options['understanding'] = LocalUnderstanding(registry.corpus, classifier,
                semantic=semantic, semantic_mode=semantic_mode,
                hospital_knowledge=getattr(registry, 'hospital_knowledge', None))
        engine = runtime_class(engine, business_path, **runtime_options, limits=BudgetLimits(
            model=int(os.environ.get('CAREGUIDE_WORKFLOW_MODEL_CALLS', '20')),
            tool=int(os.environ.get('CAREGUIDE_WORKFLOW_TOOL_CALLS', '30')),
            retry=int(os.environ.get('CAREGUIDE_WORKFLOW_RETRIES', '3')),
            replan=int(os.environ.get('CAREGUIDE_WORKFLOW_REPLANS', '3')),
            execution_seconds=float(os.environ.get('CAREGUIDE_WORKFLOW_EXECUTION_SECONDS', '120')),
            request_seconds=float(os.environ.get('CAREGUIDE_WORKFLOW_REQUEST_SECONDS', '45'))))
    monitor = PerformanceMonitor(engine, engine.tool_manager, webhook_url=None)
    return engine, memory, monitor
