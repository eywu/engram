# Spec: Slack App Setup — Screenshot Shot List

**Status:** Pending capture
**Owner:** Eric
**Tracks:** GRO-471
**Audit ref:** `memory/reports/engram-onboarding-audit-2026-04-28.md` Finding B4 (🔴 Blocker — Slack app creation has no screenshots, this is the cliff)

---

## Goal

Replace the text-only `docs/slack-app-setup.md` with a screenshot-driven walkthrough that a non-engineer can follow without getting stuck. The most likely current failure mode is a user pasting the wrong token type into the wizard and silently failing at runtime — screenshots reduce that to near-zero.

## Recommended capture environment

**Use a personal Slack workspace, NOT your Ramp workspace.** Reasons:

1. Ramp's workspace likely requires admin approval to create apps, which means your screenshots will show approval-pending state instead of the success path a user sees in their own personal workspace.
2. Screenshots may be visible in PR review and in the public repo. Ramp branding, channel names, or coworker names should not appear.
3. The capture process creates and deletes a real test app — best to do that in a sandbox.

**If you don't have a personal workspace yet:** create one at https://slack.com/create. Takes ~90 seconds. You can name it anything (e.g. "Engram Test").

**Browser:** any. Chrome / Safari / Firefox / Arc all render the Slack admin UI identically.

**Theme:** **default light theme** (not dark). Slack's admin UI defaults to light, and the audit's target persona will see it in light mode. Don't switch.

**Window width:** at least 1280px wide. The Slack admin sidebar collapses below ~900px and the screenshots will look different from what users see.

**Locale:** English. (Default. Don't change anything.)

## Capture tool & workflow

**Recommended tool:** CleanShot X (you have it installed). Skitch / macOS built-in screenshot also work.

**For each shot:**

1. Take the screenshot at the moment described in the "When" column below.
2. Save it with the **exact filename** in the table below.
3. Add the **annotation described** (almost all are simple red rectangles around the thing the user clicks next).
4. Save to `~/code/python/engram/docs/images/slack-setup/`.

**Annotation style — please match these:**

- **Color:** red (`#E5484D` if your tool lets you pick, otherwise default red)
- **Stroke:** 3–4px, no fill
- **Shape:** rectangles only — no arrows, no numbered callouts. Reader's eye goes to the box, doc text says what to click.
- **Crop:** include enough surrounding UI that the reader can orient themselves (sidebar visible if relevant, page header visible). Don't crop tight.
- **Resolution:** 2x retina is fine; CleanShot handles this. PNG, not JPEG.

**Filename convention:** `NN-short-description.png` (zero-padded number, kebab-case description).

**Image size budget:** keep each PNG under ~400KB if possible. CleanShot's "Save with quick edit → optimize" hits this naturally; if not, run `pngquant` or just live with it (these are docs, not the homepage).

---

## Shot list (14 shots)

> **Note on text vs UI:** wherever the table says "the **Foo** button," I'm matching what the Slack admin UI actually shows as of April 2026. If a label has changed by the time you capture, capture what's actually on screen and let me know in your handoff message — I'll update the doc copy to match.

| # | Filename | When | What to capture | Red box around |
|---|----------|------|-----------------|----------------|
| 1 | `01-api-apps-landing.png` | At https://api.slack.com/apps, before clicking anything. | The "Your Apps" page, with whatever existing apps you have visible (or the empty state). | The green **Create New App** button in the top right. |
| 2 | `02-create-app-modal.png` | After clicking "Create New App". | The modal showing both options: "From a manifest" and "From scratch". | The **From a manifest** card. |
| 3 | `03-pick-workspace.png` | After clicking "From a manifest". | The "Pick a workspace to develop your app in" dropdown. | The **Next** button (greyed out at first, active once a workspace is picked). Workspace names should be visible — pick your test workspace. |

> ### ⚠️ Before Shot 4 — manifest gotchas
>
> The current YAML in `docs/slack-app-setup.md` has two known issues that will block paste-and-go. Both are tracked and will be fixed in code, but until they are, **edit the YAML before pasting**:
>
> **1. Empty `usage_hint` values (tracked by [GRO-593](https://linear.app/growthteam/issue/GRO-593))**
>
> Slack rejects empty strings on `usage_hint`. Find these two lines:
>
> ```yaml
>     - command: /exclude-from-nightly
>       description: "Exclude this channel from the nightly cross-channel summary"
>       usage_hint: ""             # ← change this
>       should_escape: false
>     - command: /include-in-nightly
>       description: "Include this channel in the nightly cross-channel summary"
>       usage_hint: ""             # ← change this
>       should_escape: false
> ```
>
> Replace the empty `usage_hint: ""` values with:
>
> ```yaml
>       usage_hint: "Run in this channel to exclude it from tonight's summary"
> ```
>
> and:
>
> ```yaml
>       usage_hint: "Run in this channel to include it in tonight's summary"
> ```
>
> Without this, Slack shows **"We can't translate a manifest with errors."** with red markers on lines 20 and 24, and the `Next` button stays disabled.
>
> **2. Slash command + app name collisions (tracked by [GRO-594](https://linear.app/growthteam/issue/GRO-594))**
>
> Slack enforces workspace-uniqueness on app names AND slash commands. If you already have an Engram install in this workspace (most likely if you're capturing in your real workspace instead of a fresh personal one), the manifest will fail because:
>
> - `display_information.name: Engram` collides with the existing app
> - `/engram`, `/exclude-from-nightly`, `/include-in-nightly` are already claimed
>
> **The cleanest fix: use a personal workspace** (per the "Recommended capture environment" section above). Zero collisions, zero risk to your real Engram install.
>
> **If you must capture in a workspace where Engram is already installed**, edit the manifest to use demo-suffixed names before pasting:
>
> ```yaml
> display_information:
>   name: Engram (Demo)        # was: Engram
> ...
> features:
>   bot_user:
>     display_name: Engram (Demo)   # was: Engram
> ...
>   slash_commands:
>     - command: /engram-demo               # was: /engram
>     - command: /exclude-from-nightly-demo # was: /exclude-from-nightly
>     - command: /include-in-nightly-demo   # was: /include-in-nightly
> ```
>
> The screenshots will show "Engram (Demo)" instead of "Engram" — that's fine, the doc text can call it out. Tokens captured in shots 8 and 13 are throwaway either way.
>
> ✅ **Once GRO-593 ships,** issue 1 disappears (manifest will paste cleanly into a fresh workspace). Once GRO-594 ships, issue 2 disappears (`engram setup` will generate a per-user manifest with the user's chosen agent name). Until then, this manual edit is the workaround.
>
> When the docs are rewritten with these screenshots, fold this guidance into a short "if you already have Engram installed" sidebar near the paste step — don't make it the primary flow.

| 4 | `04-paste-manifest.png` | The manifest paste step, with the YAML manifest from `docs/slack-app-setup.md` already pasted in. **Important:** make sure you've toggled to the **YAML** tab (not JSON — the doc supplies YAML). | The full editor view showing the manifest text. | The **YAML** tab (to make it obvious which format to use). |
| 5 | `05-review-summary.png` | The "Review summary & create your app" page that comes after Next. | The page showing the summary of permissions, scopes, and slash commands. | The **Create** button at the bottom. |
| 6 | `06-app-created-basic-info.png` | The app's "Basic Information" page immediately after creation. | Full page view, sidebar visible. | The **Install to Workspace** button (in the "Install your app" section, near the top). |
| 7 | `07-install-permission-prompt.png` | After clicking Install to Workspace, Slack shows a permission consent screen. | The full consent screen showing what scopes the app is requesting. | The **Allow** button at the bottom right. |
| 8 | `08-oauth-permissions-page.png` | After consenting, the user lands on the **OAuth & Permissions** page. Sidebar should show this is selected. | The top of the OAuth & Permissions page where the **Bot User OAuth Token** is shown (starts with `xoxb-`). | The token field + the **Copy** button next to it. (Don't worry about leaking the token — you'll regenerate or delete this app afterward, and we can blur it before commit if you want.) |
| 9 | `09-back-to-basic-info.png` | Click **Basic Information** in the left sidebar to navigate back. | The Basic Information page, scrolled down to the **App-Level Tokens** section (this section is below the fold — scroll to it). | The **Generate Token and Scopes** button inside the App-Level Tokens section. |
| 10 | `10-app-level-token-modal.png` | After clicking Generate Token and Scopes, a modal opens. Type a name like `engram-socket` in the Token Name field. **Don't click Generate yet — the modal needs the scope added first.** | The modal with the name typed but no scopes added yet. | The **Add Scope** button. |
| 11 | `11-app-level-token-scope-picker.png` | After clicking Add Scope, a dropdown appears. | The dropdown showing available scopes, with `connections:write` either visible or filterable. | The `connections:write` option in the dropdown. |
| 12 | `12-app-level-token-ready.png` | Scope added, modal now shows `connections:write` as a chip. | The modal with name filled in AND `connections:write` scope visible AND **Generate** button now enabled. | The **Generate** button. |
| 13 | `13-app-level-token-generated.png` | After clicking Generate, Slack shows the generated `xapp-…` token. | The token-displayed view with the `xapp-…` token visible and a Copy button. | The token field + Copy button. (Same blur-on-commit caveat as shot 8.) |
| 14 | `14-socket-mode-toggle.png` | Navigate to **Socket Mode** in the left sidebar. | The Socket Mode page showing whether socket mode is enabled. **If the manifest worked, it should already be enabled.** If not, this is where to flip it. | The "Enable Socket Mode" toggle (capture whether it's on or off as-found, just to show the user where it lives). |

## Optional but nice-to-haves (skip if running short on time)

| # | Filename | When | What to capture |
|---|----------|------|-----------------|
| 15 | `15-engram-running-in-slack.png` | After running `engram run` and DMing the bot, capture a real DM exchange in Slack with a successful Claude reply. | The conversation pane showing your message and Engram's response. Great for the README "what success looks like" hero shot. Use the **personal workspace, not Ramp**. |
| 16 | `16-engram-doctor-output.png` | Run `engram doctor` in your terminal after a successful setup. | Terminal screenshot showing the green ✓ checks. Reinforces that "doctor" is the diagnostic command. (PNG of terminal, dark-mode is fine here since terminals are often dark.) |

---

## Token sensitivity — please read

Shots **8** and **13** capture real Slack tokens (`xoxb-…` and `xapp-…`). Two ways to handle:

**Option A (recommended): blur in the screenshot itself before saving.** CleanShot has a one-click pixelate. Blur everything after the `xoxb-` / `xapp-` prefix so the prefix is still visible (that's the teaching moment for the doc) but the secret is gone. Save the blurred PNG.

**Option B: capture as-is, regenerate the token afterward.** Slack lets you rotate `xoxb-` (Reinstall App) and re-create app-level tokens at any time. You'd commit the screenshot with a real but rotated token. **Risk:** if you forget to rotate, you've published a working token.

**My recommendation: Option A.** Less drama, no "did I forget to rotate?" anxiety, the prefix is the only thing the user actually needs to see.

If you go Option B and forget, GitHub's secret scanning will yell at us within minutes — but let's not rely on that.

## Test app cleanup

After capture, you can either:

- **Keep the test app** in your personal workspace for future re-captures or testing — fine, no cost.
- **Delete the app** at Basic Information → bottom of the page → **Delete App**. Type the workspace name to confirm.

I don't have a preference; whatever's easiest for you.

---

## Handoff back to me

When you're done capturing, just message me with:

> "Slack screenshots done. They're in `docs/images/slack-setup/`."

I'll:

1. Verify all 14 shots are present and named correctly
2. Sanity-check the blurring on shots 8 and 13
3. Rewrite `docs/slack-app-setup.md` to weave the screenshots inline with the existing doc copy (cleaning up step 4's confusing token-type explanation while I'm at it)
4. Update the README's "Slack App Setup" reference if needed
5. Open a PR for review

Total time after handoff: ~30 min. Let me know if you hit any UI surprises and I'll adjust the spec.
