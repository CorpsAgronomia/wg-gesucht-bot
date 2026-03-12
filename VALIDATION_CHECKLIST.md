# GitHub Actions Deployment Validation Checklist

## Manual Test Steps

- [ ] Add the required repository secrets: `WG_EMAIL`, `WG_PASSWORD`, `LISTING_IDS`.
- [ ] Trigger the `WG Gesucht Bot` workflow manually with `workflow_dispatch`.
- [ ] Confirm the `Install dependencies` step completes successfully.
- [ ] Confirm the `Install Chromium` step completes successfully.
- [ ] Confirm the `Random skip` step prints `Continuing execution`.
- [ ] Confirm the `Night cooldown` step reports a run is allowed.
- [ ] Confirm the `Run WG Bot` step starts `python main.py`.
- [ ] Confirm the bot logs show a completed single execution cycle.
- [ ] Confirm the listing update flow returns HTTP `200`.

## Acceptance Criteria

- [ ] Workflow triggered
- [ ] Environment variables injected
- [ ] Bot executed
- [ ] Scheduler cycle completed
- [ ] Listing update returned HTTP 200

If every item passes, the deployment status is:

`DEPLOYMENT SUCCESSFUL`

`BOT RUNNING VIA GITHUB ACTIONS`
