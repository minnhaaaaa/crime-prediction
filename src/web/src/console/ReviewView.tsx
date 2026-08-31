import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  newIdempotencyKey,
  type DispatchCase,
  type DispatchPreview,
  type PublicCandidate,
  type ReviewRequest,
} from "../api/client";
import { useAuth } from "./AuthContext";
import CandidateEvidence from "./CandidateEvidence";
import DispatchCasePanel from "./DispatchCasePanel";

const CATEGORIES = ["property", "violence", "public_order", "traffic_safety", "other"] as const;
const REASONS = [
  "false_positive",
  "insufficient_evidence",
  "duplicate",
  "outside_scope",
  "other",
] as const;

type ReviewPath = "reject" | "confirm_without_call" | "confirm_for_dispatch";

interface ReviewMutationInput {
  candidate: PublicCandidate;
  path: ReviewPath;
  body: ReviewRequest;
}

interface PendingDispatch {
  incidentId: string;
  detectionId: string;
  category: string;
  occurredAt: string;
}

function formatUtc(value: string): string {
  return `${new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(new Date(value))} UTC`;
}

function dispatchErrorMessage(error: ApiError): string {
  if (error.code === "dispatch_contact_unavailable") {
    return "Dispatch was not started: this registered zone does not have one enabled primary and one enabled supervisor contact.";
  }
  if (error.code === "dispatch_outside_calling_window") {
    return "Dispatch was not started because the configured contacts are outside their approved calling hours.";
  }
  if (error.code === "dispatch_quota_exceeded") {
    return "Dispatch was not started because this tenant reached its daily call quota.";
  }
  return `Dispatch authorization failed (${error.code}): ${error.message}`;
}

export default function ReviewView() {
  const { session } = useAuth();
  const token = session!.token;
  const tenantId = session!.activeTenantId;
  const queryClient = useQueryClient();

  const candidates = useQuery({
    queryKey: ["candidates", tenantId],
    queryFn: () => api.candidates(token),
  });

  const [decisionFor, setDecisionFor] = useState<string | null>(null);
  const [dispatchDecisionFor, setDispatchDecisionFor] = useState<string | null>(null);
  const [decision, setDecision] = useState<"confirmed" | "rejected">("confirmed");
  const [category, setCategory] = useState<(typeof CATEGORIES)[number]>("property");
  const [reason, setReason] = useState<(typeof REASONS)[number]>("false_positive");
  const [callAuthorized, setCallAuthorized] = useState(false);
  const [flowNotice, setFlowNotice] = useState<string | null>(null);
  const [flowError, setFlowError] = useState<string | null>(null);
  const [pendingDispatch, setPendingDispatch] = useState<PendingDispatch | null>(null);
  const [dispatchPreview, setDispatchPreview] = useState<DispatchPreview | null>(null);
  const [activeDispatch, setActiveDispatch] = useState<DispatchCase | null>(null);
  const actionKeys = useRef(new Map<string, string>());

  const keyFor = (action: string) => {
    const existing = actionKeys.current.get(action);
    if (existing) return existing;
    const created = newIdempotencyKey();
    actionKeys.current.set(action, created);
    return created;
  };

  const dispatch = useMutation({
    mutationFn: (pending: PendingDispatch) =>
      api.authorizeDispatch(
        token,
        pending.incidentId,
        keyFor(`dispatch:${pending.incidentId}`),
      ),
    onSuccess: (result, pending) => {
      actionKeys.current.delete(`dispatch:${pending.incidentId}`);
      setPendingDispatch(null);
      setDispatchPreview(null);
      setDecisionFor(null);
      setDispatchDecisionFor(null);
      setActiveDispatch(result);
      setFlowError(null);
      setFlowNotice(
        `Incident confirmed and call dispatch explicitly authorized. Case ${result.case_reference} is now ${result.state.replace(/_/g, " ")}.`,
      );
      void queryClient.invalidateQueries({ queryKey: ["candidates", tenantId] });
    },
  });

  const preview = useMutation({
    mutationFn: (pending: PendingDispatch) => api.dispatchPreview(token, pending.incidentId),
    onSuccess: (result) => {
      setDispatchPreview(result);
      setFlowNotice(
        "Incident confirmed. Review the server-resolved masked contacts, then explicitly authorize a call or finish without calling.",
      );
    },
  });

  const review = useMutation({
    mutationFn: async (input: ReviewMutationInput) => ({
      input,
      result: await api.reviewCandidate(
        token,
        input.candidate.detection_id,
        input.body,
        keyFor(`review:${input.candidate.detection_id}:${input.path}`),
      ),
    }),
    onSuccess: ({ input, result }) => {
      actionKeys.current.delete(`review:${input.candidate.detection_id}:${input.path}`);
      setFlowError(null);
      if (input.path === "reject") {
        setDecisionFor(null);
        setDispatchDecisionFor(null);
        setCallAuthorized(false);
        setFlowNotice("Candidate rejected. No incident was promoted and no call was created.");
        void queryClient.invalidateQueries({ queryKey: ["candidates", tenantId] });
        return;
      }
      if (input.path === "confirm_without_call") {
        setDecisionFor(null);
        setDispatchDecisionFor(null);
        setCallAuthorized(false);
        setFlowNotice("Incident confirmed by a human reviewer. No call was authorized or created.");
        void queryClient.invalidateQueries({ queryKey: ["candidates", tenantId] });
        return;
      }
      if (result.decision !== "confirmed" || result.detection_id !== input.candidate.detection_id) {
        setDecisionFor(null);
        setDispatchDecisionFor(null);
        setFlowError(
          "The confirmation response did not match this candidate. No call was attempted.",
        );
        void queryClient.invalidateQueries({ queryKey: ["candidates", tenantId] });
        return;
      }
      const pending = {
        // Dispatch endpoints resolve the immutable confirmed review from the
        // candidate detection UUID. The promoted external event identifier is
        // an audit link (for example `video-candidate:<uuid>`), not a routable
        // incident identifier.
        incidentId: result.detection_id,
        detectionId: input.candidate.detection_id,
        category: result.confirmed_category ?? category,
        occurredAt: input.candidate.occurred_at,
      };
      setPendingDispatch(pending);
      setDispatchPreview(null);
      setDispatchDecisionFor(input.candidate.detection_id);
      setCallAuthorized(false);
      setFlowNotice("Incident confirmed. Loading the safe dispatch summary; no call has been authorized.");
      preview.mutate(pending);
    },
  });

  const listError = candidates.error instanceof ApiError ? candidates.error : null;
  const reviewError = review.error instanceof ApiError ? review.error : null;
  const dispatchError = dispatch.error instanceof ApiError ? dispatch.error : null;
  const previewError = preview.error instanceof ApiError ? preview.error : null;
  const pendingCandidates =
    candidates.data?.items.filter((candidate) => candidate.review_status === "awaiting_review")
    ?? [];

  const closeDecision = () => {
    setDecisionFor(null);
    setDispatchDecisionFor(null);
    setDispatchPreview(null);
    setPendingDispatch(null);
    setCallAuthorized(false);
    review.reset();
    preview.reset();
    dispatch.reset();
  };

  if (listError?.status === 403) {
    return (
      <section className="review-view">
        <h2 className="section-title">
          Review <span className="accent">queue</span>
        </h2>
        <div className="forbidden" role="alert">
          <p>
            Candidate review requires the <strong>reviewer</strong> role. Your current role is{" "}
            {session!.role}. Evidence and review decisions are restricted.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="review-view">
      <h2 className="section-title">
        Review <span className="accent">queue</span>
      </h2>
      <p className="muted">
        Detections below are <strong>unconfirmed candidates</strong> proposed by automated
        video analysis. A human decision can confirm an incident, but it can never place a
        call by itself. Decisions are final and immutable. Voice notification requires a
        second, explicit authorization.
      </p>

      {flowNotice && <p className="ok-banner review-flow-notice" role="status">{flowNotice}</p>}
      {flowError && <p className="error-banner review-flow-notice" role="alert">{flowError}</p>}
      {dispatchError && (
        <div className="error-banner review-flow-notice" role="alert">
          <p>{dispatchErrorMessage(dispatchError)}</p>
          {pendingDispatch && (
            <button
              type="button"
              className="ghost inline-action"
              disabled={dispatch.isPending}
              onClick={() => dispatch.mutate(pendingDispatch)}
            >
              Retry the same authorized dispatch
            </button>
          )}
        </div>
      )}

      {activeDispatch && (
        <DispatchCasePanel
          token={token}
          tenantId={tenantId}
          initialCase={activeDispatch}
        />
      )}

      {candidates.isLoading && <p className="muted">Loading candidates…</p>}
      {listError && listError.status !== 403 && (
        <p role="alert" className="error-banner">
          Could not load candidates ({listError.code}).
        </p>
      )}
      {!candidates.isLoading && pendingCandidates.length === 0 && (
        <p className="muted">No candidates awaiting review.</p>
      )}

      <ul className="candidate-list">
        {pendingCandidates.map((candidate) => {
          const open = decisionFor === candidate.detection_id;
          const dispatchStep = dispatchDecisionFor === candidate.detection_id;
          const decided = candidate.review_status !== "awaiting_review";
          return (
            <li key={candidate.detection_id} className="candidate-card">
              <div className="row spread">
                <span className="chip chip-warn">UNCONFIRMED CANDIDATE</span>
                <span className={`chip ${decided ? "" : "chip-accent"}`}>
                  {candidate.review_status.replace(/_/g, " ")}
                </span>
              </div>
              <dl className="provenance">
                <dt>Proposed category</dt>
                <dd>{candidate.proposed_category.replace(/_/g, " ")}</dd>
                <dt>Observed event</dt>
                <dd>{candidate.event_type.replace(/_/g, " ")}</dd>
                <dt>Reka observation</dt>
                <dd>
                  {candidate.description}
                  <br />
                  <small>AI-generated and unconfirmed; verify against the evidence.</small>
                </dd>
                <dt>Occurred at</dt>
                <dd>{formatUtc(candidate.occurred_at)}</dd>
                <dt>Detector</dt>
                <dd>
                  {candidate.detector_version} · analysis confidence{" "}
                  {(candidate.confidence * 100).toFixed(0)}%
                </dd>
                <dt>Expires</dt>
                <dd>{formatUtc(candidate.expires_at)}</dd>
                <dt>Evidence</dt>
                <dd>
                  {candidate.evidence_available
                    ? "Available to reviewers via the secured evidence flow"
                    : "Unavailable"}
                </dd>
              </dl>

              <CandidateEvidence
                token={token}
                detectionId={candidate.detection_id}
                available={candidate.evidence_available}
                loadWhenReviewing={open}
              />

              {decided ? (
                <p className="muted small">
                  A final review exists for this candidate; it cannot be changed.
                </p>
              ) : open ? (
                <div className="decision-form">
                  {!dispatchStep && (
                    <>
                      <div className="row" role="radiogroup" aria-label="Final review decision">
                        <label>
                          <input
                            type="radio"
                            name={`decision-${candidate.detection_id}`}
                            checked={decision === "confirmed"}
                            onChange={() => setDecision("confirmed")}
                          />
                          Confirm incident
                        </label>
                        <label>
                          <input
                            type="radio"
                            name={`decision-${candidate.detection_id}`}
                            checked={decision === "rejected"}
                            onChange={() => setDecision("rejected")}
                          />
                          Reject candidate
                        </label>
                      </div>
                      {decision === "confirmed" ? (
                        <label>
                          Confirmed category
                          <select
                            value={category}
                            onChange={(event) =>
                              setCategory(event.target.value as (typeof CATEGORIES)[number])
                            }
                          >
                            {CATEGORIES.map((item) => (
                              <option key={item} value={item}>
                                {item.replace("_", " ")}
                              </option>
                            ))}
                          </select>
                        </label>
                      ) : (
                        <label>
                          Rejection reason
                          <select
                            value={reason}
                            onChange={(event) =>
                              setReason(event.target.value as (typeof REASONS)[number])
                            }
                          >
                            {REASONS.map((item) => (
                              <option key={item} value={item}>
                                {item.replace("_", " ")}
                              </option>
                            ))}
                          </select>
                        </label>
                      )}
                      <p className="muted small">
                        This review is final. Rejecting never creates an incident or dispatch case.
                      </p>
                      <div className="row">
                        {decision === "confirmed" ? (
                          <>
                            <button
                              type="button"
                              disabled={review.isPending}
                              onClick={() =>
                                review.mutate({
                                  candidate,
                                  path: "confirm_for_dispatch",
                                  body: { decision: "confirmed", confirmed_category: category },
                                })
                              }
                            >
                              {review.isPending ? "Confirming incident…" : "Confirm and review call options"}
                            </button>
                            <button
                              type="button"
                              className="ghost"
                              disabled={review.isPending}
                              onClick={() =>
                                review.mutate({
                                  candidate,
                                  path: "confirm_without_call",
                                  body: { decision: "confirmed", confirmed_category: category },
                                })
                              }
                            >
                              Confirm without calling
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            disabled={review.isPending}
                            onClick={() =>
                              review.mutate({
                                candidate,
                                path: "reject",
                                body: { decision: "rejected", rejection_reason: reason },
                              })
                            }
                          >
                            {review.isPending ? "Rejecting…" : "Submit final rejection"}
                          </button>
                        )}
                        <button
                          type="button"
                          className="ghost"
                          disabled={review.isPending}
                          onClick={closeDecision}
                        >
                          Cancel
                        </button>
                      </div>
                    </>
                  )}

                  {dispatchStep && (
                    <section className="dispatch-authorization" aria-label="Dispatch authorization">
                      <div className="row spread">
                        <div>
                          <p className="eyebrow">Separate human decision</p>
                          <h3>Incident confirmed — choose notification</h3>
                        </div>
                        <span className="chip chip-warn">NO AUTOMATIC CALL</span>
                      </div>
                      <p className="muted small">
                        The candidate video remains directly above. The immutable human confirmation
                        is complete; no call occurs unless you separately authorize it below.
                      </p>
                      {preview.isPending && <p className="muted">Resolving masked contacts from the registered zone…</p>}
                      {dispatchPreview && (
                        <dl className="dispatch-review-summary">
                          <div><dt>Case reference</dt><dd>{dispatchPreview.case_reference}</dd></div>
                          <div><dt>Category</dt><dd>{dispatchPreview.category.replace(/_/g, " ")}</dd></div>
                          <div><dt>UTC time</dt><dd>{formatUtc(dispatchPreview.occurred_at)}</dd></div>
                          <div><dt>Area</dt><dd>{dispatchPreview.zone_label}</dd></div>
                          <div><dt>Primary POC</dt><dd>{dispatchPreview.primary_contact.display_name} · {dispatchPreview.primary_contact.phone_masked} · attempts 1 and 2</dd></div>
                          <div><dt>Supervisor</dt><dd>{dispatchPreview.supervisor_contact.display_name} · {dispatchPreview.supervisor_contact.phone_masked} · attempt 3</dd></div>
                          <div><dt>Retry delay</dt><dd>{dispatchPreview.retry_delay_seconds} seconds</dd></div>
                          <div><dt>Maximum calls</dt><dd>{dispatchPreview.maximum_attempts}</dd></div>
                        </dl>
                      )}
                      {previewError && (
                        <div className="error-banner" role="alert">
                          Safe dispatch summary unavailable ({previewError.code}). No call was authorized.
                          {pendingDispatch && (
                            <button
                              type="button"
                              className="ghost inline-action"
                              disabled={preview.isPending}
                              onClick={() => preview.mutate(pendingDispatch)}
                            >
                              Retry safe summary
                            </button>
                          )}
                        </div>
                      )}
                      <div className="dispatch-policy-note" role="note">
                        <strong>Call contains:</strong> case reference, confirmed category, broad
                        area label, UTC time and keypad acknowledgement only. No identities,
                        descriptions, raw coordinates or forecast scores are spoken.
                      </div>
                      <label className="authorization-check">
                        <input
                          type="checkbox"
                          checked={callAuthorized}
                          disabled={!dispatchPreview || preview.isPending}
                          onChange={(event) => setCallAuthorized(event.target.checked)}
                        />
                        I explicitly authorize the bounded 2× primary → 1× supervisor call path
                        for this human-confirmed incident.
                      </label>
                      <div className="row">
                        <button
                          type="button"
                          disabled={!callAuthorized || !dispatchPreview || !pendingDispatch || dispatch.isPending}
                          onClick={() => pendingDispatch && dispatch.mutate(pendingDispatch)}
                        >
                          {dispatch.isPending ? "Authorizing call…" : "Authorize call"}
                        </button>
                        <button
                          type="button"
                          className="ghost"
                          disabled={dispatch.isPending}
                          onClick={() => {
                            setDecisionFor(null);
                            setDispatchDecisionFor(null);
                            setDispatchPreview(null);
                            setPendingDispatch(null);
                            setCallAuthorized(false);
                            setFlowNotice("Incident remains confirmed. No call was authorized or created.");
                            void queryClient.invalidateQueries({ queryKey: ["candidates", tenantId] });
                          }}
                        >
                          Finish without calling
                        </button>
                      </div>
                    </section>
                  )}

                  {reviewError && (
                    <p role="alert" className="error-banner">
                      {reviewError.code === "review_final"
                        ? "A final review already exists. No new call was created."
                        : `Review failed (${reviewError.code}): ${reviewError.message}`}
                    </p>
                  )}
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    setDecisionFor(candidate.detection_id);
                    setDispatchDecisionFor(null);
                    setDecision("confirmed");
                    setCallAuthorized(false);
                    setFlowError(null);
                    setDispatchPreview(null);
                    setPendingDispatch(null);
                    review.reset();
                    preview.reset();
                    dispatch.reset();
                  }}
                >
                  Review this candidate
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
