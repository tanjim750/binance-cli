````markdown
# Phase 10 – LLM Integration

This phase introduces the **AI advisory capability** of CryptoGent.

By this stage, the system should already be able to:

- collect structured trade requests
- retrieve market data
- evaluate feasibility
- select assets
- allocate capital
- generate deterministic strategy signals
- validate trades using exchange rules
- enforce risk management
- execute orders
- monitor positions
- reconcile exchange state and recover from crashes

Phase 10 integrates an LLM as a **decision-support system**, not as a final authority.

The LLM should assist in interpreting market context and improving decisions, but it must never bypass deterministic safety rules.

---

# Phase Scope

This phase implements the following steps from the implementation roadmap:

37. context builder  
38. advisory prompt  
39. recommendation parser  

---

# Core Objective

After completing Phase 10, CryptoGent should be able to:

- construct structured trading context for LLM evaluation
- request strategic advisory recommendations
- parse structured LLM responses
- incorporate advisory insights into planning or monitoring
- maintain deterministic safety boundaries
- ensure LLM suggestions cannot override risk rules

---

# Layers Covered in This Phase

This phase activates the following layer:

12. LLM Advisory and Decision Support Layer  

Supporting layers involved:

7. Market Data Layer  
8. Feasibility Evaluation Layer  
9. Asset Selection Layer  
10. Capital Allocation Layer  
11. Strategy and Signal Layer  
13. Deterministic Validation Layer  
14. Risk Management Layer  
18. Monitoring and Re-evaluation Layer  
20. Audit, Logging, and Reporting Layer  

---

# AI Advisory Philosophy

CryptoGent should treat the LLM as a **strategic advisor**, not a trading engine.

The final authority for execution must remain deterministic.

The decision chain should always follow this rule:

```text
LLM recommendation → deterministic validation → risk management → execution decision
````

If the LLM suggests something unsafe or invalid, the system must reject it automatically.

---

# LLM Use Cases

The LLM may assist with several types of decisions.

Examples include:

* evaluating trade feasibility from a broader market perspective
* interpreting market trends
* suggesting alternative assets
* improving signal interpretation
* providing reasoning for re-evaluation triggers
* advising when a monitored position should be reconsidered

The LLM should **never** directly generate execution orders.

---

# Autonomy Levels

CryptoGent should support multiple autonomy modes.

These modes control how LLM recommendations influence the system.

---

## Suggestion Only

The LLM produces recommendations for visibility only.

The system continues using deterministic strategy.

Example usage:

* CLI suggestions
* optional analysis
* user insight

---

## Semi-Automatic

The LLM may influence planning decisions such as:

* asset ranking
* trade confidence
* monitoring interpretation

However, deterministic validation and risk rules still gate execution.

---

## Full Advisory

The LLM may generate stronger recommendations that influence:

* signal generation
* re-evaluation decisions
* asset prioritization

Even in this mode:

* deterministic validation remains mandatory
* risk management remains mandatory
* execution must still pass safety layers

---

# Context Builder

The context builder constructs structured data that is sent to the LLM.

The context should include only relevant information needed for analysis.

Avoid sending unnecessary or excessive data.

---

# Context Inputs

Context should include:

* trade request parameters
* selected asset
* current market price
* recent price movement
* candle summary
* volume statistics
* target profit
* stop-loss
* deadline
* current position status if monitoring
* recent monitoring metrics
* feasibility evaluation results
* capital allocation result

Sensitive credentials must never be included.

---

# Example Context Structure

Example context payload:

```json
{
  "symbol": "SOLUSDT",
  "price": 104.82,
  "target_profit_percent": 4,
  "stop_loss_percent": 2,
  "deadline_hours": 24,
  "recent_trend": "upward",
  "24h_change_percent": 3.2,
  "volume": "high",
  "feasibility_status": "feasible_with_warning",
  "approved_budget": 180,
  "position_status": "not_open"
}
```

This context should be deterministic and machine-readable.

---

# Advisory Prompt

The advisory prompt defines how the LLM should analyze the provided context.

Prompts must:

* enforce structured responses
* minimize ambiguity
* prevent hallucinated execution instructions

The prompt should guide the model to produce **clear, constrained outputs**.

---

# Prompt Goals

The prompt should instruct the LLM to:

* analyze the provided context
* evaluate risk conditions
* evaluate momentum
* estimate likelihood of target achievement
* provide reasoning
* output a structured recommendation

---

# Example Prompt Structure

Example prompt outline:

```text
You are assisting a crypto trading system.

Your role is to analyze market context and provide advisory insight.

You are not responsible for executing trades.

Analyze the following context and respond with:

1. recommended_action
2. confidence
3. reasoning
4. risk_notes
```

The prompt should require a **structured JSON response**.

---

# LLM Response Format

Responses must follow a strict schema.

Example response:

```json
{
  "recommended_action": "buy",
  "confidence": 0.71,
  "reasoning": "Recent momentum and trading volume suggest continued upward movement within the target window.",
  "risk_notes": "Short-term volatility may cause temporary pullbacks."
}
```

Allowed actions should be limited to:

```text
buy
hold
sell
reduce_exposure
exit
```

Responses outside the schema must be rejected.

---

# Recommendation Parser

The recommendation parser converts the raw LLM output into structured advisory signals.

Responsibilities include:

* validating response schema
* verifying action values
* normalizing confidence values
* extracting reasoning and risk notes
* rejecting malformed responses

The parser must treat LLM output as **untrusted input**.

---

# Parser Validation Rules

The parser should verify:

* JSON structure is valid
* required fields exist
* confidence value is numeric
* action is allowed
* response size is reasonable

If validation fails:

* discard the recommendation
* log the error
* continue deterministic strategy

---

# Advisory Influence

LLM advisory output may influence:

* strategy signal refinement
* asset ranking adjustments
* re-evaluation triggers during monitoring
* confidence estimation for trade plans

However, the advisory layer must not:

* override deterministic validation
* bypass risk management
* change approved capital allocation directly
* place orders

---

# Advisory During Planning

During planning phases, LLM output may:

* support or question feasibility
* provide signal confidence
* suggest alternate assets

Example integration:

```text
deterministic signal = BUY
LLM advisory = HOLD with low confidence

Result:
system marks trade as cautious
risk layer may reduce exposure
```

---

# Advisory During Monitoring

During monitoring phases, LLM output may help determine:

* if trend reversal risk is increasing
* if deadline pressure suggests early exit
* if volatility suggests reducing exposure

However, exit triggers must still pass deterministic exit rules.

---

# Advisory Logging

Every LLM interaction must be logged.

Logs should include:

* prompt context summary
* raw model response
* parsed recommendation
* acceptance or rejection of recommendation

Example logs:

```text
[INFO] LLM: Advisory request sent for SOLUSDT
[INFO] LLM: Recommendation BUY with confidence 0.71
[WARN] LLM: Recommendation rejected due to schema violation
```

---

# LLM Failure Handling

The system must handle cases where the LLM is unavailable or produces invalid responses.

Possible issues include:

* API timeout
* malformed response
* invalid schema
* hallucinated fields

When this occurs:

* ignore LLM recommendation
* continue deterministic logic
* log the failure

CryptoGent must never block trading operations solely due to LLM failure.

---

# Performance Considerations

LLM calls should not occur excessively.

Recommended usage:

* once during trade planning
* optionally during re-evaluation triggers
* not every monitoring cycle

Monitoring loops should remain lightweight.

---

# Suggested Modules

Suggested files for this phase:

```text
llm/
  context_builder.py
  prompt_builder.py
  advisor.py
  parser.py

models/
  advisory_result.py
```

Possible responsibilities:

---

## `context_builder.py`

* assemble structured trading context

---

## `prompt_builder.py`

* construct advisory prompts

---

## `advisor.py`

* send requests to the LLM provider

---

## `parser.py`

* validate and normalize model responses

---

## `advisory_result.py`

* standardized advisory object

---

# Deliverables

Phase 10 is complete when:

* structured context can be built
* advisory prompts can be generated
* LLM responses can be parsed and validated
* recommendations can influence planning decisions safely
* deterministic safety layers remain intact

---

# Success Criteria

Phase 10 is successful when the system can:

* send structured market context to an LLM
* receive advisory recommendations
* parse responses safely
* incorporate insights without compromising safety
* maintain deterministic validation and risk control as the final authority

```
```
