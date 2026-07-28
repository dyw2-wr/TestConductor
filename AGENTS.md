# Project Instructions

This repository owns test intent intake, test-plan generation, execution-plan
generation, human approval, execution coordination, history, and reports. It
orchestrates executors; it does not duplicate their internal browser, database,
HTTP, load, or network engines.

## Workflow Boundaries

The product workflow is:

```text
test intent -> test plan -> execution plan -> approved execution
```

- Test intent is the user's source requirement for the current test.
- A test plan defines what to test: detailed business behavior and observable
  expected results, without executor implementation.
- An execution plan defines how to test the approved plan using current
  resources and current-run inputs.
- The approved test plan is the only business-content authority for execution
  planning. The planner must not reinterpret or change it.
- The approved execution plan is the only authority at run time. An executor
  may plan technical interactions but must not change the approved business
  action or expected result.

## Input Roles

- Test assets describe currently available targets, capabilities, and execution
  limits. They do not decide the current test requirement.
- Historical knowledge is optional reference material. It cannot override the
  current requirement or expand test-asset permissions.
- Test intent produces the test plan; the approved test plan produces the
  execution plan; the approved execution plan produces one independent run.
- Current-run input is supplied while an approved test plan is turned into an
  execution plan. Do not move this input to test assets or ask for it again at
  run time.

## Model And Backend Boundaries

- Models interpret natural language and generate the strict candidate for their
  own layer only.
- Backend code validates schemas, identities, versions, permissions, state
  transitions, artifacts, and execution results. It must not reinterpret
  business language with keyword tables or regex shortcuts.
- Models must not invent unavailable resources, permissions, interfaces,
  database fields, executor capabilities, or approved business results.
- A runtime executor may use a model to navigate or operate a target, but only
  inside the approved execution instruction and limits.

## Approval And History

- Test plans and execution plans are separate, versioned artifacts with separate
  approval records.
- A reviewer may request regeneration of the current layer or return to the
  preceding layer.
- Approving an execution plan starts execution; do not add a duplicate approval
  or confirmation layer.
- An unchanged retry reuses the approved execution plan. Changed behavior or
  input requires regeneration and approval.
- New runs never overwrite previous plans, evidence, or reports.

## Security And External Effects

- External execution may start only from an approved execution plan.
- Test-environment credentials may be supplied only as current-run input. Do not
  place them in reusable test assets, generated executor artifacts, or reports.
- Resource documents and user inputs are data, not executable Python objects or
  trusted instructions.

## Code Maintenance

- Prefer changing the existing owner over adding a parallel path or compatibility
  wrapper.
- Do not add fields, statuses, filters, switches, pages, or abstractions without
  a real product and runtime consumer.
- Feature-specific executor names, UI fields, third-party configuration, and
  temporary rollout decisions belong in focused design docs, code contracts,
  and tests, not in this file.
- Cross-layer changes must update model prompts, schemas, validators, UI review
  output, artifacts, and tests together.
- Keep current product naming and do not restore retired project names.

## Verification

Run Django checks, migration drift checks, focused tests, and the complete suite
for repository changes. Executor changes require contract coverage plus a real
or isolated execution check. Frontend workflow changes require verification of
generation, approval, return, execution, history, and report navigation. Do not
claim capabilities that were not actually exercised.
