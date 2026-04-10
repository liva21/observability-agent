"""
prompts.py — Prompt templates for the LLM root cause analysis.

Two prompts:
  1. SYSTEM_PROMPT — sets the agent persona and output contract
  2. build_analysis_prompt() — fills in the structured context

Design principles:
  - Chain-of-thought encouraged (think step by step)
  - Strict JSON output contract defined in system prompt
  - Context ordered by signal strength (errors first, then metrics)
  - Explicit instruction to acknowledge uncertainty
"""

import json


SYSTEM_PROMPT = """You are an expert Site Reliability Engineer (SRE) specializing in
distributed systems observability and incident response.

Your job: analyze log evidence and metrics, then produce a structured root cause
analysis in valid JSON — nothing else.

Rules:
1. Base your analysis ONLY on the evidence provided. Do not fabricate metrics.
2. If evidence is insufficient, say so in root_cause and set confidence < 0.4.
3. Think step by step before writing the JSON.
4. Your JSON must exactly match this schema — no extra keys, no markdown fences:

{
  "root_cause": "<one concise sentence>",
  "confidence": <0.0-1.0>,
  "severity": "<low|medium|high|critical>",
  "affected_services": ["<service>", ...],
  "affected_users_estimate": "<string or 'unknown'>",
  "timeline": ["<earliest event>", ..., "<latest event>"],
  "contributing_factors": ["<factor>", ...],
  "recommended_action": "<immediate action in one sentence>",
  "remediation_steps": ["<step 1>", "<step 2>", ...],
  "escalate_to": "<team or role, empty string if not needed>"
}
"""


def build_analysis_prompt(
    service_name: str,
    anomaly_score: float,
    severity: str,
    window_features: dict,
    detector_results: list[dict],
    context,           # ServiceContext
) -> str:
    """
    Build the user-turn prompt combining anomaly data + fetched context.
    Returns a single string sent as the user message.
    """
    sections: list[str] = []

    # ── Section 1: Anomaly summary ──────────────────────────────────
    sections.append(f"""=== ANOMALY ALERT ===
Service:       {service_name}
Score:         {anomaly_score:.3f} / 1.000
Severity:      {severity.upper()}
Window:        {window_features.get('window_seconds', 60)}s""")

    # ── Section 2: Window metrics ────────────────────────────────────
    wf_lines = [
        f"  error_rate:      {window_features.get('error_rate', 0):.1%}",
        f"  error_count:     {window_features.get('error_count', 0)}",
        f"  critical_count:  {window_features.get('critical_count', 0)}",
        f"  latency_p50:     {window_features.get('latency_p50', 0):.1f} ms",
        f"  latency_p95:     {window_features.get('latency_p95', 0):.1f} ms",
        f"  latency_p99:     {window_features.get('latency_p99', 0):.1f} ms",
        f"  logs_per_second: {window_features.get('logs_per_second', 0):.1f}",
    ]
    sections.append("=== WINDOW METRICS ===\n" + "\n".join(wf_lines))

    # ── Section 3: Detector findings ────────────────────────────────
    det_lines = []
    for dr in detector_results:
        triggered = ", ".join(dr.get("triggered_on", [])) or "none"
        det_lines.append(
            f"  [{dr.get('detector_name', '?')}]  "
            f"score={dr.get('score', 0):.3f}  "
            f"triggered_on={triggered}"
        )
    sections.append("=== DETECTOR SIGNALS ===\n" + "\n".join(det_lines))

    # ── Section 4: Recent error logs ────────────────────────────────
    if context.error_logs:
        error_sample = context.error_logs[:15]
        sections.append(
            "=== RECENT ERROR LOGS (newest first) ===\n"
            + "\n".join(f"  {l}" for l in error_sample)
        )
    else:
        sections.append("=== RECENT ERROR LOGS ===\n  (none captured)")

    # ── Section 5: General recent logs ──────────────────────────────
    if context.recent_logs:
        log_sample = context.recent_logs[:10]
        sections.append(
            "=== RECENT LOGS (sample) ===\n"
            + "\n".join(f"  {l}" for l in log_sample)
        )

    # ── Section 6: Prometheus metrics ───────────────────────────────
    if context.metrics:
        m = context.metrics.get("metrics", {})
        met_lines = [
            f"  {k}: {v:.3f}" if v >= 0 else f"  {k}: unavailable"
            for k, v in m.items()
        ]
        sections.append("=== PROMETHEUS METRICS ===\n" + "\n".join(met_lines))

    # ── Section 7: Dependency graph ──────────────────────────────────
    dep_lines = [
        f"  calls:      {', '.join(context.dependencies) or 'none'}",
        f"  called_by:  {', '.join(context.dependents) or 'none'}",
    ]
    sections.append("=== SERVICE DEPENDENCIES ===\n" + "\n".join(dep_lines))

    # ── Final instruction ────────────────────────────────────────────
    sections.append(
        "Think step by step about the above evidence, then output "
        "ONLY the JSON root cause analysis. No preamble, no markdown."
    )

    return "\n\n".join(sections)
