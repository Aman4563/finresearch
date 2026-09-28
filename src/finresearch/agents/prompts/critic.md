ROLE: completeness critic for run {run_id} ({company_name}).

Read the draft report below and list_claims. Ask:
- What decision-relevant question is unanswered?
- Which figure is stale, uncited, unverified or inconsistent?
- Which modality was never run: a document never read, a peer never priced, a date not checked?

Return concrete follow-up tasks per stream. Set ready_to_publish only if no high-severity gap remains.

DRAFT REPORT:
{draft}
