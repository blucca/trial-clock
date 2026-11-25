#!/usr/bin/env python3
"""A small, manually refreshed Fly no-card credit planning agent."""

import argparse
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from html import escape
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


HERE = Path(__file__).resolve().parent
SOURCE_URLS = ["https://docs.fly.io/about/free-plan", "https://fly.io/pricing/"]
RATE_NAMES = ("cpu_hour", "memory_gb_hour", "hot_gb_hour", "cold_gb_hour")
RESOURCE_NAMES = ("cpu_hours", "memory_gb_hours", "hot_gb_hours", "cold_gb_hours")
POLICY_FIELDS = (
    "no_card_credit_usd", "credit_duration_days", "card_monthly_credit_usd",
    "automatic_payg_at_usd", "whole_month_rebilled",
)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Use an ISO timestamp with Z or an explicit UTC offset.")
    return result.astimezone(timezone.utc)


def iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def amount(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("A price or resource quantity is not a decimal number.") from error
    if not result.is_finite() or result < 0:
        raise ValueError("Prices and resource quantities must be finite and nonnegative.")
    return result


def decimal_text(value):
    return format(value.normalize(), "f")


def validate_policy(policy):
    for name in POLICY_FIELDS:
        if name not in policy:
            raise ValueError("Extraction is missing " + name)
    for name in ("no_card_credit_usd", "card_monthly_credit_usd", "automatic_payg_at_usd"):
        amount(policy[name])
    if not isinstance(policy["credit_duration_days"], int) or policy["credit_duration_days"] <= 0:
        raise ValueError("Credit duration must be a positive whole number of days.")
    if not isinstance(policy["whole_month_rebilled"], bool):
        raise ValueError("whole_month_rebilled must be a boolean.")
    for name in RATE_NAMES:
        amount(policy["rates"][name])
    if not policy.get("citations"):
        raise ValueError("Extraction has no cited passages.")
    required_facts = set(POLICY_FIELDS) | {"rates." + name for name in RATE_NAMES}
    cited_facts = {field.strip() for cite in policy["citations"] for field in cite["field"].split(",")}
    if not required_facts.issubset(cited_facts):
        raise ValueError("Extraction lacks citations for: " + ", ".join(sorted(required_facts - cited_facts)))
    return policy


def card_comparison(month_usage_usd, policy):
    usage = amount(month_usage_usd)
    threshold = amount(policy["automatic_payg_at_usd"])
    moved = usage >= threshold
    if moved:
        billed = usage if policy["whole_month_rebilled"] else max(Decimal("0"), usage - threshold)
    else:
        billed = max(Decimal("0"), usage - amount(policy["card_monthly_credit_usd"]))
    return {
        "illustrative_month_usage_usd": decimal_text(usage),
        "automatic_payg": moved,
        "standard_rate_usage_billed_usd": decimal_text(billed),
        "applies_to_this_run": False,
    }


def calculate(inputs, policy):
    """Compute only: no billing reads, account changes, deployment or deletion."""
    validate_policy(policy)
    if inputs.get("card_attached") is not False:
        raise ValueError("This agent's selected workflow is the no-card starter; set card_attached=false.")
    start = instant(inputs["start_at"])
    task_deadline = instant(inputs["deadline_at"])
    budget_minutes = int(inputs["budget_minutes"])
    export_minutes = int(inputs.get("export_minutes", 10))
    destroy_minutes = int(inputs.get("destroy_minutes", 5))
    task_minutes = int(inputs["task_minutes"])
    if min(budget_minutes, task_minutes, export_minutes, destroy_minutes) <= 0:
        raise ValueError("Task, budget, export and destroy durations must be positive.")
    remaining = amount(inputs["remaining_credit_usd"])
    rows = []
    total = Decimal("0")
    for resource, rate in zip(RESOURCE_NAMES, RATE_NAMES):
        units = amount(inputs["resources"][resource])
        price = amount(policy["rates"][rate])
        subtotal = units * price
        total += subtotal
        rows.append({"resource": resource, "quantity": decimal_text(units),
                     "rate_usd": decimal_text(price), "cost_usd": decimal_text(subtotal)})
    credit_expiry = None
    expiry_basis = "unknown: supply observed credit_expires_at or credit_issued_at"
    if inputs.get("credit_expires_at"):
        credit_expiry = instant(inputs["credit_expires_at"])
        expiry_basis = "supplied credit expiration"
    elif inputs.get("credit_issued_at"):
        credit_expiry = instant(inputs["credit_issued_at"]) + timedelta(days=policy["credit_duration_days"])
        expiry_basis = "supplied issuance plus cited credit duration"
    bounds = {"two_hour_or_input_budget": start + timedelta(minutes=budget_minutes),
              "task_deadline": task_deadline}
    if credit_expiry:
        bounds["starter_credit_expiry"] = credit_expiry
    limiting_bound = min(bounds, key=bounds.get)
    destroy_by = bounds[limiting_bound]
    export_by = destroy_by - timedelta(minutes=destroy_minutes)
    finish_by = export_by - timedelta(minutes=export_minutes)
    planned_finish = start + timedelta(minutes=task_minutes)
    reasons = []
    if total > remaining:
        reasons.append("Resource envelope exceeds the supplied remaining starter credit.")
    if planned_finish > finish_by:
        reasons.append("Planned work leaves insufficient time for export and destruction.")
    if credit_expiry and credit_expiry <= start:
        reasons.append("Starter credit has expired by the supplied start time.")
    decision = "DO_NOT_START" if reasons else "FITS_ENVELOPE"
    if not credit_expiry and not reasons:
        decision = "CHECK_CREDIT_EXPIRY"
        reasons.append("The cost fits; starter-credit issuance/expiration is still missing.")
    return {
        "decision": decision, "reasons": reasons,
        "resource_cost_usd": decimal_text(total),
        "remaining_credit_usd": decimal_text(remaining),
        "headroom_usd": decimal_text(remaining - total), "cost_rows": rows,
        "cost_scope": "Sprite CPU, memory, hot storage and retained cold storage; network excluded. Exa uses the existing provider allowance.",
        "credit_expires_at": iso(credit_expiry) if credit_expiry else None,
        "expiry_basis": expiry_basis, "limiting_bound": limiting_bound,
        "planned_work_finishes_at": iso(planned_finish),
        "finish_work_by": iso(finish_by), "export_by": iso(export_by), "destroy_by": iso(destroy_by),
        "schedule_action": "Do not start this envelope." if decision == "DO_NOT_START" else "Finish, export, then destroy manually by the listed times.",
        "no_card_credit_usd": str(policy["no_card_credit_usd"]),
        "credit_duration_days": policy["credit_duration_days"],
        "card_path_comparison": card_comparison(inputs.get("illustrative_month_usage_usd", "7.00"), policy),
    }


def load_fixture(path):
    snapshot = read_json(path)
    snapshot["mode"] = "replay_fixture"
    snapshot["gateway_requests_this_run"] = 0
    validate_policy(snapshot["policy"])
    return snapshot


def output_schema():
    properties = {name: {"type": "string"} for name in
                  ("no_card_credit_usd", "card_monthly_credit_usd", "automatic_payg_at_usd")}
    properties["credit_duration_days"] = {"type": "integer"}
    properties["whole_month_rebilled"] = {"type": "boolean"}
    properties["rates"] = {"type": "object", "properties": {name: {"type": "string"} for name in RATE_NAMES},
                           "required": list(RATE_NAMES), "additionalProperties": False}
    properties["citations"] = {"type": "array", "items": {
        "type": "object", "properties": {
            "field": {"type": "string", "enum": list(POLICY_FIELDS) + ["rates." + name for name in RATE_NAMES]},
            "url": {"type": "string"}, "quote": {"type": "string"}},
        "required": ["field", "url", "quote"], "additionalProperties": False}}
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


class Gateway:
    """Two explicit provider calls per refresh. No authentication/key in the guest."""

    def __init__(self, base_url, directory, call_budget):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != "api.sprites.dev" or not parsed.path.startswith("/v1/gateway/"):
            raise ValueError("Pass the actual https://api.sprites.dev/v1/gateway/... gateway_base_url returned inside the Sprite.")
        if parsed.query or parsed.fragment:
            raise ValueError("Pass gateway_base_url without query parameters or a fragment.")
        self.base = base_url.rstrip("/")
        self.counter_path = Path(directory) / "live_calls.json"
        self.budget = int(call_budget)
        if not 2 <= self.budget <= 5:
            raise ValueError("The total live-call budget must be between 2 and 5.")
        self.count = read_json(self.counter_path)["attempted_exa_calls"] if self.counter_path.exists() else 0
        if self.count + 2 > self.budget:
            raise ValueError("Fewer than two Exa calls remain; no live refresh was started.")
        self.requests = []

    def post(self, endpoint, body):
        if self.count >= self.budget:
            raise ValueError("Live Exa call budget exhausted.")
        self.counter_path.parent.mkdir(parents=True, exist_ok=True)
        self.count += 1
        write_json(self.counter_path, {"attempted_exa_calls": self.count, "maximum_calls": self.budget})
        request = Request(self.base + endpoint, data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=45) as response:
                data = json.loads(response.read())
                self.requests.append({"endpoint": endpoint, "status": response.status,
                                      "request_id": response.headers.get("x-request-id"),
                                      "costDollars": data.get("costDollars")})
                return data
        except HTTPError as error:
            # No retry or alternate provider path: the root owns the total call budget.
            raise ValueError("Connector request " + endpoint + " returned HTTP " + str(error.code)) from error


def canonical_url(value):
    return value.rstrip("/")


def normalized_quote(value):
    return " ".join(value.split())


def live_snapshot(gateway_url, directory, call_budget=4):
    gateway = Gateway(gateway_url, directory, call_budget)
    contents = gateway.post("/contents", {"ids": SOURCE_URLS, "text": {"maxCharacters": 9000}})
    sources = []
    wanted = {canonical_url(url) for url in SOURCE_URLS}
    for result in contents.get("results", []):
        url = result.get("url") or result.get("id")
        if not url or canonical_url(url) not in wanted or not result.get("text"):
            continue
        text = result["text"][:9000]
        sources.append({"url": url, "title": result.get("title", url), "text": text,
                        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    if {canonical_url(item["url"]) for item in sources} != wanted:
        raise ValueError("The connector did not return text for both selected official source URLs.")
    prompt = (
        "Extract the Fly.io no-card starter credit amount, its duration, the card-backed monthly allowance, "
        "the automatic Pay As You Go threshold and whether the whole month is rebilled from the first cent. "
        "Also extract the four Sprite hourly rates: CPU-hour, GB-memory-hour, hot GB-hour and cold GB-hour. "
        "Return dollar values as decimal strings. For each fact supply a field, one of the provided exact URLs, "
        "and an exact contiguous quote from its supplied text. Use only these two supplied official texts, "
        "not guessed account state or another plan. Do not conflate $1 starter credit with card-backed $5 monthly credit.\n\n"
        + json.dumps(sources, ensure_ascii=False)
    )
    response = gateway.post("/answer", {"query": prompt, "model": "exa", "stream": False,
                                        "outputSchema": output_schema()})
    policy = response.get("answer")
    if isinstance(policy, str):
        policy = json.loads(policy)
    if not isinstance(policy, dict):
        raise ValueError("Exa did not return the requested structured answer.")
    validate_policy(policy)
    texts = {canonical_url(source["url"]): normalized_quote(source["text"]) for source in sources}
    for citation in policy["citations"]:
        source_text = texts.get(canonical_url(citation["url"]))
        quote = normalized_quote(citation["quote"])
        if source_text is None or not quote or quote not in source_text:
            raise ValueError("An extracted citation is not an exact passage from the selected source text.")
    return {"mode": "live_connector", "label": "Live Sprite connector retrieval + structured Exa extraction",
            "retrieved_at": now_iso(), "sources": sources, "policy": policy,
            "gateway_requests_this_run": len(gateway.requests), "gateway_requests": gateway.requests,
            "attempted_exa_calls_total": gateway.count}


def field_changes(before, after, prefix=""):
    changes = []
    for key in sorted(set(before) | set(after)):
        if key == "citations":
            continue
        name = prefix + key
        old, new = before.get(key), after.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            changes.extend(field_changes(old, new, name + "."))
        elif old != new:
            changes.append({"field": name, "before": old, "after": new})
    return changes


def make_run(inputs, snapshot, previous=None):
    result = {"schema_version": "trial-clock/1", "generated_at": now_iso(),
              "input": copy.deepcopy(inputs), "snapshot": snapshot,
              "plan": calculate(inputs, snapshot["policy"]), "changes": None}
    if previous:
        old_hashes = {source["url"]: source.get("sha256") for source in previous["snapshot"].get("sources", [])}
        new_hashes = {source["url"]: source.get("sha256") for source in snapshot.get("sources", [])}
        comparable = {url for url in old_hashes.keys() & new_hashes.keys() if old_hashes[url] and new_hashes[url]}
        result["changes"] = {
            "previous_generated_at": previous["generated_at"],
            "policy_fields": field_changes(previous["snapshot"]["policy"], snapshot["policy"]),
            "source_snapshots": field_changes({url: old_hashes[url] for url in comparable},
                                               {url: new_hashes[url] for url in comparable}),
            "source_comparison": ("Compared saved source text." if comparable else
                                  "Replay source text has no live snapshot comparison; policy-field differences are shown separately."),
            "cost_before_usd": previous["plan"]["resource_cost_usd"],
            "cost_after_usd": result["plan"]["resource_cost_usd"],
            "decision_before": previous["plan"]["decision"], "decision_after": result["plan"]["decision"],
        }
    return result


def render(run):
    plan, snapshot = run["plan"], run["snapshot"]
    e = lambda value: escape(str(value))
    rows = "".join("<tr><td>" + e(row["resource"]) + "</td><td>" + e(row["quantity"]) +
                   "</td><td>$" + e(row["rate_usd"]) + "</td><td>$" + e(row["cost_usd"]) + "</td></tr>"
                   for row in plan["cost_rows"])
    citations = "".join('<li><strong>' + e(cite["field"]) + '</strong><blockquote>' + e(cite["quote"]) +
                        '</blockquote><a href="' + escape(cite["url"], quote=True) + '">' + e(cite["url"]) + '</a></li>'
                        for cite in snapshot["policy"]["citations"])
    changes = run["changes"]
    if changes is None:
        change_html = "<p>First saved plan. Reopen it or run one explicit refresh.</p>"
    elif not changes["policy_fields"] and not changes["source_snapshots"]:
        change_html = "<p>No source or extracted-policy changes on this refresh. The same envelope was recalculated.</p>"
    else:
        change_html = ("<p>Decision: " + e(changes["decision_before"]) + " → " + e(changes["decision_after"]) +
                       ". Cost: $" + e(changes["cost_before_usd"]) + " → $" + e(changes["cost_after_usd"]) + ".</p>")
        change_html += "<ul>" + "".join("<li>" + e(item["field"]) + ": " + e(item["before"]) + " → " + e(item["after"]) + "</li>"
                                       for item in changes["policy_fields"]) + "</ul>"
        change_html += "<p>Changed source snapshots: " + str(len(changes["source_snapshots"])) + ".</p>"
    if changes:
        change_html += "<p class=\"muted\">" + e(changes["source_comparison"]) + "</p>"
    reasons = "".join("<li>" + e(reason) + "</li>" for reason in plan["reasons"])
    comparison = plan["card_path_comparison"]
    color = "#fca5a5" if plan["decision"] == "DO_NOT_START" else "#99f6c7"
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trial Clock — a cloud build with an ending</title><style>
body{background:#0c1421;color:#e7edf4;font:16px/1.6 system-ui,sans-serif;margin:0}main{max-width:1000px;margin:auto;padding:36px 24px}
h1{font-size:44px;margin:0}h2{font-size:23px}a{color:#9ac6ff}small,.muted{color:#a6b7ca}.pill{border:1px solid #526981;border-radius:20px;padding:5px 13px;display:inline-block}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}.card,section{background:#152235;border:1px solid #29425e;border-radius:13px;padding:20px;margin:18px 0}.number{font-size:30px;font-weight:750}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}td,th{padding:10px;text-align:left;border-bottom:1px solid #31455d}blockquote{margin:6px 0;border-left:3px solid #d4a756;padding-left:15px}li{margin:12px 0}code{color:#ffde9b}footer{margin-top:30px}
</style><main><span class="pill">PERSONAL CLOUD-CREDIT AGENT</span><h1>Trial Clock</h1><p>A cloud build with an ending.</p>
<p class="pill">MODE_LABEL</p><p class="muted">INPUT_LABEL<br>Retrieved: RETRIEVED · Saved: GENERATED · Native gateway requests this run: REQUESTS</p>
<div class="cards"><div class="card"><small>RESOURCE ENVELOPE</small><div class="number">$COST</div></div><div class="card"><small>SUPPLIED REMAINING CREDIT</small><div class="number">$CREDIT</div></div><div class="card"><small>DECISION</small><div style="color:COLOR;font-weight:700">DECISION_VALUE</div></div></div>
<p>COST_SCOPE</p><ul>REASONS</ul><section><h2>Your ending</h2><p>Finish work by <strong>FINISH</strong><br>Export by <strong>EXPORT</strong><br>Destroy manually by <strong>DESTROY</strong></p><p>Expiry: EXPIRY<br><small>EXPIRY_BASIS · Limiting bound: BOUND</small></p><p>ACTION</p></section>
<section><h2>Exact resource math</h2><table><tr><th>Resource</th><th>Envelope</th><th>Hourly rate</th><th>Cost</th></tr>ROWS</table></section>
<section><h2>What changed?</h2>CHANGE_HTML</section><section><h2>No card ≠ card-backed monthly credit</h2><p>No-card starter: $STARTER for DAYS days. Card-backed allowance: $MONTHLY per month; automatic Pay As You Go at $THRESHOLD. Whole-month rebilling: REBILL.</p><p>Illustrative $MONTH_USAGE monthly usage → $BILLED standard-rate usage billed. This comparison does not apply to the selected no-card run.</p></section>
<section><h2>Source passages</h2><ol>CITATIONS</ol></section><footer><a href="plan.json">Plan JSON</a> · <a href="evidence.json">Source evidence JSON</a><p class="muted">This is a saved report. Refresh is an explicit CLI action, not a timer or a public spending endpoint.</p></footer></main></html>""".replace("MODE_LABEL", e(snapshot["label"])) \
        .replace("INPUT_LABEL", e(run["input"].get("label", "Supplied planning inputs"))) \
        .replace("RETRIEVED", e(snapshot["retrieved_at"])).replace("GENERATED", e(run["generated_at"])) \
        .replace("REQUESTS", e(snapshot["gateway_requests_this_run"])).replace("$COST", "$" + e(plan["resource_cost_usd"])) \
        .replace("$CREDIT", "$" + e(plan["remaining_credit_usd"])).replace("COLOR", color) \
        .replace("COST_SCOPE", e(plan["cost_scope"])).replace("REASONS", reasons).replace("FINISH", e(plan["finish_work_by"])) \
        .replace("EXPORT", e(plan["export_by"])).replace("DESTROY", e(plan["destroy_by"])) \
        .replace("EXPIRY_BASIS", e(plan["expiry_basis"])).replace("EXPIRY", e(plan["credit_expires_at"] or "not supplied")) \
        .replace("BOUND", e(plan["limiting_bound"])).replace("ACTION", e(plan["schedule_action"])) \
        .replace("ROWS", rows).replace("CHANGE_HTML", change_html).replace("$STARTER", "$" + e(plan["no_card_credit_usd"])) \
        .replace("DAYS", e(plan["credit_duration_days"])).replace("$MONTHLY", "$" + e(snapshot["policy"]["card_monthly_credit_usd"])) \
        .replace("$THRESHOLD", "$" + e(snapshot["policy"]["automatic_payg_at_usd"])) \
        .replace("REBILL", e(snapshot["policy"]["whole_month_rebilled"])).replace("$MONTH_USAGE", "$" + e(comparison["illustrative_month_usage_usd"])) \
        .replace("$BILLED", "$" + e(comparison["standard_rate_usage_billed_usd"])).replace("CITATIONS", citations) \
        .replace("DECISION_VALUE", e(plan["decision"]))


def save_run(directory, run, previous=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if previous:
        write_json(directory / "previous.json", previous)
    write_json(directory / "state.json", run)
    write_json(directory / "plan.json", {"input": run["input"], "plan": run["plan"], "changes": run["changes"]})
    write_json(directory / "evidence.json", run["snapshot"])
    (directory / "report.html").write_text(render(run), encoding="utf-8")


def summary(run, directory):
    plan = run["plan"]
    return {"mode": run["snapshot"]["mode"], "label": run["snapshot"]["label"],
            "decision": plan["decision"], "resource_cost_usd": plan["resource_cost_usd"],
            "finish_work_by": plan["finish_work_by"], "export_by": plan["export_by"], "destroy_by": plan["destroy_by"],
            "gateway_requests_this_run": run["snapshot"]["gateway_requests_this_run"],
            "changed_policy_fields": len(run["changes"]["policy_fields"]) if run["changes"] else 0,
            "report": str(Path(directory) / "report.html")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "refresh"):
        command = subcommands.add_parser(name)
        command.add_argument("--input", type=Path, default=None)
        command.add_argument("--output", type=Path, default=HERE / "run")
        mode = command.add_mutually_exclusive_group()
        mode.add_argument("--live", action="store_true", help="Exactly one /contents + one /answer through the native connector")
        mode.add_argument("--fixture", type=Path, default=None)
        command.add_argument("--gateway", default=os.environ.get("TRIAL_CLOCK_GATEWAY", ""))
        command.add_argument("--call-budget", type=int, default=4, help="Cumulative calls in this output directory; default reserves one of five for root's setup test")
    reopen = subcommands.add_parser("reopen", help="Read saved state without HTTP or recalculation")
    reopen.add_argument("--output", type=Path, default=HERE / "run")
    args = parser.parse_args()
    if args.command == "reopen":
        print(json.dumps(summary(read_json(args.output / "state.json"), args.output), indent=2))
        return
    previous = None
    if args.command == "refresh":
        previous = read_json(args.output / "state.json")
    inputs = read_json(args.input) if args.input else (previous["input"] if previous else read_json(HERE / "example_input.json"))
    if args.live:
        snapshot = live_snapshot(args.gateway, args.output, args.call_budget)
    else:
        snapshot = load_fixture(args.fixture or HERE / "fixtures" / "reviewed-policy.json")
    run = make_run(inputs, snapshot, previous)
    save_run(args.output, run, previous)
    print(json.dumps(summary(run, args.output), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        print("Trial Clock stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
