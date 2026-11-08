"""统一身份与出现次数的进展；措辞或判定降级不等于修复。"""
from services.issue_identity import identity_key, problem_counts, occurrence_id


def problem_keys(findings):
    return {identity_key(f) for f in findings or [] if isinstance(f, dict)}


def review_progress(previous, current_findings, complete):
    old_items = (previous.get('issue_set', previous.get('hard_finding_set')) or []) if previous else []
    old_counts, new_counts = problem_counts(old_items), problem_counts(current_findings)
    resolved_counts, introduced_counts = old_counts-new_counts, new_counts-old_counts
    resolved = [occurrence_id(key, new_counts[key]+i) for key,n in resolved_counts.items() for i in range(n)]
    introduced = [occurrence_id(key, old_counts[key]+i) for key,n in introduced_counts.items() for i in range(n)]
    persisting = [occurrence_id(key,i) for key in old_counts.keys() & new_counts.keys()
                  for i in range(min(old_counts[key],new_counts[key]))]
    uncertain = [f for f in [*old_items,*current_findings] if f.get('identity_certainty') == 'unverified']
    unknown_logical = {identity_key(f, include_target=False) for f in uncertain}
    uncertain_keys = {identity_key(f) for f in uncertain}
    for f in [*old_items, *current_findings]:
        if identity_key(f, include_target=False) in unknown_logical:
            uncertain_keys.add(identity_key(f))
    if any(f.get('identity_certainty') == 'unverified' for f in current_findings):
        uncertain_keys.update(key for key in old_counts if key not in new_counts)
    unavailable_targets = {f.get('target_id') for f in current_findings
        if (f.get('rule') or f.get('kind')) in {'invalid_reference','missing_citation'} and f.get('target_id')}
    unavailable_global = any(f.get('conflict_fields', {}).get('error_category') in
        {'source_registry','no_sources','no_citations'} for f in current_findings)
    for item in old_items:
        if item.get('target_id') in unavailable_targets or (unavailable_global and
                (item.get('rule') or item.get('kind')) in {'unbound_number','merged_fact_range','unsupported_wording'}):
            uncertain_keys.add(identity_key(item))
    resolved = [occurrence_id(key,new_counts[key]+i) for key,n in resolved_counts.items() if key not in uncertain_keys for i in range(n)]
    introduced = [occurrence_id(key,old_counts[key]+i) for key,n in introduced_counts.items() if key not in uncertain_keys for i in range(n)]
    unverified = {occurrence_id(key,i) for key in uncertain_keys for i in range(max(old_counts[key],new_counts[key]))}
    stopped = bool(complete and previous and previous.get('review_complete')
                   and previous.get('rewrite', {}).get('changed') and old_counts and not resolved and not uncertain)
    return dict(resolved=sorted(resolved),introduced=sorted(introduced),persisting=sorted(persisting),
                unverified=sorted(unverified),
                previous_problem_count=sum(old_counts.values()),current_problem_count=sum(new_counts.values()),
                current_review_complete=complete,stopped=stopped,
                basis='all_previous_hard_problems_persist' if stopped else 'continue_or_pass')
