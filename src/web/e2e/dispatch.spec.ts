import { expect, test, type Page, type Route } from "@playwright/test";

const TENANT_ID = "10000000-0000-4000-8000-000000000001";
const DETECTION_ID = "22000000-0000-4000-8000-000000000001";
const DISPATCH_CASE_ID = "53000000-0000-4000-8000-000000000001";

const candidate = {
  schema_version: "1.1.0",
  tenant_id: TENANT_ID,
  detection_id: DETECTION_ID,
  source_id: "20000000-0000-4000-8000-000000000001",
  asset_id: "21000000-0000-4000-8000-000000000001",
  occurred_at: "2026-08-30T12:30:00Z",
  received_at: "2026-08-30T12:31:00Z",
  proposed_category: "traffic_safety",
  event_type: "vehicle_collision",
  description: "Two vehicles visibly collide.",
  confidence: 0.82,
  detector_version: "reka-demo-v1",
  review_status: "awaiting_review",
  expires_at: "2026-08-31T12:30:00Z",
  evidence_available: true,
};

const dispatchCase = {
  dispatch_case_id: DISPATCH_CASE_ID,
  incident_id: DETECTION_ID,
  case_reference: "CH-1042",
  category: "traffic_safety",
  zone_label: "Demo Zone A",
  occurred_at: "2026-08-30T12:30:00Z",
  state: "retry_scheduled",
  message_template_version: "dispatch-alert-v1",
  authorized_by_principal_id: "reviewer-demo-one",
  authorized_at: "2026-08-30T12:35:00Z",
  primary_contact: {
    display_name: "Demo Zone primary",
    phone_masked: "•••• 1001",
    role: "primary",
  },
  supervisor_contact: {
    display_name: "Demo Zone supervisor",
    phone_masked: "•••• 2002",
    role: "supervisor",
  },
  attempts: [
    {
      attempt_id: "54000000-0000-4000-8000-000000000001",
      attempt_number: 1,
      target_role: "primary",
      contact_name: "Demo Zone primary",
      phone_masked: "•••• 1001",
      state: "unacknowledged",
      created_at: "2026-08-30T12:35:01Z",
      updated_at: "2026-08-30T12:35:22Z",
    },
  ],
  next_attempt_at: "2026-08-30T12:35:52Z",
  canceled_at: null,
};

const dispatchPreview = {
  incident_id: DETECTION_ID,
  case_reference: "CH-1042",
  category: "traffic_safety",
  zone_label: "Demo Zone A",
  occurred_at: "2026-08-30T12:30:00Z",
  primary_contact: {
    display_name: "Demo Zone primary",
    phone_masked: "•••• 1001",
    role: "primary",
  },
  supervisor_contact: {
    display_name: "Demo Zone supervisor",
    phone_masked: "•••• 2002",
    role: "supervisor",
  },
  maximum_attempts: 3,
  retry_delay_seconds: 30,
};

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockSession(page: Page, role: "reviewer" | "tenant_admin") {
  await page.route("**/v1/me/tenants", (route) =>
    json(route, {
      active_tenant_id: TENANT_ID,
      tenants: [
        {
          tenant_id: TENANT_ID,
          slug: "demo-one",
          display_name: "Demo Tenant One",
          role,
        },
      ],
    }),
  );
  await page.route("**/v1/demo/session/start", (route) =>
    json(route, { status: "started", deleted_pending_candidates: 0 }),
  );
}

async function openReviewer(page: Page) {
  await mockSession(page, "reviewer");
  await page.route("**/v1/candidate-detections?*", (route) =>
    json(route, { items: [candidate] }),
  );
  await page.route("**/v1/candidate-detections/*/evidence", (route) =>
    route.fulfill({
      status: 200,
      contentType: "video/mp4",
      body: "bounded-demo-video",
    }),
  );
  await page.goto("/#/console/review");
  await page.getByRole("button", { name: /Reviewer · Demo One/ }).click();
  await expect(page.getByText("UNCONFIRMED CANDIDATE", { exact: true })).toBeVisible();
}

function reviewResponse(decision: "confirmed" | "rejected") {
  return {
    schema_version: "1.0.0",
    tenant_id: TENANT_ID,
    review_id: "42000000-0000-4000-8000-000000000001",
    detection_id: DETECTION_ID,
    decision,
    ...(decision === "confirmed"
      ? {
          confirmed_category: "traffic_safety",
          promoted_external_event_id: `video-candidate:${DETECTION_ID}`,
        }
      : { rejection_reason: "false_positive" }),
    reviewed_by: "reviewer-demo-one",
    reviewed_at: "2026-08-30T12:34:00Z",
  };
}

test("rejecting a candidate never invokes dispatch", async ({ page }) => {
  let dispatchRequests = 0;
  await page.route("**/v1/incidents/*/dispatch-authorizations", async (route) => {
    dispatchRequests += 1;
    await json(route, dispatchCase, 201);
  });
  await page.route("**/v1/candidate-detections/*/review", async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      decision: "rejected",
      rejection_reason: "false_positive",
    });
    expect(route.request().headers()["idempotency-key"]).toBeTruthy();
    await json(route, reviewResponse("rejected"));
  });

  await openReviewer(page);
  await page.getByRole("button", { name: "Review this candidate" }).click();
  await page.getByLabel("Reject candidate").check();
  await page.getByRole("button", { name: "Submit final rejection" }).click();

  await expect(page.getByText(/Candidate rejected\. No incident was promoted/)).toBeVisible();
  expect(dispatchRequests).toBe(0);
});

test("confirm without calling creates no dispatch request", async ({ page }) => {
  let dispatchRequests = 0;
  await page.route("**/v1/incidents/*/dispatch-authorizations", async (route) => {
    dispatchRequests += 1;
    await json(route, dispatchCase, 201);
  });
  await page.route("**/v1/candidate-detections/*/review", async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      decision: "confirmed",
      confirmed_category: "property",
    });
    await json(route, reviewResponse("confirmed"));
  });

  await openReviewer(page);
  await page.getByRole("button", { name: "Review this candidate" }).click();
  await page.getByRole("button", { name: "Confirm without calling" }).click();

  await expect(page.getByText(/No call was authorized or created/)).toBeVisible();
  expect(dispatchRequests).toBe(0);
});

test("explicit checkbox authorization creates one case and shows the masked timeline", async ({ page }) => {
  let dispatchRequests = 0;
  let cancelRequests = 0;
  await page.route("**/v1/candidate-detections/*/review", (route) =>
    json(route, reviewResponse("confirmed")),
  );
  await page.route("**/v1/incidents/*/dispatch-preview", (route) => {
    expect(new URL(route.request().url()).pathname).toBe(
      `/v1/incidents/${DETECTION_ID}/dispatch-preview`,
    );
    return json(route, dispatchPreview);
  });
  await page.route("**/v1/incidents/*/dispatch-authorizations", async (route) => {
    dispatchRequests += 1;
    expect(new URL(route.request().url()).pathname).toBe(
      `/v1/incidents/${DETECTION_ID}/dispatch-authorizations`,
    );
    expect(route.request().postDataJSON()).toEqual({
      authorize_call: true,
      message_template_version: "dispatch-alert-v1",
    });
    expect(route.request().headers()["idempotency-key"]).toBeTruthy();
    await json(route, dispatchCase, 201);
  });
  await page.route(`**/v1/dispatch-cases/${DISPATCH_CASE_ID}`, (route) =>
    json(route, dispatchCase),
  );
  await page.route(`**/v1/dispatch-cases/${DISPATCH_CASE_ID}/cancel`, async (route) => {
    cancelRequests += 1;
    expect(route.request().postDataJSON()).toMatchObject({ cancel_pending_calls: true });
    expect(route.request().headers()["idempotency-key"]).toBeTruthy();
    await json(route, {
      ...dispatchCase,
      state: "canceled",
      next_attempt_at: null,
      canceled_at: "2026-08-30T12:36:00Z",
    });
  });

  await openReviewer(page);
  await page.getByRole("button", { name: "Review this candidate" }).click();
  await expect(page.getByLabel(`Evidence video for candidate ${DETECTION_ID.slice(0, 8)}`)).toBeVisible();
  await page.getByRole("button", { name: "Confirm and review call options" }).click();
  await expect(page.getByLabel("Dispatch authorization")).toBeVisible();
  await expect(page.getByText(/candidate video remains directly above/i)).toBeVisible();
  await expect(page.getByText(/Demo Zone primary · •••• 1001/)).toBeVisible();

  const authorize = page.getByRole("button", { name: "Authorize call", exact: true });
  await expect(authorize).toBeDisabled();
  const authorizationCheck = page.getByLabel(/I explicitly authorize the bounded/);
  await authorizationCheck.focus();
  await page.keyboard.press("Space");
  await expect(authorizationCheck).toBeChecked();
  await expect(authorize).toBeEnabled();
  await authorize.click();

  await expect(page.getByRole("heading", { name: "Case CH-1042" })).toBeVisible();
  await expect(page.getByText("Primary attempt 1")).toBeVisible();
  await expect(page.getByText("Primary attempt 2")).toBeVisible();
  await expect(page.getByText("Supervisor escalation")).toBeVisible();
  await expect(page.getByText("•••• 1001").first()).toBeVisible();
  await expect(page.getByText("reviewer-demo-one")).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel before next attempt" })).toBeVisible();
  expect(dispatchRequests).toBe(1);
  await expect(page.getByText("+15551231001")).toHaveCount(0);

  await page.getByRole("button", { name: "Cancel before next attempt" }).click();
  await expect(page.getByLabel("Cancellation audit reason")).toBeVisible();
  await page.getByRole("button", { name: "Confirm cancellation" }).click();
  await expect(page.getByText("canceled", { exact: true })).toBeVisible();
  expect(cancelRequests).toBe(1);
});

test("admin demo test call remains masked and requires a second opt-in gate", async ({ page }) => {
  await mockSession(page, "tenant_admin");
  const contact = {
    contact_id: "52000000-0000-4000-8000-000000000001",
    zone_id: "demo-zone-a",
    broad_location_label: "Demo Zone A",
    coverage_h3_cells: ["8861892581fffff"],
    display_name: "Demo Zone primary",
    phone_masked: "•••• 1001",
    role: "primary",
    enabled: true,
    opted_in_for_demo: true,
    timezone: "Asia/Kolkata",
    calling_window_start: "08:00",
    calling_window_end: "22:00",
    last_verified_at: "2026-08-30T10:00:00Z",
    created_at: "2026-08-30T10:00:00Z",
    updated_at: "2026-08-30T10:00:00Z",
  };
  await page.route("**/v1/response-contacts", (route) =>
    json(route, { items: [contact], next_cursor: null }),
  );
  let testCalls = 0;
  await page.route("**/v1/response-contacts/*/test-calls", async (route) => {
    testCalls += 1;
    expect(route.request().postDataJSON()).toEqual({ authorize_test_call: true });
    await json(
      route,
      {
        test_call_id: "55000000-0000-4000-8000-000000000001",
        contact_id: contact.contact_id,
        contact_name: contact.display_name,
        phone_masked: contact.phone_masked,
        state: "simulated",
        created_at: "2026-08-30T12:40:00Z",
      },
      202,
    );
  });

  await page.goto("/#/console/response");
  await page.getByRole("button", { name: /Tenant admin · Demo One/ }).click();
  await expect(page.getByText("•••• 1001")).toBeVisible();
  await expect(page.getByText("+15551231001")).toHaveCount(0);
  await page.getByRole("button", { name: "Open test-call gate" }).click();
  const callButton = page.getByRole("button", { name: "Place demo test call" });
  await expect(callButton).toBeDisabled();
  await page.getByLabel(/belongs to an opted-in teammate/).check();
  await callButton.click();
  await expect(page.getByText(/Test call simulated/)).toBeVisible();
  expect(testCalls).toBe(1);
});

test("admin creates a zone contact and receives only the masked projection", async ({ page }) => {
  await mockSession(page, "tenant_admin");
  const created = {
    contact_id: "52000000-0000-4000-8000-000000000009",
    zone_id: "demo-zone-b",
    broad_location_label: "Demo Zone B",
    coverage_h3_cells: ["8861892581fffff"],
    display_name: "Demo Zone B supervisor",
    phone_masked: "•••• 3003",
    role: "supervisor",
    enabled: true,
    opted_in_for_demo: true,
    timezone: "Asia/Kolkata",
    calling_window_start: "08:00",
    calling_window_end: "22:00",
    last_verified_at: "2026-08-30T10:00:00Z",
    created_at: "2026-08-30T10:00:00Z",
    updated_at: "2026-08-30T10:00:00Z",
  };
  let stored: typeof created | null = null;
  await page.route("**/v1/response-contacts", async (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      expect(body).toMatchObject({
        zone_id: "demo-zone-b",
        broad_location_label: "Demo Zone B",
        coverage_h3_cells: ["8861892581fffff"],
        display_name: "Demo Zone B supervisor",
        phone_number: "+15551233003",
        role: "supervisor",
        opted_in_for_demo: true,
      });
      expect(route.request().headers()["idempotency-key"]).toBeTruthy();
      stored = created;
      await json(route, created, 201);
      return;
    }
    await json(route, { items: stored ? [stored] : [], next_cursor: null });
  });

  await page.goto("/#/console/response");
  await page.getByRole("button", { name: /Tenant admin · Demo One/ }).click();
  await page.getByLabel("Coverage zone").fill("demo-zone-b");
  await page.getByLabel("Broad location label").fill("Demo Zone B");
  await page.getByLabel("Coverage H3 cells").fill("8861892581fffff");
  await page.getByLabel("Contact label").fill("Demo Zone B supervisor");
  await page.getByLabel("Phone number (E.164)").fill("+15551233003");
  await page.getByLabel("Escalation role").selectOption("supervisor");
  await page.getByLabel("Opted in for hackathon demo calls").check();
  await page.getByRole("button", { name: "Add masked contact" }).click();

  await expect(page.getByText("•••• 3003")).toBeVisible();
  await expect(page.locator(".response-contact-list")).toContainText("Demo Zone B");
  await expect(page.getByText("+15551233003")).toHaveCount(0);
  await expect(page.getByLabel("Phone number (E.164)")).toHaveValue("");
});
