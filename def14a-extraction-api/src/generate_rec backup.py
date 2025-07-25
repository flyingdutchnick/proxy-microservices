# lambdas/generate_rec.py
"""
Lambda: generateRec
Produces a structured voting recommendation for ONE proxy-question.

Expected event (Map iterator supplies it):
{
  "question_id": "1a",
  "filing_id":   42,                  # <-- needed for RAG search
  "company_name": "ACME Corp",
  "proxy_question": "Approve executive compensation …",
  "policy_context": "... full text of policy rules …",
  "past_votes_context": "... record …",
  "formatted_date": "June 15 2025"
}
Returned event:
{ …same fields…, "recommendation": { …VotingRecommendation… } }
"""
import os, json, logging, textwrap
from typing import Literal, List

import psycopg
from pgvector.psycopg import register_vector, Vector
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

# ------------------------------------------------------------------ #
# 0.  Config
# ------------------------------------------------------------------ #
PG_DSN        = os.environ["PG_DSN"]
OPENAI_MODEL  = "gpt-4.1-mini"           # same tier your TS code used
EMBED_MODEL   = "text-embedding-3-small"
RAG_TOP_K     = 20
client        = OpenAI()                 # uses OPENAI_API_KEY env var

DEFAULT_POLICY_RULES = ["Do not support a related-party transaction where the disclosure is insufficient to assess the proposed transaction and its impact on minority shareholders.", "Do not support a share issuance that does not create long-term value for shareholders or does not treat all shareholders fairly.", "In developed markets, do not support a general authority to issue shares without pre-emptive rights above 20 percent of currently issued capital.", "Do not support a general authority to issue shares with pre-emptive rights if the size of the issuance is considered excessive relative to currently issued capital.", "Do not support changes to a company’s governing documents that are not in the best interest of shareholders.", "Support proposals requiring shareholder approval to adopt or amend anti-takeover measures.", "In emerging markets, do not support a general authority to issue shares without pre-emptive rights above 30 percent of currently issued capital.", "Do not support mergers, acquisitions, and other corporate transactions that do not create long-term value for shareholders.", "Support the abolition of a class of common stock with unequal voting rights on equitable terms.", "Do not support proposals on a company’s climate transition plan (‘Say on Climate’) where the plan does not meet NBIM core expectations on climate change or substantive guidance on transition plans.", "Do not support the introduction of voting caps.", "Support a proposal to de-bundle all agenda items.", "Support the right of shareholders to request a general meeting.", "Support the right of shareholders representing 50 percent of outstanding shares to act by written consent, i.e. act without calling a formal meeting.", "Do not support changes to a company’s governing documents if there is lack of disclosure.", "Support the right of shareholders to request a general meeting with a minimum ownership threshold of between 10 and 25 percent.", "Do not support a bundled agenda item unless all included items are acceptable.", "Support abolition of class of common stock with unequal voting rights on equitable terms.", "Do not support the creation of new or additional classes of common stock with unequal voting rights.", "Do not support creation of new or additional classes of common stock with unequal voting rights.", "Do not support remuneration policy/report where outcomes could prove unusually costly and incentive structure does not clearly align with shareholders’ interests.", "Do not support a remuneration policy or report where the accelerated vesting arrangement fails to meet local-market best practice.", "Do not support remuneration policy/report where pension arrangements are considered excessive in the local market.", "Do not support a remuneration policy or report with clear misalignment between pay and long-term value creation.", "Do not support a remuneration policy or report with significant concerns over one-off payments, including golden hellos, golden parachutes, or severance payments.", "Do not support re-election of a director or entire board if there are material failures in oversight, management, or disclosure of climate risks.", "Do not support remuneration policy or report if board received low shareholder support for most recent pay-related proposal and failed to address concern. Shareholder support below 85% raises concerns despite what may be claimed in the proxy statement.", "Do not support re-election of a director or entire board if shareholders have experienced unsatisfactory financial performance or there is a lack of faith in board strategy.", "Do not support a remuneration policy or report with significant concerns over its structure.", "Do not support re-election of a director or entire board if there are material failures in oversight, management, or disclosure of social risks.", "Do not support a remuneration policy or report where the vesting or holding period fails to meet local-market best practice.", "Do not support re-election of a director or entire board if there are material failures in oversight, management, or disclosure of environmental risks.", "Do not support a proposal to discharge a director or the board of liability for their activities if there is significant concern regarding board actions in previous years, such as misstatements, large goodwill write-offs, or material legal actions.", "Support a proposal to introduce proxy access with holding requirements of up to three years and up to three percent ownership.", "Do not support re-election of nomination/governance committee members, or other directors, if the board amended governing documents without shareholder approval.", "Do not support election of chair of nomination committee in emerging markets if board does not include at least one director of each gender.", "Do not support re-election of remuneration committee members or the board if they received low shareholder support for pay-related proposals and failed to address concern.", "Do not support a proposal to classify (stagger) the board.", "Support a proposal to introduce majority requirements in director elections.", "Do not support election of a director whose service on other boards has been associated with misconduct.", "Do not support re-election of audit committee members or other directors if the company’s financial statements received an adverse opinion or auditor flagged material weaknesses, and concerns were not adequately addressed.", "Support a proposal to declassify (de-stagger) the board.", "Do not support re-election of nomination/governance committee members, or other directors, if the company failed to act on material requests from shareholders that received majority support the previous year.", "Do not support re-election of a director or entire board if there have been material failures of governance or risk oversight, or breaches of fiduciary responsibility.", "Support a proposal to shorten the election term of a director, with a preference for a one-year term.", "Do not support the election of a director whose name has not been disclosed in the proxy information or where disclosure is insufficient for suitability assessment.", "Support a proposal to eliminate cumulative voting only if the company allows for shareholder input on director elections.", "Do not support the election of a director with a term longer than local-market best practice.", "Do not support re-election of chair of nomination committee in developed markets if board does not include at least two members of each gender.", "Do not support re-election of chair of nomination committee in developed markets if there is not at least one non-executive director with relevant industry experience.", "Vote against a board member who has has attended 50 percent or less of the board meetings in a single year", "Do not support the election of a director at a developed market company who sits on more than five boards, holds more than two board chairs, or otherwise has too many board or management roles to fulfil responsibilities effectively.", "Do not support the re-election of the chair of the nomination committee at companies in developed markets if the board does not include at least two members of each gender.", "Do not support the re-election of the chair of the nomination committee at a developed market company unless there is at least one non-executive director who has worked in the company’s industry.", "Support proposals to separate the roles of chairperson and CEO."]

# ------------------------------------------------------------------ #
# 1.  Pydantic schema  (mirror of the Zod schema)
# ------------------------------------------------------------------ #
class VotingRecommendation(BaseModel):
    question_id: str
    proxy_question: str
    voting_recommendation: Literal["For", "Against", "Abstain", "Not Stated"]
    rationale: str
    citation: str
    confidence: float = Field(ge=0, le=1)

# ------------------------------------------------------------------ #
# 2.  Helpers: embed + multi-vector search
# ------------------------------------------------------------------ #
def _embed_queries(queries: List[str]) -> List[List[float]]:
    res = client.embeddings.create(
        input=queries,
        model=EMBED_MODEL,
    )
    return [d.embedding for d in res.data]

def _multi_vector_search(conn, filing_id: int, q_embeds: List[List[float]], k: int):
    """
    Returns top-k distinct chunk_texts for a list of query embeddings.
    """
    register_vector(conn)      # idempotent
    seen, context = set(), []
    with conn.cursor() as cur:
        for e in q_embeds:
            cur.execute(
                """
                SELECT chunk_index, chunk_text
                FROM   proxy_chunks
                WHERE  filing_id = %s
                ORDER  BY embedding <#> %s          -- cosine distance
                LIMIT  %s
                """,
                (filing_id, Vector(e), k),
            )
            for idx, txt in cur.fetchall():
                if idx not in seen:
                    seen.add(idx)
                    context.append(txt)
    return context[:k]

# ------------------------------------------------------------------ #
# 3.  Lambda handler
# ------------------------------------------------------------------ #
def handler(event, context):
    try:
        # ---- 3.1  Retrieve RAG context from proxy_chunks ----
        with psycopg.connect(PG_DSN) as conn:
            embeds = _embed_queries([event["proxy_question"]])
            rag_chunks = _multi_vector_search(conn,
                                              event["filing_id"],
                                              embeds,
                                              RAG_TOP_K)
        context_blob = "\n\n".join(rag_chunks)
        policy_rules = event.get("policy_context") or "\n".join(DEFAULT_POLICY_RULES)

        # ---- 3.2  Build prompts ----
        dev_prompt = textwrap.dedent(f"""
        Given the text of a proxy voting question, analyze the official proxy statement and **the following voting policy rules** to produce a structured voting recommendation.
        Ignore the board recommendation and develop an independent analysis based on the facts presented in the proxy statement and the policy rules.
        This is for a shareholder meeting to be held on {event['formatted_date']}.

        Relevant Voting Policy Rules:
        {policy_rules}

        Follow these steps:
        1. Analyze the Proxy Statement
           - Extract key facts from the proxy (see context below). Do *not* invent facts.
        2. Apply Policy Criteria
           - Identify the relevant policies and check compliance.
        3. Synthesize a Recommendation
           - Decide “For”, “Against”, “Abstain”, or “Not Stated”.
           - Justify your decision, linking facts to policy rules.
           - If “Against” or “Abstain”, cite the exact policy breach.

        Expected Output:
        Return **only** this JSON object:
        {{
          "question_id": "string",
          "proxy_question": "string",
          "voting_recommendation": "For | Against | Abstain | Not Stated",
          "rationale": "string",
          "citation": "string",
          "confidence": 0-1 float
        }}

        No commentary outside the JSON!
        """)

        user_prompt = (f"Generate a voting recommendation for "
                       f"question {event['question_id']}: {event['proxy_question']}")

        # ---- 3.3  Call OpenAI responses.parse ----
        resp = client.responses.parse(
            model=OPENAI_MODEL,
            input=[
                {"role": "system",    "content": dev_prompt},
                {"role": "assistant", "content": context_blob},
                {"role": "user",      "content": user_prompt},
            ],
            text_format=VotingRecommendation,     # Pydantic schema enforces shape
            temperature=0.1,
        )

        parsed: VotingRecommendation | None = resp.output_parsed
        if parsed is None:
            raise RuntimeError("Structured output was null")

        # ---- 3.4  Pass forward for StoreRec ----
        event["recommendation"] = parsed.model_dump()
        return event

    # ---------- Error handling ----------
    except (json.JSONDecodeError, ValidationError) as ve:
        logging.exception("Parse/validation error")
        raise RuntimeError(f"Failed to parse model output: {ve}")
    except Exception:
        logging.exception("Unexpected error in generate_rec")
        raise



# def extract_filer_name(cik: str | int):
#     """
#     Ignore *html* and look the registrant up in companies.json.

#     Returns `(filer_name_or_None, is_issuer_flag_or_None)`.
#     Because the name comes from the official CIK mapping, it’s always
#     the issuer’s own name, so `is_issuer` is True when we have a hit.
#     """
#     info = get_company_info(cik)          # uses your existing mapping
#     if not info:
#         return None, None

#     filer_name = info["name"]
#     # By definition this matches the issuer
#     return filer_name



# 2. Rules-based labeler: can add more cases if needed.
def rules_based_label(form_type, header_text=None):
    form = form_type.upper().replace(" ", "")
    if form == "DEF14A":
        return ("proxy_statement", "original")
    if form == "DEFR14A":
        return ("proxy_statement", "revision")
    if form == "DEFA14A":
        return ("proxy_supplement", "supplement")
    if form in ["DEFC14A", "PREC14A", "PRRN14A"]:
        return ("contestant_proxy", "original")
    return ("other", "other")
