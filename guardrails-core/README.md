# NeMo Guardrails — Technical Architecture and Core Concepts

This document serves as an engineering revision reference covering the operational mechanics, architecture, and syntax of NVIDIA NeMo Guardrails.

---

## 1. Core Problem: Why System Prompts Fail

In standard LLM deployments, developers rely on system prompts:
```text
"You are an enterprise HR assistant. Never discuss off-topic queries or reveal salaries."
```

System prompting alone is vulnerable because:
* Non-deterministic: LLMs predict probabilities, not hard rules.
* Jailbreak susceptibility: Techniques such as role-play ("You are DAN"), hypothetical scenarios, or payload splitting can bypass instructions.
* State blindness: A prompt cannot enforce strict state machine execution across multi-turn dialogs.
* High cost and latency: Adding extensive negative constraints ("Do not do X, Y, Z...") consumes context window tokens on every query.

NeMo Guardrails addresses this by placing a programmable, event-driven state machine between the user and the language model.

---

## 2. Complete Execution Lifecycle

When a user submits a prompt, the message traverses four distinct inspection layers:

```
[User Input]
     |
     v
+-------------------------------------------------------------------+
| 1. Systematic Input Rails (Python Actions)                        |
|    - Pattern matching (Regex for PII, SSN, API tokens, emails)   |
|    - Deterministic: 0ms LLM latency, zero hallucination           |
+-------------------------------------------------------------------+
     | (Passed)
     v
+-------------------------------------------------------------------+
| 2. Semantic Input Rails (Guard LLM + Colang Flows)                |
|    - Maps raw input to Canonical Form (e.g., "user ask off topic")|
|    - Matches against Colang State Machine                         |
|    - If blocked: Halts pipeline immediately, returns canned text  |
+-------------------------------------------------------------------+
     | (Passed / Sentinel QUERY_PASSED)
     v
+-------------------------------------------------------------------+
| 3. Domain Logic & Generation (RAG + Generation LLM)               |
|    - Vector retrieval from FAISS index                            |
|    - Grounded generation using retrieved context only             |
+-------------------------------------------------------------------+
     |
     v
+-------------------------------------------------------------------+
| 4. Output Rails (Sanitizers & Verifiers)                          |
|    - Validates generated response before delivering to user       |
|    - Blocks hallucinated secrets, credentials, or PII             |
+-------------------------------------------------------------------+
     |
     v
[Delivered Response]
```

---

## 3. Colang Language Specification

Colang (`.co`) is NVIDIA's domain-specific modeling language designed specifically for conversational safety rules. It operates on three fundamental primitives.

### Primitive 1: `define user` (Canonical Forms)
Defines semantic user intents by providing few-shot representative examples. NeMo uses these examples to classify incoming queries.

```colang
define user ask off topic
  "tell me a joke"
  "what is the capital of France"
  "write me python code for binary search"
  "who won the football match"
```

How classification works:
* At runtime, NeMo provides the query and canonical candidates to the Guard LLM.
* The Guard LLM returns the matching canonical identifier (e.g., `user ask off topic`).
* The system transitions state based on the identified intent.

### Primitive 2: `define bot` (Predefined Responses)
Defines deterministic bot outputs. These can be exact strings, templates, or prefixed protocol tags.

```colang
define bot refuse off topic
  "[RAIL_BLOCKED:OFF_TOPIC] I am an enterprise HR assistant and can only assist with company policies."
```

Key Advantage: The generation LLM is never invoked for blocked queries. The response is returned directly from memory.

### Primitive 3: `define flow` (State Transitions)
Defines the sequential interaction logic and execution paths.

```colang
define flow handle off topic
  user ask off topic
  bot refuse off topic
  stop
```

Keywords in flows:
* `stop`: Immediately halts further rail processing and terminates the turn.
* `continue`: Resumes default execution after executing side effects.
* `execute <action>`: Calls a registered asynchronous Python function.

---

## 4. Python Action Integration (`@action`)

Python actions allow programmatic logic to run inside Colang flows.

### Action Definition (Python):
```python
from nemoguardrails.actions import action

@action(is_system_action=True)
async def detect_pii(context: dict = None):
    msg = context.get("user_message", "")
    patterns = {
        "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
        "api_key": r"(?i)(api_key|token)[:\s]+[A-Za-z0-9_\-]{10,}",
    }
    found = [k for k, p in patterns.items() if re.search(p, msg)]
    return len(found) > 0
```

### Action Invocation (Colang):
```colang
define bot ask to remove pii
  "[RAIL_BLOCKED:PII] Sensitive information detected. Please remove personal identifiers."

define flow check input for pii
  $pii_found = execute detect_pii
  if $pii_found
    bot ask to remove pii
    stop
```

---

## 5. Dual-LLM Architecture

In enterprise deployments, running a massive frontier model for both safety classification and generation is suboptimal for cost and latency.

| Role | Target Model Type | Typical Models | Responsibilities |
|---|---|---|---|
| LLM 1 (Guard) | Fast, deterministic, safety-tuned | Llama-3.1-8B, Llama-Guard, GPT-OSS-120B | Intent classification, canonical mapping, jailbreak detection |
| LLM 2 (Chat) | High-reasoning, instruction-following | Llama-3.3-70B, GPT-4o, GPT-OSS-20B | Context synthesis, policy grounding, answer generation |

### Runtime Implementation:
```python
from nemoguardrails import RailsConfig, LLMRails
from langchain_groq import ChatGroq

guard_llm = ChatGroq(api_key=key, model="openai/gpt-oss-120b", temperature=0)
config = RailsConfig.from_content(colang_content=COLANG, yaml_content=YAML)

# Guard LLM bound strictly to NeMo runtime
rails = LLMRails(config, llm=guard_llm)
```

---

## 6. Integrating NeMo Guardrails with RAG

There are two primary integration patterns:

### Pattern A: Internal Actions (NeMo Controls Retrieval)
NeMo executes retrieval as an internal action and invokes the main model directly within Colang.
* Advantage: Fully self-contained.
* Disadvantage: Harder to customize advanced hybrid search, reranking, or custom UI stream rendering.

### Pattern B: Sentinel Pass-Through (Recommended for Production)
NeMo acts as an ingress firewall. If all guardrails pass, it emits a unique token (e.g., `QUERY_PASSED`). The external pipeline intercepts this token and proceeds with vector retrieval.

```colang
define user ask hr question
  "how many vacation days do I get"
  "what is the parental leave policy"

define bot query passed
  "QUERY_PASSED"

define flow answer hr question
  user ask hr question
  bot query passed
  stop
```

Application Pipeline:
```python
text, is_blocked, reason, is_dialog, needs_rag = parse_nemo_response(raw)

if is_blocked:
    return text  # Predefined refusal string

if is_dialog:
    return text  # Instant scripted greeting

if needs_rag:
    chunks = vectorstore.similarity_search(query, k=3)
    return chat_llm.generate(prompt=query, context=chunks)
```

---

## 7. Key Defense Scenarios Implemented

* Off-Topic Queries:
  Queries unrelated to enterprise scope are diverted before reaching domain vector indexes.
* Jailbreak / Role Reversal:
  Direct attacks such as "Ignore rules", "Developer Mode", or "DAN" triggers predefined refusal states.
* Confidential Data Isolation:
  Attempts to query individual peer compensation or records are trapped and deflected to official human HR channels.
* PII & Secret Leakage:
  Inbound SSNs, tokens, or emails are intercepted by regex actions; outbound credentials or compensation leaks are scrubbed before stream delivery.

