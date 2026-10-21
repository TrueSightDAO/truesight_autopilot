# Visual UAT playbook — `visual_uat`

Standard acceptance-testing process for a live page. Replaces the hand-rolled
"screenshot → eyeball → ask a vision model → manually work out whether the model
was right" loop, which produced two concrete failures in one session (see
*Failure modes*, below).

## The core rule

**The verdict comes from ground truth. The model never decides pass/fail.**

`visual_uat` returns two channels that must never be mixed:

| Channel | Contents | Trust |
|---|---|---|
| `assertions` + `dom_facts` | evaluated DOM text, console errors, failed requests, broken/collapsed elements | **Verified.** Decides the verdict. |
| `visual_findings` | Gemini's prose, every item `verified: false` | **UNVERIFIED.** A lead to probe, never a fact to report. |

If `visual_findings` disagrees with a governor's report, the governor is more
likely right than the model. Probe before believing either.

## Standard call

```
visual_uat(
  url="https://beta.agroverse.shop/cacao-journeys/",
  expectations={"brazilian-farms-count": "28", "gathering-venues-count": "27"},
  judge=True,
)
```

- Renders desktop (1440x900) **and** mobile (390x844) at 2x DPR.
- `expectations` maps a DOM **id or CSS selector** to the expected trimmed
  `textContent`. Use exact strings, including `~` and separators.
- `judge=False` for a pure DOM gate (fast, no vision call, no API key needed).

## Before promoting beta → prod

1. Run `visual_uat` against the **beta** URL with the values prod should show.
2. Require `verdict == "pass"`, `console_errors == []`, `failed_requests == []`.
3. Only then run `sync_beta_to_prod`.
4. Re-run `visual_uat` against the **prod** URL and require `pass` again.

Never promote on a `pass` you did not re-run against prod itself.

## How to treat a `visual_findings` item

1. Read it as a *hypothesis* ("something is off near X").
2. **Probe it** with a DOM or pixel check before repeating it to a governor.
3. Report it as verified only if the probe confirms it. Otherwise say the model
   claimed it and the probe disproved it — that is a useful result too.

## Failure modes this tool exists to prevent

Both happened on the real cacao-journeys page:

| Model claim | Reality |
|---|---|
| "Cart button protrudes out of the header into the hero" | The three elements crossing the header edge were `nav-links.mobile-menu`, `cart-sidebar`, `cart-items` — **off-canvas hidden panels**. Pixel sample of the strip below/right of the header: `rgb(242,241,241)`. **Disproven.** |
| "Hero text has no adequate dark scrim, text washes out" | The scrim **does** exist: `linear-gradient(rgba(59,51,51,.6), rgba(77,77,77,.7))` at opacity 1. The `<p>` genuinely had `text-shadow: none` while `<h1>` had one. **Right symptom, wrong cause.** |

Consequences baked into the design:

- **Never hardcode the model name.** `gemini-2.5-flash` returns `404` for new
  keys and names its replacement. The tool resolves from the live `ListModels`
  response, probes each candidate with a real `generateContent` call, and caches
  the winner. Override with `GEMINI_VISION_MODEL` only if you must.
- **`broken_images: 0` is not proof images are fine.** Cards that fail via
  `onerror → display:none` report `naturalWidth === 0` count of **zero**.
  The check that actually catches them is **`collapsed_elements`** — content
  present but rendered height 0. Read both fields.
- **Degrade, don't fail.** With no `GEMINI_API_KEY` the DOM channel still runs
  and `judge_error` says so; the verdict stays valid.

## Evidence you get back

- `dom_facts.desktop.screenshot` / `.mobile.screenshot` — paths on the autopilot
  box; attach them to the governor's thread with `send_telegram_attachment` when
  they need to see it rather than read a table.
- `collapsed_elements[]` — `{selector, content_len, height, sample}`.
- `assertions[]` — per viewport, `{key, expected, actual, pass}`.

## Adding page coverage

Coverage is declarative — put the ids you care about in `expectations`. When a
stat is computed client-side (like the journey stats), assert the **computed**
value, not the pre-JS literal; a pre-JS placeholder of `—` is a passing render
only if the JS replaced it, so always assert the final number.

## Reference
- Tool: `app/tools/visual_uat.py`
- Tests: `tests/test_visual_uat.py` (31 cases)
- Authoring guide: `app/tools/README.md`
