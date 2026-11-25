# Trial Clock

**A cloud build with an ending.** A personal Fly.io no-card credit planning agent, built for Hacksgiving 2026.

Trial Clock reads the official Free-plan and Sprite-pricing pages through a real Sprites Custom API connector, extracts dated source passages with structured Exa Answer, and computes a deterministic resource-cost and finish/export/destroy plan. It saves its state on the Sprite. A manual refresh compares the same plan against a new source snapshot; reopening makes no requests.

## Working demo

[Open the saved working demo](https://blucca.github.io/trial-clock/). The November 25 Sprite run made four successful native-connector requests: one plan, a request-free reopen, and one manual refresh. Both snapshots returned the same policy and source text. Exa used its default content-cache policy. The provider responses reported $0.014 total API cost.

The live result is **$0.19401 / CHECK_CREDIT_EXPIRY**: the account displayed the $1 no-card starter, but its exact issuance/expiration was not verified. The controlled, labelled cold-storage-price replay changes the same envelope to **$1.85133 / DO_NOT_START**, with zero API calls. All report inputs and source evidence are downloadable from the demo.

The run's source and output were exported and the Sprite was deleted within its two-hour budget. The published demo serves saved files; reproduce a live run inside your own authorized Sprite connector.

## Run without live access

Python 3.10+; standard library only. No installation.

```sh
python3 trial_clock.py plan
python3 trial_clock.py reopen
python3 trial_clock.py refresh
```

These commands use the clearly labelled November 24 reviewed-policy **replay fixture**. They do not contact Fly or Exa. View `run/report.html`; inspect `run/plan.json`, `run/evidence.json` and `run/state.json`.

`example_input.json` contains **example account timings**, including an invented credit-issuance time for demonstration. Replace the label, remaining credit and issuance/expiry with observed inputs before making an account-specific live plan. An explicit `credit_expires_at` takes precedence over issuance plus the source's duration. With neither, the result is `CHECK_CREDIT_EXPIRY`.

The example cost envelope is 2 CPU-hours + 4 GB-memory-hours + 10 GB-hot-storage-hours + 840 GB-cold-storage-hours = **$0.19401**, before network. Cold storage deliberately reserves 5 GB for seven days; early export/deletion can reduce that component. With the example two-hour budget starting 00:31 UTC, work must finish by 02:16, export by 02:26, and manual destruction by 02:31.

## One real connector run, inside the Sprite

The organization administrator configures the existing Exa key in a **Custom API connector**:

- Name: `trial-clock-exa`
- Base API URL: `https://api.exa.ai`
- Authentication: custom header `x-api-key`
- Access prefix: the actual Sprite name, e.g. `web-terminal`
- Allowed endpoints: `/contents`, `/answer`

From inside that Sprite, discover `https://api.sprites.dev/v1/gateway/list` and copy the selected connector's actual `gateway_base_url`. Connector creation is not a successful provider test. Trial Clock only receives the gateway URL; the raw Exa key remains in the organization connector.

```sh
export TRIAL_CLOCK_GATEWAY='ACTUAL_GATEWAY_BASE_URL_FROM_NATIVE_DISCOVERY'
python3 trial_clock.py plan --live --input actual_input.json --output live-run
python3 trial_clock.py reopen --output live-run
python3 trial_clock.py refresh --live --output live-run
```

Each live plan/refresh makes **one batch `/contents` request for the two official URLs, then one structured `/answer` request**. No retries, timers or direct-provider fallback. The default cumulative call budget is four, recorded before each attempted request in `live-run/live_calls.json`; that leaves one of the event run's five calls for the root's connector test. Setup/playground calls count separately toward the same total. Do not reset that counter to obtain more calls. `--call-budget` can reduce the allocation to two, or use up to five only when the root assigns that allowance.

Extraction checks required fields, decimal rates and exact quoted passages against the two retrieved texts. Failed retrieval or extraction stops the run; it does not invent a live result. Evidence records the source texts, fetch time, gateway endpoint outcomes, available provider request IDs and returned cost estimate. No provider credential or Fly control-plane token belongs in source, input or evidence files.

## Show a changed source without pretending Fly changed its policy

First save an ordinary replay plan. Then:

```sh
python3 trial_clock.py refresh --fixture fixtures/simulated-price-change.json
```

This **SIMULATED** source fixture raises cold-storage pricing. The same resource envelope moves from $0.19401 / `FITS_ENVELOPE` to $1.85133 / `DO_NOT_START`. It is a controlled replay, not a live Fly pricing update. A real unchanged refresh instead says no source/policy change and recomputes the saved plan.

## Demo on the Sprite

Run the live command on the Sprite and show its saved report alongside native connector evidence. The app has no web backend. To view its static output in the authenticated Sprite URL, serve only the report directory on port 8080:

```sh
cd live-run
python3 -m http.server 8080
```

Stop that temporary server after capturing the short demo. Viewing the static HTML never invokes Exa. Export source and the successful run's public-source evidence; then destroy the Sprite manually within the run budget. Code and demo artifacts suffice for the entry; a continuing public spending service is not part of this workflow.

## Three deterministic checks

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Checks cover exact resource math/seven-day expiry/two-hour ending, the card-backed full-month transition, and persistence/reopen/manual source-change replay. No network calls. Test scratch is under workspace `temp/hacksgiving-r79/` and is deleted with Python.

## Scope and entry artifacts

Selected workflow: one no-card Sprite, one existing-key connector, one plan and one manual refresh. The card-backed $5 pathway is an explanatory comparison, never an action or a recommendation to attach a card. Trial Clock does not create resources, change billing, open a public spending endpoint, auto-delete resources or run a continuing poller.

For the entry: publish code, share one short working demo on the selected authorized social account tagging Fly, and submit code/demo links plus a short description before November 30, 2026 at 08:00 UTC. Publication and submission are handled by the root after actual live review.

Sources: https://docs.fly.io/about/free-plan · https://fly.io/pricing/ · https://docs.fly.io/sprites/concepts/connectors · https://docs.exa.ai/reference/answer
